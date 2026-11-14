"""Own xray process: pool of share links, canary probe, rotation.

Adapted from the ``targeted-proxy-access`` skill (``scripts/tunnel.py``), with one
deliberate difference: this inbound carries **no domain routing rules**. The
proxy is a plain pipe, so all routing decisions stay in ``router.py`` where the
learned list is. Rotation = restart xray with another outbound.

Data lives next to the rest of the service (``WEBSEARCH_HOME``):
  pool.txt     one vless:// or trojan:// link per line, top tried first
  state.json   last good config, dead cooldowns
  gateway.json generated xray config (do not edit by hand)
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from ..config import Config

SCAN_TIMEOUT = 6
XRAY_VERSION = os.environ.get("WEBSEARCH_XRAY_VERSION") or "26.10.31"
LOG_MAX_BYTES = 2_000_000


class TunnelUnavailable(Exception):
    pass


class TunnelBusy(TunnelUnavailable):
    """Another rotation is in flight, or the clock ran out before a verdict.

    Kept separate so the router can report a transient condition without
    opening the backend's breaker.
    """


# A probe that ran out of time says nothing about the config's health.
PROBE_OK = "ok"
PROBE_FAILED = "failed"
PROBE_EXPIRED = "expired"


# --------------------------------------------------------------- xray binary
def _asset_name() -> str:
    osname = {"nt": "windows", "darwin": "macos"}.get(os.name, "linux")
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64-v8a"
    elif machine in ("i386", "i686", "x86"):
        arch = "32"
    elif machine.startswith("armv7"):
        arch = "arm32-v7a"
    elif machine.startswith("armv6"):
        arch = "arm32-v6"
    else:
        raise TunnelUnavailable(f"no xray build for {os.name}/{machine}; set WEBSEARCH_XRAY_BIN")
    return f"Xray-{osname}-{arch}.zip"


def _http_get(url: str, timeout: int = 180) -> bytes:
    request = urllib.request.Request(url, headers={"user-agent": "agent-websearch/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _latest_release() -> str | None:
    try:
        meta = json.loads(
            _http_get("https://api.github.com/repos/XTLS/Xray-core/releases/latest", timeout=30)
        )
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return str(meta.get("tag_name") or "").lstrip("v") or None


def _release_files(version: str, asset: str) -> tuple[str, bytes, str]:
    def fetch(tag: str) -> tuple[bytes, str]:
        url = f"https://github.com/XTLS/Xray-core/releases/download/v{tag}/{asset}"
        return _http_get(url), _http_get(url + ".dgst", timeout=30).decode("utf-8", "replace")

    try:
        archive, digest = fetch(version)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        latest = _latest_release()
        if not latest or latest == version:
            raise
        version = latest
        archive, digest = fetch(version)
    return version, archive, digest


def download_xray(config: Config) -> str:
    """Download and checksum the official release once, then cache it."""
    asset = _asset_name()
    try:
        version, archive, digest_text = _release_files(XRAY_VERSION, asset)
    except (OSError, urllib.error.URLError) as exc:
        raise TunnelUnavailable(f"xray download failed ({exc}); set WEBSEARCH_XRAY_BIN") from exc
    expected = re.search(r"SHA2-256=\s*([0-9a-f]{64})", digest_text)
    if expected is None or hashlib.sha256(archive).hexdigest() != expected.group(1):
        raise TunnelUnavailable("downloaded xray archive failed its SHA-256 check")
    target = config.bin_dir / f"xray-{version}"
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        member = next((n for n in bundle.namelist() if Path(n).name in ("xray", "xray.exe")), None)
        if member is None:
            raise TunnelUnavailable(f"{asset} does not contain an xray binary")
        binary = target / Path(member).name
        binary.write_bytes(bundle.read(member))
    binary.chmod(0o755)
    return str(binary)


def find_binary(config: Config) -> str | None:
    env = os.environ.get("WEBSEARCH_XRAY_BIN") or os.environ.get("XRAY_BIN")
    if env and Path(env).exists():
        return env
    on_path = shutil.which("xray")
    if on_path:
        return on_path
    name = "xray.exe" if os.name == "nt" else "xray"
    roots = []
    if os.name == "nt":
        for base in (os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")):
            if base:
                roots.append(Path(base) / "v2rayN")
    elif sys.platform == "darwin":
        roots.append(Path.home() / "Library/Application Support/v2rayN")
    else:
        roots += [Path.home() / ".local/share/v2rayN", Path.home() / "v2rayN"]
    for root in roots:
        for candidate in (root / "bin/xray" / name, root / "bin" / name):
            if candidate.exists():
                return str(candidate)
    for folder in sorted(config.bin_dir.glob("xray-*"), reverse=True):
        candidate = folder / name
        if candidate.exists():
            return str(candidate)
    return None


def ensure_binary(config: Config) -> str:
    # An explicit path in the config wins over everything else: the user said
    # which binary to run, and silently preferring PATH would hide a mistake.
    if config.tunnel.binary:
        candidate = Path(config.tunnel.binary).expanduser()
        if not candidate.exists():
            raise TunnelUnavailable(f"xray binary not found: {candidate}")
        return str(candidate)
    found = find_binary(config)
    if found:
        return found
    return download_xray(config)


def configured_binary(config: Config) -> str | None:
    """The binary this config would use, without downloading anything."""
    if config.tunnel.binary:
        candidate = Path(config.tunnel.binary).expanduser()
        return str(candidate) if candidate.exists() else None
    return find_binary(config)


# --------------------------------------------------------------- share links
def _q(query: dict, *names: str, default: str = "") -> str:
    for name in names:
        values = query.get(name)
        if values:
            return values[0]
    return default


def parse_uri(uri: str) -> dict:
    """Turn one vless:// or trojan:// share link into an xray outbound."""
    parsed = urllib.parse.urlparse(uri.strip())
    scheme = parsed.scheme.lower()
    if scheme not in ("vless", "trojan"):
        raise ValueError(f"unsupported scheme {scheme!r} (only vless and trojan)")
    if not parsed.hostname or not parsed.port:
        raise ValueError("missing host or port")

    query = urllib.parse.parse_qs(parsed.query)
    net = _q(query, "type", "network", default="tcp").lower()
    net = {"raw": "tcp", "http": "tcp"}.get(net, net)
    sec = _q(query, "security", default="none").lower()
    sni = _q(query, "sni", "peer") or parsed.hostname
    host_header = _q(query, "host") or sni

    stream: dict = {"network": net}
    if net == "ws":
        stream["wsSettings"] = {
            "path": _q(query, "path", default="/"),
            "headers": {"Host": host_header},
        }
    elif net == "grpc":
        stream["grpcSettings"] = {"serviceName": _q(query, "serviceName")}
    elif net == "tcp" and _q(query, "headerType").lower() == "http":
        stream["tcpSettings"] = {
            "header": {
                "type": "http",
                "request": {
                    "path": [_q(query, "path", default="/")],
                    "headers": {"Host": [host_header]},
                },
            }
        }
    elif net == "xhttp":
        stream["xhttpSettings"] = {
            "path": _q(query, "path", default="/"),
            "host": host_header,
            "mode": _q(query, "mode", default="auto"),
        }

    if sec in ("tls", "reality"):
        stream["security"] = sec
        tls = {"serverName": sni, "allowInsecure": _q(query, "allowInsecure") == "1"}
        alpn = _q(query, "alpn")
        if alpn:
            tls["alpn"] = [item for item in alpn.split(",") if item]
        fingerprint = _q(query, "fp", "fingerprint")
        if fingerprint:
            tls["fingerprint"] = fingerprint
        stream["tlsSettings"] = tls
        if sec == "reality":
            stream["realitySettings"] = {
                "serverName": sni,
                "fingerprint": fingerprint or "chrome",
                "publicKey": _q(query, "pbk"),
                "shortId": _q(query, "sid"),
                "spiderX": _q(query, "spx", default="/"),
            }
    elif sec not in ("none", ""):
        raise ValueError(f"unsupported security {sec!r}")

    if scheme == "vless":
        user = {
            "id": urllib.parse.unquote(parsed.username or ""),
            "encryption": _q(query, "encryption", default="none"),
        }
        flow = _q(query, "flow")
        if flow:
            user["flow"] = flow
        out = {
            "protocol": "vless",
            "settings": {"vnext": [{"address": parsed.hostname, "port": parsed.port, "users": [user]}]},
        }
    else:
        out = {
            "protocol": "trojan",
            "settings": {
                "servers": [
                    {
                        "address": parsed.hostname,
                        "port": parsed.port,
                        "password": urllib.parse.unquote(parsed.username or ""),
                    }
                ]
            },
        }
    out["streamSettings"] = stream
    return out


