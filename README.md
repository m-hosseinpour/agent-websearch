# agent-websearch

A local web-search service for AI agents. One endpoint, several search backends, and a response
that always says what each backend did — including when it failed and why.

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](#requirements)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

- **MCP:** `POST /mcp` (streamable HTTP, stateless) exposing one tool: `search`
- **REST:** `GET|POST /search` for curl and scripts
- **Health:** `GET /healthz` (no token)
- MCP and REST return the same model, so an agent and a shell command see identical data.

**Contents:** [Features](#features) · [Requirements](#requirements) · [Quick start](#quick-start) ·
[Configuration](#configuration) · [Usage](#usage) · [How it works](#how-it-works) ·
[Troubleshooting](#troubleshooting) · [Limitations](#limitations) · [Development](#development) ·
[License](#license)

## Features

- **Failures are part of the contract.** Every response carries `backends[]` with `status`,
  `count`, `latency_ms`, `error_code` and a human-readable `detail`. An empty result list is
  never the only answer.
- **Pluggable backends.** A backend is a class with a `name` and a `search()` coroutine,
  registered through an entry point. Adding one does not touch the core.
- **Adaptive egress.** Requests try the direct route first; a domain that fails directly and
  then succeeds through the bundled tunnel is remembered and routed through the tunnel from
  then on. Works on networks where some engines are unreachable or answer with a CAPTCHA.
- **Free to run.** Keyless backends are supported, with a free-tier keyed API as the primary
  source.
- **Safe defaults.** Loopback-only binding, a Bearer token on every route except `/healthz`,
  and credentials stored with mode `0600`.
- **Local and stateless.** In-memory cache, per-backend circuit breaker and rate limiter; no
  database, no daemon dependency other than the optional xray process it manages itself.

## Requirements

- **Python 3.11 or newer**
- **[uv](https://docs.astral.sh/uv/)** (recommended) or pip
- **xray** *(optional)* — discovered on `PATH`, in a v2rayN installation, or in the local cache;
  if none is found it is downloaded from the official release on the first proxied request and
  verified against the release `.dgst` SHA-256. Point at an existing binary with
  `AGENT_WEBSEARCH_XRAY_BIN` or `XRAY_BIN`.
- **TinyFish API key** *(recommended)* — free, no credit card:
  <https://agent.tinyfish.ai/api-keys>

## Quick start

```bash
git clone https://github.com/m-hosseinpour/agent-websearch.git
cd agent-websearch
uv tool install .
websearch --help            # the websearch command should now be on PATH
```

If `~/.local/bin` is not on `PATH`, run `uv tool update-shell` and open a new shell.

**1. Create the service home** (`~/.config/agent-websearch/`):

```bash
websearch setup
```

This writes `config.toml`, a `0600` `token` file, and an empty `pool.txt`.

**2. Add the TinyFish key** and enable the backend:

```bash
printf 'TINYFISH_API_KEY=%s\n' 'sk-tinyfish-...' > ~/.config/agent-websearch/env
chmod 600 ~/.config/agent-websearch/env
```

```toml
# ~/.config/agent-websearch/config.toml
[backends.tinyfish]
enabled = true
```

**3. Decide on DuckDuckGo.** `setup` enables it by default, but it needs a working tunnel pool in
networks where DuckDuckGo gates automated traffic. Without a pool, turn it off:

```toml
[backends.ddg]
enabled = false
```

If you have `vless://` or `trojan://` links, put them one per line in
`~/.config/agent-websearch/pool.txt` and leave DDG enabled.

**4. Run and verify:**

```bash
websearch serve             # http://127.0.0.1:7971
curl -s http://127.0.0.1:7971/healthz
TOKEN=$(cat ~/.config/agent-websearch/token)
curl -s "http://127.0.0.1:7971/search?q=python+asyncio&count=3" \
  -H "Authorization: Bearer $TOKEN"
websearch doctor --live     # token, xray, pool, key and tunnel canary
```

`/healthz` answers without a token; every other route returns `401` until the Bearer header is
present.

**5. Install as a user service** (Linux, systemd):

```bash
mkdir -p ~/.config/systemd/user
cp deploy/agent-websearch.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent-websearch
```

If you installed with a virtualenv instead of `uv tool`, change `ExecStart` in the unit to
`%h/path/to/agent-websearch/.venv/bin/websearch serve`.

## Configuration

Settings resolve in this order: built-in defaults < `config.toml` < environment variables.
Environment overrides use the pattern `AGENT_WEBSEARCH_<SECTION>__<KEY>`:

```bash
AGENT_WEBSEARCH_SERVER__PORT=8000
AGENT_WEBSEARCH_SEARCH__CACHE_TTL=0
AGENT_WEBSEARCH_EGRESS__MODE=tunnel
AGENT_WEBSEARCH_TOKEN=...            # instead of the token file
AGENT_WEBSEARCH_HOME=/path/to/home   # all relative paths resolve under this
```

`config.example.toml` documents every key. The main groups:

| Section | What it controls |
|---|---|
| `[server]` | Bind address, port, token file, automatic token creation |
| `[search]` | Default and maximum `count`, `lang`, cache TTL, per-backend timeout |
| `[backends.<name>]` | Enablement, minimum interval between calls, concurrency, hourly cap |
| `[egress]` | Mode (`auto` / `direct` / `tunnel`), local proxy port, retry and time budgets |
| `[egress.tunnel]` | xray binary, pool file, canary URL, rotation and probe timings |

### Files in the service home

| File | Purpose |
|---|---|
| `config.toml` | Configuration |
| `token` | Bearer token, created automatically, mode `0600` |
| `env` | Optional `TINYFISH_API_KEY=...` file read by the CLI and the systemd unit |
| `pool.txt` | One `vless://` or `trojan://` link per line; the first healthy one wins |
| `blocked-domains.txt` | Domains that must always use the tunnel; learned automatically, editable by hand |
| `state.json` | Last good proxy config and per-link cooldowns |
| `bin/` | Downloaded xray releases, when no binary was found on the system |
| `gateway.json` | Generated xray configuration (contains proxy credentials) |
| `tunnel.log` | xray output, rotated at 2 MB |

Every file that holds credentials is created with mode `0600`; the home directory is `0700`.

### Backends

| Name | Credentials | Notes |
|---|---|---|
| `tinyfish` | `TINYFISH_API_KEY` | Primary. Free tier: 30 req/min, 500/hour, 12,000/day. Google-backed index, `language`/`location` support, first page only (10 results). |
| `mwmbl` | none | Small, English-heavy independent index. Useful as a fallback, rarely useful for non-English queries. |
| `ddg` | none | Scrapes the DuckDuckGo HTML endpoint. Keyless but CAPTCHA-prone under automated use; keep `min_interval = 2.0` and a concurrency of 1, or leave it disabled. |

Results are merged in priority order (`tinyfish` > `ddg` > `mwmbl`), de-duplicated by a canonical
URL that ignores scheme, `www.`, fragments and tracking parameters, then trimmed to `count`.

### Egress modes

| Mode | Behaviour |
|---|---|
| `auto` *(default)* | Learned domain → tunnel only. Anything else → direct, then tunnel once if the direct attempt fails with a route-level error. |
| `direct` | Never use the tunnel; the xray process is not started at all. |
| `tunnel` | Always use the tunnel. |

A domain is added to `blocked-domains.txt` only after a direct attempt failed **and** the same
request succeeded through the tunnel. Failures alone are not learned: a host that is simply down
would otherwise be routed through the tunnel forever. Editing the list by hand requires a service
restart.

## Usage

### MCP

The server speaks streamable HTTP, stateless, at `/mcp`; the MCP server name is `websearch`, so
clients that prefix tool names show it as `websearch_search`. Tool parameters:

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `query` | string | required | Search terms |
| `count` | integer | 10 | Capped by `search.max_count` (30) |
| `lang` | string | `auto` | `auto` detects Persian script, else English; also accepts `fa`, `en` or a regional code such as `de-de` |

Client configuration details differ; the URL and the header are what matter. OpenCode example:

```jsonc
{
  "mcp": {
    "servers": {
      "websearch": {
        "type": "remote",
        "oauth": false,
        "url": "http://127.0.0.1:7971/mcp",
        "headers": { "Authorization": "Bearer <token from ~/.config/agent-websearch/token>" }
      }
    }
  }
}
```

`oauth: false` matters for clients that would otherwise attempt OAuth against a remote server.

### REST

```bash
GET  /search?q=...&count=5&lang=fa
POST /search   {"query": "...", "count": 5, "lang": "fa"}
```

```bash
TOKEN=$(cat ~/.config/agent-websearch/token)
curl -s "http://127.0.0.1:7971/search?q=%D8%AA%D8%AD%D9%82%DB%8C%D9%82&count=5" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "query": "تحقیق", "lang": "fa", "count": 5, "partial": false, "cached": false,
  "error_code": null,
  "results": [
    {"title": "…", "url": "https://…", "snippet": "…", "engine": "tinyfish"}
  ],
  "backends": [
    {"name": "tinyfish", "status": "ok", "count": 10, "latency_ms": 1050,
     "error_code": null, "detail": null},
    {"name": "ddg", "status": "skipped", "count": 0, "latency_ms": 0,
     "error_code": null, "detail": "disabled in config"},
    {"name": "mwmbl", "status": "ok", "count": 1, "latency_ms": 700,
     "error_code": null, "detail": null}
  ]
}
```

Status codes: `200` when at least one backend was `ok` (even if partial), `502` when none was,
`400` for a missing or malformed query, `401` for a missing or wrong token. A backend disabled in
configuration appears as `skipped` and does not make the response partial.

### CLI

| Command | Purpose |
|---|---|
| `websearch serve [--host H] [--port P]` | Run the HTTP/MCP server (loopback addresses only) |
| `websearch search "query" [--count N] [--lang L]` | Run one search and print JSON |
| `websearch setup [--seed-from PATH]` | Create the service home, token and pool; fetch xray when a pool exists |
| `websearch status` | Configuration, home, egress mode, tunnel health, learned domains |
| `websearch tunnel up\|down\|status` | Start, stop or inspect the xray pipe |
| `websearch doctor [--live]` | Check token, xray, pool, key and file modes; `--live` also calls Mwmbl and the tunnel canary |

### Error codes

| Code | Meaning |
|---|---|
| `captcha` | The engine answered with a CAPTCHA or block page |
| `blocked` | Access denied (for example HTTP 403 both directly and through the tunnel) |
| `timeout` | The request budget ran out |
| `unreachable` | Connection, DNS or reset failure |
| `http_error` | An unexpected HTTP status |
| `parse_error` | The response did not match the expected shape |
| `unauthorized` | A backend key is missing or invalid |
| `quota` | The backend's free allowance is exhausted |
| `rate_limited` | Local limiter or the upstream asked us to slow down |
| `circuit_open` | The backend's circuit breaker is cooling down |
| `proxy_unavailable` | The tunnel is not configured, the pool is empty, or no link works |
| `proxy_busy` | A tunnel rotation is in progress |
| `all_backends_failed` | Every backend failed; see each entry for the reason |
| `no_backends_enabled` | No backend is enabled in configuration |

Circuit breaker: `captcha`, `blocked` and `unauthorized` open it for 10 minutes, `quota` for one
hour, `proxy_unavailable` for 2 minutes, `rate_limited` for 1 minute; two consecutive generic
failures open it for 1 minute.

## How it works

```
client (agent or curl)
        │  Bearer token (every route except /healthz)
        ▼
   Starlette app ── /mcp · /search · /healthz
        │
        ▼
    SearchService ── cache · circuit breaker · rate limiter · priority merge
        │
        ▼
  tinyfish     ddg     mwmbl          plugins, loaded from entry points
        │
        ▼
      Egress ──► direct ─────────────────────────► internet
        └─────► xray (mixed, 127.0.0.1:1081) ────► internet
```

Three decisions worth knowing:

- **A block page is not a route problem.** If DuckDuckGo answers with a CAPTCHA, switching
  routes cannot help — the same exit address gets the same answer. The router reports the
  backend error instead of burning a tunnel attempt, and the breaker protects the quota.
- **The time budget is split, not shared greedily.** A request has a total budget (18 s by
  default); each attempt gets at most `backend_timeout` and 6 s are reserved so the tunnel
  fallback is never starved by a hung direct connection.
- **The tunnel is a pipe, not a router.** The generated xray configuration has one inbound and
  one outbound and no domain rules. All routing policy lives in the application, next to the
  learned domain list. Rotation means restarting xray with another link from the pool and
  probing the canary URL through it.

## Troubleshooting

| Symptom | Meaning | Action |
|---|---|---|
| `ddg: error_code=captcha` | DuckDuckGo gated automated traffic | Keep it disabled, or enable it only with a working pool; never exceed one concurrent request and 2 s between calls |
| `tinyfish: error_code=unauthorized` | Key missing or invalid | Check `~/.config/agent-websearch/env` and restart |
| `tinyfish: error_code=quota` | Daily allowance exhausted (resets 00:00 UTC) | Wait, or rely on the other backends |
| `proxy_unavailable` | Empty or dead pool | Refresh `pool.txt`, then `websearch tunnel up` |
| `proxy_busy` | A rotation is still running | Retry shortly |
| `circuit_open` | Breaker cooling down | Wait for the reported number of seconds |
| Results empty but `status: ok` | The engine found nothing (common for Mwmbl on non-English queries) | Add or enable another backend |
| Tunnel still used for a domain you fixed | The learned list is sticky | Remove the line from `blocked-domains.txt` and restart |

## Limitations

- **TinyFish** returns the first result page (10 hits), so `count` above 10 is capped by that
  backend. News results carry publisher and date instead of a snippet; those are substituted.
  Free-tier limits may change, and the key must be rotated in `~/.config/agent-websearch/env`.
- **Mwmbl** has a small, English-heavy index; for many queries it legitimately returns zero hits
  with `status: ok`.
- **DuckDuckGo** automates a page meant for humans; CAPTCHAs are a real risk. Automated use is
  the operator's responsibility.
- **Public proxy links** reset at random; rotation exists for that, but if the whole pool is dead
  the service reports `proxy_unavailable` until fresh links are added.
- **The tunnel solves network filtering, not legal blocks.** A domain returning 403 through every
  route is reported as `blocked` and is not learned.
- **Search only.** Page fetching, browser rendering and PDF extraction are intentionally out of
  scope.
- The service binds to loopback by design; exposing it to a network requires a reverse proxy and
  is not a supported configuration.

## Development

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e .
uv pip install --python .venv/bin/python pytest pytest-asyncio
.venv/bin/python -m pytest -q
```

The suite runs offline against recorded fixtures; the DuckDuckGo HTML parser has the most
coverage because it is the most fragile part. If DuckDuckGo changes its markup, record a fresh
result page first, then adjust the parser.

An editable install records the checkout path, so reinstall after moving the directory
(`uv pip install --python .venv/bin/python -e .`).

Adding a backend takes three steps: write a class with `name` and
`search(query, count, lang, fetch)`, register it under
`[project.entry-points."websearch.backends"]`, and enable it in configuration. The backend never
opens sockets itself; it receives `fetch` from the egress layer so routing stays in one place.

## Security notes

- The server refuses to bind to anything other than `127.0.0.1`, `localhost` or `::1`.
- Routes other than `/healthz` require `Authorization: Bearer <token>`; comparison is
  constant-time. A missing token file is generated on first start.
- Credential-bearing files (`token`, `env`, `pool.txt`, `state.json`, `gateway.json`,
  `tunnel.log`) are written with mode `0600`; the systemd unit sets `UMask=0077`.
- HTTP clients set `trust_env=False`, so an ambient `HTTPS_PROXY` cannot silently reroute
  traffic.
- API keys are read from the environment file at request time and are never written to logs or
  responses.

## License

[MIT](LICENSE)
