"""Configuration: defaults, a TOML file, and nested env overrides.

Paths in the config file may be absolute, start with ``~``, or be relative to the
service home (``~/.config/websearch`` by default, ``WEBSEARCH_HOME`` overrides it).
Relative-to-home is the default so a test can relocate everything with one env var.
"""

from __future__ import annotations

import os
import secrets
import sys
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

ENV_PREFIX = "WEBSEARCH_"


def _home() -> Path:
    return Path(os.environ.get(f"{ENV_PREFIX}HOME") or "~/.config/websearch").expanduser()


def _path(value: str | None, home: Path) -> Path | None:
    if not value:
        return None
    text = os.path.expandvars(os.path.expanduser(str(value)))
    candidate = Path(text)
    return candidate if candidate.is_absolute() else home / candidate


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 7971
    token_file: str = "token"
    auto_token: bool = True


@dataclass(frozen=True)
class SearchConfig:
    default_count: int = 10
    max_count: int = 30
    default_lang: str = "auto"
    cache_ttl: float = 300.0
    backend_timeout: float = 10.0


@dataclass(frozen=True)
class BackendConfig:
    enabled: bool = True
    min_interval: float = 0.0
    max_concurrency: int = 1


@dataclass(frozen=True)
class EgressConfig:
    mode: str = "auto"  # auto | direct | tunnel
    proxy_port: int = 1081
    retry_transient: int = 1
    request_timeout: float = 18.0
    tunnel_reserve: float = 6.0
    learned_file: str = "blocked-domains.txt"
    connection_timeout: float = 5.0


@dataclass(frozen=True)
class TunnelConfig:
    binary: str = ""
    pool_file: str = "pool.txt"
    canary: str = "https://www.youtube.com/"
    auto_start: bool = True
    start_attempts: int = 5
    start_timeout: float = 60.0
    port_timeout: float = 5.0
    stop_wait: float = 10.0
    healthy_ttl: float = 60.0
    probe_attempts: int = 3
    dead_cooldown: int = 90


