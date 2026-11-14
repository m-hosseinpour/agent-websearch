import asyncio
import json
import os
import socket
import subprocess
import threading
import time

import pytest

from websearch.config import Config, EgressConfig, TunnelConfig
from websearch.egress.tunnel import (
    PROBE_EXPIRED,
    PROBE_FAILED,
    Tunnel,
    TunnelBusy,
    TunnelUnavailable,
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_config(tmp_path, port: int, binary: str, stop_wait: float = 10.0) -> Config:
    return Config(
        home=tmp_path,
        egress=EgressConfig(proxy_port=port),
        tunnel=TunnelConfig(
            binary=binary,
            pool_file="pool.txt",
            start_attempts=5,
            start_timeout=10.0,
            port_timeout=1.0,
            stop_wait=stop_wait,
            probe_attempts=1,
            dead_cooldown=90,
        ),
    )


def test_a_broken_link_does_not_abort_the_rotation(tmp_path):
    """xray exits before opening its port: that link must be cooled down, next tried."""
    calls = tmp_path / "calls.log"
    binary = tmp_path / "fake-xray"
    binary.write_text(f"#!/bin/sh\necho run >> {calls}\nexit 1\n", encoding="utf-8")
    binary.chmod(0o755)

    (tmp_path / "pool.txt").write_text(
        "vless://11111111-1111-1111-1111-111111111111@a.example:443?security=tls#a\n"
        "vless://22222222-2222-2222-2222-222222222222@b.example:443?security=tls#b\n",
        encoding="utf-8",
    )
    tunnel = Tunnel(make_config(tmp_path, free_port(), str(binary)))

    with pytest.raises(TunnelUnavailable):
        asyncio.run(tunnel.ensure(deadline=time.monotonic() + 10))

    assert len(calls.read_text().splitlines()) == 2  # both candidates were attempted
    state = json.loads((tmp_path / "state.json").read_text())
    assert len(state["dead"]) == 2


def test_umask_does_not_make_credentials_readable(tmp_path):
    calls = tmp_path / "calls.log"
    binary = tmp_path / "fake-xray"
    binary.write_text(f"#!/bin/sh\necho run >> {calls}\nexit 1\n", encoding="utf-8")
    binary.chmod(0o755)
    (tmp_path / "pool.txt").write_text(
        "vless://33333333-3333-3333-3333-333333333333@c.example:443?security=tls#c\n",
        encoding="utf-8",
    )
    tunnel = Tunnel(make_config(tmp_path, free_port(), str(binary)))

    old = os.umask(0o022)
    try:
        with pytest.raises(TunnelUnavailable):
            asyncio.run(tunnel.ensure(deadline=time.monotonic() + 5))
    finally:
        os.umask(old)

    gateway = tmp_path / "gateway.json"
    assert gateway.stat().st_mode & 0o077 == 0


def test_explicit_stop_kills_a_tunnel_this_process_does_not_own(tmp_path, monkeypatch):
    tunnel = Tunnel(make_config(tmp_path, free_port(), "/bin/true"))
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    (tmp_path / "tunnel.pid").write_text(str(proc.pid))
    monkeypatch.setattr(tunnel, "_is_ours", lambda pid: True)

    asyncio.run(tunnel.stop())

    for _ in range(20):
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    assert proc.poll() is not None  # `websearch tunnel down` must really stop it


def test_busy_rotation_is_its_own_error_and_marks_nothing_dead(tmp_path):
    (tmp_path / "pool.txt").write_text(
        "vless://44444444-4444-4444-4444-444444444444@d.example:443?security=tls#d\n",
        encoding="utf-8",
    )
    tunnel = Tunnel(make_config(tmp_path, free_port(), "/bin/true"))
    tunnel._sync_lock.acquire()  # an in-flight rotation holds the lock
    try:
        with pytest.raises(TunnelBusy):
            asyncio.run(tunnel.ensure(deadline=time.monotonic() + 0.3))
    finally:
        tunnel._sync_lock.release()
        tunnel._idle.set()

    assert not (tmp_path / "state.json").exists()


def test_an_expired_probe_does_not_mark_the_config_dead(tmp_path, monkeypatch):
    (tmp_path / "pool.txt").write_text(
        "vless://55555555-5555-5555-5555-555555555555@e.example:443?security=tls#e\n",
        encoding="utf-8",
    )
    tunnel = Tunnel(make_config(tmp_path, free_port(), "/bin/true"))
    monkeypatch.setattr(tunnel, "_running", lambda: True)
    monkeypatch.setattr(tunnel, "_probe", lambda deadline=None: (PROBE_EXPIRED, None))
    monkeypatch.setattr(tunnel, "_stop", lambda: pytest.fail("must not stop a live tunnel"))

    with pytest.raises(TunnelBusy):
        asyncio.run(tunnel.ensure(deadline=time.monotonic() + 5))

    assert not (tmp_path / "state.json").exists()


def test_probe_reports_expired_when_the_deadline_cuts_it_short(tmp_path):
    # A listener that accepts and never answers: the attempt is cut short.
    hanging = socket.socket()
    hanging.bind(("127.0.0.1", 0))
    hanging.listen(1)
    try:
        tunnel = Tunnel(make_config(tmp_path, hanging.getsockname()[1], "/bin/true"))
        status, seconds = tunnel._probe(deadline=time.monotonic() + 0.4)
    finally:
        hanging.close()

    assert status == PROBE_EXPIRED
    assert seconds is None


def test_probe_reports_failed_for_a_fast_refusal(tmp_path):
    # Closed port, full budget: this is a real failure, not an expired probe.
    tunnel = Tunnel(make_config(tmp_path, free_port(), "/bin/true"))

    status, seconds = tunnel._probe(deadline=time.monotonic() + 5)

    assert status == PROBE_FAILED
    assert seconds is None


def test_ensure_waits_for_an_in_flight_rotation(tmp_path, monkeypatch):
    tunnel = Tunnel(make_config(tmp_path, free_port(), "/bin/true"))
    monkeypatch.setattr(tunnel, "_ensure_locked", lambda force, deadline: None)

    tunnel._idle.clear()
    tunnel._sync_lock.acquire()
    release = threading.Timer(0.2, lambda: (tunnel._sync_lock.release(), tunnel._idle.set()))
    release.start()
    try:
        # The deadline is long enough to wait: this must not raise TunnelBusy.
        asyncio.run(tunnel.ensure(deadline=time.monotonic() + 2))
    finally:
        release.cancel()


def test_stop_during_a_rotation_still_kills_the_process(tmp_path, monkeypatch):
    tunnel = Tunnel(make_config(tmp_path, free_port(), "/bin/true", stop_wait=0.3))
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    (tmp_path / "tunnel.pid").write_text(str(proc.pid))
    monkeypatch.setattr(tunnel, "_is_ours", lambda pid: True)

    tunnel._idle.clear()
    tunnel._sync_lock.acquire()  # simulate a rotation that will not yield
    try:
        asyncio.run(tunnel.stop())
    finally:
        tunnel._sync_lock.release()
        tunnel._idle.set()

    for _ in range(20):
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    assert proc.poll() is not None
    # The flag must outlive the forced stop: the rotation is still alive, so it
    # must not be able to start a new xray after `down`.
    assert tunnel._stopping.is_set()
