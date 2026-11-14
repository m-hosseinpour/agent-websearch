"""websearch command line: serve | search | setup | status | doctor."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from .backends.tinyfish import API_KEY_ENV
from .config import DEFAULT_CONFIG_TOML, Config
from .egress.tunnel import (
    Tunnel,
    TunnelUnavailable,
    configured_binary,
    ensure_binary,
    load_pool,
    parse_uri,
)
from .service import build_service

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _env_file_path() -> Path:
    home = Path(os.environ.get("WEBSEARCH_HOME") or "~/.config/websearch").expanduser()
    return home / "env"


def _load_env_file(path: Path) -> None:
    """Let the CLI see the same EnvironmentFile the systemd unit loads.

    Explicit environment variables win; the file fills only what is missing.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def _config(args: argparse.Namespace) -> Config:
    _load_env_file(_env_file_path())
    return Config.load(Path(args.config).expanduser() if getattr(args, "config", None) else None)


# ------------------------------------------------------------------ commands
def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .server.app import build_app

    config = _config(args)
    host = args.host or config.server.host
    if host not in LOOPBACK:
        raise SystemExit(
            f"refusing to bind {host}: this service is localhost-only "
            f"(allowed: {', '.join(sorted(LOOPBACK))})"
        )
    app = build_app(config)
    port = args.port or config.server.port
    uvicorn.run(app, host=host, port=port, log_level=args.log_level)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    config = _config(args)

    async def run():
        service = build_service(config)
        try:
            return await service.search(args.query, args.count, args.lang)
        finally:
            await service.close()

    response = asyncio.run(run())
    print(json.dumps(response.model_dump(), ensure_ascii=False, indent=2))
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    config = _config(args)
    config.home.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path = config.source or (config.home / "config.toml")
    if not config_path.exists():
        config_path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
        print(f"wrote config      : {config_path}")
    else:
        print(f"config exists     : {config_path}")

    token_path = config.token_path
    config.ensure_token()
    if token_path.exists():
        mode = oct(token_path.stat().st_mode & 0o777)
        print(f"token             : {token_path} (mode {mode})")
    else:
        print("token             : from WEBSEARCH_TOKEN (no file written)")

    pool_path = config.pool_path
    if args.seed_from:
        source = Path(args.seed_from).expanduser()
        if not source.exists():
            print(f"seed source missing: {source}", file=sys.stderr)
            return 2
        incoming = load_pool(source)
        existing = set(load_pool(pool_path))
        added = [uri for uri in incoming if uri not in existing]
        with pool_path.open("a", encoding="utf-8") as fh:
            if pool_path.stat().st_size == 0:
                fh.write("# one vless:// or trojan:// link per line; top of file tried first\n")
            for uri in added:
                fh.write(uri + "\n")
        print(f"pool              : {pool_path} (+{len(added)} links)")
    elif not pool_path.exists():
        pool_path.write_text(
            "# one vless:// or trojan:// link per line; top of file tried first\n",
            encoding="utf-8",
        )
        print(f"pool              : {pool_path} (empty - paste your links)")
    else:
        print(f"pool              : {pool_path} ({len(load_pool(pool_path))} links)")

    if pool_path.exists():  # share links are credentials
        pool_path.chmod(0o600)

    if load_pool(pool_path):
        binary = ensure_binary(config)
        print(f"xray              : {binary}")
    else:
        print("xray              : skipped (empty pool; fetched at first proxied request)")

    print()
    print("next:")
    print(f"  websearch serve                      # http://{config.server.host}:{config.server.port}")
    print(f"  websearch search \"query\" --count 5")
    print("  systemctl --user enable --now websearch   # after installing the unit")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = _config(args)
    tunnel = Tunnel(config)
    payload = {
        "config": str(config.source) if config.source else None,
        "home": str(config.home),
        "egress_mode": config.egress.mode,
        "tunnel": tunnel.health(),
        "learned_domains": _learned(config),
        "token": str(config.token_path) if config.token_path.exists() else None,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_tunnel(args: argparse.Namespace) -> int:
    config = _config(args)
    tunnel = Tunnel(config)
    if args.action == "up":
        asyncio.run(tunnel.ensure())
    elif args.action == "down":
        asyncio.run(tunnel.stop())
    health = tunnel.health()
    print(json.dumps(health, indent=2, ensure_ascii=False))
    if args.action == "down" and health["running"]:
        print("warning: tunnel is still running", file=sys.stderr)
        return 1
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = _config(args)
    failures: list[str] = []

    def line(level: str, text: str) -> None:
        print(f"{level:4s} {text}")
        if level == "FAIL":
            failures.append(text)

    line("PASS" if config.source else "WARN", f"config: {config.source or 'defaults (no config.toml)'}")

    token_path = config.token_path
    if token_path.exists():
        mode = token_path.stat().st_mode & 0o777
        line("PASS" if mode == 0o600 else "WARN", f"token: {token_path} (mode {oct(mode)})")
    else:
        line("WARN", f"token: missing, will be generated at {token_path}")

    env_path = config.home / "env"
    if env_path.exists():
        mode = env_path.stat().st_mode & 0o777
        line("PASS" if mode == 0o600 else "WARN", f"env: {env_path} (mode {oct(mode)})")

    binary = configured_binary(config)
    line("PASS" if binary else "WARN", f"xray: {binary or 'not found (downloaded at first proxied request)'}")

    pool = load_pool(config.pool_path)
    if not pool:
        line("WARN", f"pool: empty ({config.pool_path})")
    else:
        bad = 0
        for uri in pool:
            try:
                parse_uri(uri)
            except ValueError:
                bad += 1
        line("PASS" if bad == 0 else "WARN", f"pool: {len(pool)} links, {bad} unparseable")

    line("PASS" if config.learned_path.exists() else "WARN",
         f"learned: {len(_learned(config))} domains ({config.learned_path})")

    tinyfish = config.backends.get("tinyfish")
    if tinyfish is not None and tinyfish.enabled:
        key = (os.environ.get(API_KEY_ENV) or "").strip()
        line(
            "PASS" if key else "FAIL",
            f"tinyfish: {API_KEY_ENV} "
            + ("set" if key else "missing (put it in ~/.config/websearch/env)"),
        )

    if args.live:
        for level, text in _live_checks(config):
            line(level, text)
    else:
        line("SKIP", "live backend check (pass --live)")

    print()
    print("ok" if not failures else f"{len(failures)} failure(s)")
    return 0 if not failures else 1


def _live_checks(config: Config) -> list[tuple[str, str]]:
    import httpx

    results: list[tuple[str, str]] = []

    async def probe_mwmbl() -> int:
        async with httpx.AsyncClient(timeout=8, trust_env=False, follow_redirects=True) as client:
            response = await client.get(
                "https://api.mwmbl.org/api/v1/search/",
                params={"s": "test"},
                headers={"User-Agent": "agent-websearch/0.1"},
            )
            return response.status_code

    try:
        status = asyncio.run(probe_mwmbl())
        results.append(("PASS" if status == 200 else "FAIL", f"mwmbl: HTTP {status}"))
    except Exception as exc:  # noqa: BLE001
        results.append(("FAIL", f"mwmbl: {type(exc).__name__}: {exc}"))

    # The tunnel is the only working route to DDG from this network, so prove it
    # opens the canary rather than only reporting that a pidfile exists.
    tunnel = Tunnel(config)
    if not load_pool(config.pool_path):
        results.append(("WARN", "tunnel: pool empty, cannot verify a route"))
        return results
    try:
        asyncio.run(tunnel.ensure(deadline=time.monotonic() + config.tunnel.start_timeout))
    except TunnelUnavailable as exc:
        results.append(("FAIL", f"tunnel: {exc}"))
        return results
    health = tunnel.health()
    results.append(
        ("PASS" if health["running"] else "FAIL", f"tunnel: running on port {health['port']}")
    )
    return results


def _learned(config: Config) -> list[str]:
    path = config.learned_path
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


# -------------------------------------------------------------------- parser
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="websearch", description=__doc__)
    parser.add_argument("--config", help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the HTTP/MCP server")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--log-level", default="info")
    serve.set_defaults(func=cmd_serve)

    search = sub.add_parser("search", help="run one search and print JSON")
    search.add_argument("query")
    search.add_argument("--count", type=int)
    search.add_argument("--lang")
    search.set_defaults(func=cmd_search)

    setup = sub.add_parser("setup", help="create config/token/pool and fetch xray")
    setup.add_argument("--seed-from", help="copy share links from another pool file")
    setup.set_defaults(func=cmd_setup)

    status = sub.add_parser("status", help="tunnel, pool and learned-domain status")
    status.set_defaults(func=cmd_status)

    tunnel = sub.add_parser("tunnel", help="start, stop or inspect the local xray pipe")
    tunnel.add_argument("action", choices=["up", "down", "status"])
    tunnel.set_defaults(func=cmd_tunnel)

    doctor = sub.add_parser("doctor", help="check config, token, xray, pool, backends")
    doctor.add_argument("--live", action="store_true", help="also call Mwmbl")
    doctor.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