DEFAULTS: dict[str, Any] = {
    "server": {"host": "127.0.0.1", "port": 7971, "token_file": "token", "auto_token": True},
    "search": {
        "default_count": 10,
        "max_count": 30,
        "default_lang": "auto",
        "cache_ttl": 300.0,
        "backend_timeout": 10.0,
    },
    "backends": {
        "ddg": {"enabled": True, "min_interval": 2.0, "max_concurrency": 1},
        "mwmbl": {"enabled": True, "min_interval": 0.5, "max_concurrency": 1},
    },
    "egress": {
        "mode": "auto",
        "proxy_port": 1081,
        "retry_transient": 1,
        "request_timeout": 18.0,
        "tunnel_reserve": 6.0,
        "learned_file": "blocked-domains.txt",
        "connection_timeout": 5.0,
        "tunnel": {
            "binary": "",
            "pool_file": "pool.txt",
            "canary": "https://www.youtube.com/",
            "auto_start": True,
            "start_attempts": 5,
            "start_timeout": 60.0,
            "port_timeout": 5.0,
            "stop_wait": 10.0,
            "healthy_ttl": 60.0,
            "probe_attempts": 3,
            "dead_cooldown": 90,
        },
    },
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _coerce(raw: str, example: Any) -> Any:
    if isinstance(example, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(example, int) and not isinstance(example, bool):
        return int(raw)
    if isinstance(example, float):
        return float(raw)
    return raw


def env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """Turn WEBSEARCH_SECTION__KEY=value into nested overrides.

    Types are taken from the value already in the tree, so ``PORT=8000`` becomes an
    int while ``HOST`` stays a string. Unknown keys are ignored.
    """
    result: dict[str, Any] = {}
    for name, raw in os.environ.items():
        if not name.startswith(ENV_PREFIX) or name in (f"{ENV_PREFIX}HOME", f"{ENV_PREFIX}TOKEN"):
            continue
        parts = [p.lower() for p in name[len(ENV_PREFIX) :].split("__") if p]
        if not parts:
            continue
        cursor = data
        node = result
        ok = True
        for part in parts[:-1]:
            if not isinstance(cursor.get(part), dict):
                ok = False
                break
            cursor = cursor[part]
            node = node.setdefault(part, {})
        if not ok or parts[-1] not in cursor:
            continue
        example = cursor[parts[-1]]
        try:
            node[parts[-1]] = _coerce(raw, example)
        except ValueError:
            continue
    return result


@dataclass(frozen=True)
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    egress: EgressConfig = field(default_factory=EgressConfig)
    tunnel: TunnelConfig = field(default_factory=TunnelConfig)
    backends: dict[str, BackendConfig] = field(default_factory=dict)
    home: Path = field(default_factory=_home)
    source: Path | None = None

    # ---- paths -----------------------------------------------------------
    def resolve(self, value: str | None) -> Path | None:
        return _path(value, self.home)

    @property
    def token_path(self) -> Path:
        return self.resolve(self.server.token_file) or self.home / "token"

    @property
    def learned_path(self) -> Path:
        return self.resolve(self.egress.learned_file) or self.home / "blocked-domains.txt"

    @property
    def pool_path(self) -> Path:
        return self.resolve(self.tunnel.pool_file) or self.home / "pool.txt"

    @property
    def state_path(self) -> Path:
        return self.home / "state.json"

    @property
    def pid_path(self) -> Path:
        return self.home / "tunnel.pid"

    @property
    def gateway_path(self) -> Path:
        return self.home / "gateway.json"

    @property
    def tunnel_log_path(self) -> Path:
        return self.home / "tunnel.log"

    @property
    def bin_dir(self) -> Path:
        return self.home / "bin"

    # ---- secrets ---------------------------------------------------------
    def ensure_token(self) -> str:
        env = (os.environ.get(f"{ENV_PREFIX}TOKEN") or "").strip()
        if env:
            return env
        path = self.token_path
        if path.exists():
            token = path.read_text(encoding="utf-8").strip()
            if token:
                return token
        if not self.server.auto_token:
            raise RuntimeError(f"token missing: {path} (set WEBSEARCH_TOKEN or create the file)")
        token = secrets.token_urlsafe(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token + "\n", encoding="utf-8")
        path.chmod(0o600)
        return token

    # ---- loading ---------------------------------------------------------
    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        home = _home()
        source = path or (home / "config.toml")
        raw: dict[str, Any] = {}
        if source.exists():
            with source.open("rb") as fh:
                raw = tomllib.load(fh)
        base = _merge(DEFAULTS, raw)
        tree = _merge(base, env_overrides(base))  # env wins over the file
        tunnel_raw = dict(tree["egress"].get("tunnel") or {})
        egress_raw = {k: v for k, v in tree["egress"].items() if k != "tunnel"}
        backends = {
            name: _build(BackendConfig, cfg, f"backends.{name}")
            for name, cfg in (tree.get("backends") or {}).items()
        }
        return cls(
            server=_build(ServerConfig, tree["server"], "server"),
            search=_build(SearchConfig, tree["search"], "search"),
            egress=_build(EgressConfig, egress_raw, "egress"),
            tunnel=_build(TunnelConfig, tunnel_raw, "egress.tunnel"),
            backends=backends,
            home=home,
            source=source if source.exists() else None,
        )


def _build(cls, data: dict[str, Any], section: str):
    """Construct a config dataclass, warning about keys it does not know.

    A typo in TOML should not turn into an opaque restart loop under systemd, and
    silently dropping the key would hide the mistake; say the key name.
    """
    known = {item.name for item in fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        print(
            f"websearch: ignoring unknown config keys in [{section}]: {', '.join(unknown)}",
            file=sys.stderr,
        )
    return cls(**{key: value for key, value in data.items() if key in known})


DEFAULT_CONFIG_TOML = """\
# websearch configuration. Environment overrides: WEBSEARCH_SECTION__KEY=value
[server]
host = "127.0.0.1"
port = 7971

[search]
default_count = 10
max_count = 30
default_lang = "auto"      # auto | fa | en
cache_ttl = 300            # seconds
backend_timeout = 10

[backends.ddg]
enabled = true
min_interval = 2.0         # conservative: DDG bans fast automated use
max_concurrency = 1

[backends.mwmbl]
enabled = true
min_interval = 0.5

[egress]
mode = "auto"              # auto | direct | tunnel
proxy_port = 1081
request_timeout = 18       # total budget for one host across routes
tunnel_reserve = 6         # seconds kept for the tunnel attempt

[egress.tunnel]
# Leave binary empty to autodetect XRAY_BIN, PATH, v2rayN, the local cache,
# and finally download an official Xray release once.
binary = ""
pool_file = "pool.txt"     # one vless:// or trojan:// link per line
"""