def key_of(uri: str) -> str:
    return hashlib.sha256(uri.encode()).hexdigest()[:16]


def load_pool(path: Path) -> list[str]:
    if not path.exists():
        return []
    links = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            links.append(line)
    return links


# ------------------------------------------------------------------ tunnel
class Tunnel:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.port = config.egress.proxy_port
        self._healthy_until = 0.0
        self._lock: asyncio.Lock | None = None
        self._sync_lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._stopping = threading.Event()
        self._started_here = False
        self._binary: str | None = None

    # ---- lifecycle -------------------------------------------------------
    async def ensure(self, force: bool = False, deadline: float | None = None) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        if deadline is None:
            deadline = time.monotonic() + self.config.tunnel.start_timeout
        async with self._lock:
            await asyncio.to_thread(self._ensure_sync, force, deadline)

    async def aclose(self) -> None:
        # Only stop a tunnel this process started. `websearch search` shares the
        # pidfile with the running service and must not kill its egress.
        if self._started_here:
            await self.stop()

    async def stop(self) -> None:
        """Explicit shutdown (CLI `tunnel down`): stop the pidfile's process."""
        await asyncio.to_thread(self._stop_sync)

    def health(self) -> dict:
        return {
            "port": self.port,
            "running": self._running(),
            "healthy_cached": time.time() < self._healthy_until,
            "pool": len(load_pool(self.config.pool_path)),
            "binary": self._binary or configured_binary(self.config),
        }

    # ---- sync internals (run in a worker thread) -------------------------
    def _ensure_sync(self, force: bool, deadline: float) -> None:
        # A new explicit request lifts a previous stop; the flag otherwise stays
        # set so a rotation that outlived `tunnel down` cannot start xray again.
        self._stopping.clear()
        # The asyncio lock can be released by cancellation while this thread is
        # still rotating. Wait for that rotation (bounded by the caller's
        # deadline) instead of starting a second xray or failing the request.
        # Clear before acquiring: when the event is set, the previous owner has
        # already released the lock, so a set event means "try now".
        self._idle.clear()
        if not self._sync_lock.acquire(blocking=False):
            remaining = max(0.0, deadline - time.monotonic())
            self._idle.wait(remaining)
            if not self._sync_lock.acquire(timeout=0.1):
                raise TunnelBusy("rotation still in progress")
        try:
            self._ensure_locked(force, deadline)
        finally:
            self._sync_lock.release()
            self._idle.set()

    def _ensure_locked(self, force: bool, deadline: float) -> None:
        if self._stopping.is_set():
            raise TunnelBusy("stop requested")
        if not force and time.time() < self._healthy_until and self._port_open():
            return
        if deadline - time.monotonic() <= 0.5:
            raise TunnelBusy("no time left to start the tunnel")
        if not self.config.tunnel.auto_start and not self._running():
            raise TunnelUnavailable("tunnel is stopped (auto_start = false)")

        pool = load_pool(self.config.pool_path)
        if not pool:
            raise TunnelUnavailable(f"pool is empty: {self.config.pool_path}")

        binary = self._binary or ensure_binary(self.config)
        self._binary = binary

        if self._running():
            status, _ = self._probe(deadline)
            if status == PROBE_OK:
                self._healthy_until = time.time() + self.config.tunnel.healthy_ttl
                return
            if status == PROBE_EXPIRED:
                raise TunnelBusy("canary probe ran out of time")
            self._mark_current_dead("probe_failed")
            self._stop()

        if self._port_open():
            # Someone else holds the port; silently stealing it would break them.
            raise TunnelUnavailable(f"port {self.port} is in use by another process")

        self._start_any(binary, pool, deadline)

    def _start_any(self, binary: str, pool: list[str], deadline: float) -> None:
        state = self._load_state()
        now = time.time()
        dead = state.get("dead", {})
        candidates = [(key_of(uri), uri) for uri in pool if dead.get(key_of(uri), 0) <= now]
        if not candidates:  # cooldowns must never hide every option
            candidates = [(key_of(uri), uri) for uri in pool]
        candidates = candidates[: max(1, self.config.tunnel.start_attempts)]

        for key, uri in candidates:
            if self._stopping.is_set():
                raise TunnelBusy("stop requested")
            if deadline - time.monotonic() <= 1.0:
                break
            try:
                outbound = parse_uri(uri)
            except ValueError:
                state.setdefault("dead", {})[key] = int(now + self.config.tunnel.dead_cooldown)
                self._save_state(state)
                continue
            try:
                self._start_with(binary, outbound, deadline)
            except OSError as exc:
                # The binary itself is the problem; trying more links cannot help.
                raise TunnelUnavailable(f"xray failed to start: {exc}") from exc
            except TunnelBusy:
                # Started but not confirmed yet; leave it for the next request.
                raise
            except TunnelUnavailable:
                # This link's config is unusable (or xray rejected it): cooldown
                # it and move on instead of aborting the whole rotation.
                state.setdefault("dead", {})[key] = int(now + self.config.tunnel.dead_cooldown)
                self._save_state(state)
                self._stop()
                continue
            status, _ = self._probe(deadline)
            if status == PROBE_OK:
                if self._stopping.is_set():
                    # A stop arrived while the canary was being checked.
                    self._stop()
                    raise TunnelBusy("stop requested")
                state["last_good"] = key
                state.get("dead", {}).pop(key, None)
                self._save_state(state)
                self._healthy_until = time.time() + self.config.tunnel.healthy_ttl
                return
            if status == PROBE_EXPIRED:
                # The config may be fine; we only ran out of time to prove it.
                raise TunnelBusy("canary probe ran out of time")
            state.setdefault("dead", {})[key] = int(now + self.config.tunnel.dead_cooldown)
            self._save_state(state)
            self._stop()
        raise TunnelUnavailable("no config in the pool could open the canary")

    def _start_with(self, binary: str, outbound: dict, deadline: float) -> None:
        self._stop()
        if self._stopping.is_set():
            raise TunnelBusy("stop requested")
        self.config.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        gateway = {
            "log": {"loglevel": "warning"},
            "inbounds": [
                {
                    "tag": "mixed",
                    "listen": "127.0.0.1",
                    "port": self.port,
                    "protocol": "mixed",
                    "settings": {"udp": False},
                }
            ],
            "outbounds": [outbound],
        }
        # The generated config carries the share link's credentials.
        _write_private(self.config.gateway_path, json.dumps(gateway, indent=2))
        self._rotate_log()
        fd = os.open(
            self.config.tunnel_log_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        log = os.fdopen(fd, "ab")
        try:
            process = subprocess.Popen(
                [binary, "run", "-c", str(self.config.gateway_path)],
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        finally:
            log.close()
        self._started_here = True
        self.config.pid_path.write_text(str(process.pid), encoding="utf-8")
        if self._stopping.is_set():
            self._stop()
            raise TunnelBusy("stop requested")
        timeout = max(1.0, min(self.config.tunnel.port_timeout, deadline - time.monotonic()))
        if not self._wait_port(timeout):
            # poll() reaps an exited child; without it the zombie answers
            # os.kill(pid, 0) and looks like a slow start forever.
            if process.poll() is None:
                # Slow start, not a bad link: keep it for the next request.
                raise TunnelBusy("xray is still starting; try again")
            self._stop()
            raise TunnelUnavailable("xray did not open its port; see tunnel.log")

    def _stop_sync(self) -> None:
        self._stopping.set()
        self._idle.clear()
        if not self._sync_lock.acquire(blocking=False):
            self._idle.wait(self.config.tunnel.stop_wait)
            if not self._sync_lock.acquire(timeout=0.1):
                # The rotation ignored the stop flag for too long. Kill the
                # process we know about rather than reporting a stop we did not
                # achieve. The flag stays set so that a rotation still alive
                # cannot start a new xray; only the next ensure() lifts it.
                self._stop()
                return
        try:
            self._stop()
        finally:
            self._sync_lock.release()
            self._idle.set()

    def _stop(self) -> None:
        self._started_here = False
        pid = self._pid()
        if pid is None:
            self.config.pid_path.unlink(missing_ok=True)
            self._healthy_until = 0.0
            return
        if self._is_ours(pid):
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except OSError:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            for _ in range(20):
                if not _pid_alive(pid):
                    break
                time.sleep(0.1)
        self.config.pid_path.unlink(missing_ok=True)
        self._healthy_until = 0.0

    # ---- probes ----------------------------------------------------------
    def _probe(self, deadline: float | None = None) -> tuple[str, float | None]:
        """Best latency over a few canary fetches through our proxy.

        Returns a status as well as a number. `EXPIRED` means an attempt was cut
        short by the caller's deadline, so a failure proves nothing; `FAILED`
        means the canary was refused or answered badly within a full attempt.
        """
        proxy = {"http": f"http://127.0.0.1:{self.port}", "https": f"http://127.0.0.1:{self.port}"}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxy))
        best = None
        for _ in range(max(1, self.config.tunnel.probe_attempts)):
            if self._stopping.is_set():
                return PROBE_EXPIRED, None
            timeout = float(SCAN_TIMEOUT)
            truncated = False
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0.2:
                    return (PROBE_OK, best) if best is not None else (PROBE_EXPIRED, None)
                if left < SCAN_TIMEOUT:
                    truncated = True
                    timeout = max(0.1, left)
            started = time.time()
            try:
                with opener.open(self.config.tunnel.canary, timeout=timeout) as response:
                    response.read(1024)
                    if response.status == 200:
                        best = time.time() - started if best is None else min(best, time.time() - started)
            except (TimeoutError, socket.timeout):
                if truncated:
                    return PROBE_EXPIRED, None
                continue
            except urllib.error.URLError as exc:
                if truncated and isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
                    return PROBE_EXPIRED, None
                continue
            except Exception:  # noqa: BLE001 - public configs reset; one try proves nothing
                continue
        if best is not None:
            return PROBE_OK, best
        if deadline is not None and deadline - time.monotonic() <= 0.2:
            return PROBE_EXPIRED, None
        return PROBE_FAILED, None

    # ---- pid / port ------------------------------------------------------
    def _pid(self) -> int | None:
        try:
            pid = int(self.config.pid_path.read_text().strip())
        except (FileNotFoundError, ValueError):
            return None
        return pid if _pid_alive(pid) else None

    def _is_ours(self, pid: int) -> bool:
        """Only kill a process we started, never another xray on the machine."""
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
            return str(self.config.gateway_path).encode() in cmdline
        except OSError:
            # No /proc (macOS/Windows): the pidfile is ours by contract, but a
            # stale pid could have been reused. Only trust it if we started it.
            return self._started_here

    def _running(self) -> bool:
        return self._pid() is not None and self._port_open()

    def _port_open(self) -> bool:
        with socket.socket() as sock:
            sock.settimeout(0.2)
            return sock.connect_ex(("127.0.0.1", self.port)) == 0

    def _wait_port(self, timeout: float = 5.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self._port_open():
                return True
            time.sleep(0.05)
        return False

    # ---- state -----------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            data = json.loads(self.config.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(self, state: dict) -> None:
        self.config.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.config.state_path.with_suffix(".tmp")
        _write_private(tmp, json.dumps(state, indent=2))
        os.replace(tmp, self.config.state_path)

    def _mark_current_dead(self, reason: str) -> None:
        last = self._load_state().get("last_good")
        if not last:
            return
        state = self._load_state()
        state.setdefault("dead", {})[last] = int(time.time() + self.config.tunnel.dead_cooldown)
        state["last_good_dead_reason"] = reason
        self._save_state(state)

    def _rotate_log(self) -> None:
        path = self.config.tunnel_log_path
        try:
            if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
                os.replace(path, path.with_suffix(".log.1"))
        except OSError:
            pass


def _write_private(path: Path, text: str) -> None:
    """Write a file only the owner can read.

    gateway.json and state.json carry proxy credentials; the token file being
    0600 is not enough if these are group/world readable.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
