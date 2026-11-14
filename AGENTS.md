# AGENTS.md

Guidance for coding agents working in this repository.

## What this is

A local web-search service for agents. One Starlette app exposes MCP (`/mcp`) and REST
(`/search`); a fan-out service merges results from pluggable backends; an egress layer falls back
to a local xray tunnel when a domain needs it. The package lives in `src/websearch/`, the command
is `websearch`, the project is `agent-websearch`.

## Keep the two names separate

- **Project identity — `agent-websearch`:** repository, `pyproject.toml` name, User-Agent,
  config home `~/.config/agent-websearch`, systemd unit `agent-websearch.service`, env prefix
  `AGENT_WEBSEARCH_`.
- **Runtime identity — `websearch`:** the CLI command, the Python module (`src/websearch`), the
  entry-point group `websearch.backends`, and the MCP server name (agents see
  `websearch_search`).

Renaming one without the other breaks clients or scripts. Change both only with a migration.

## Non-negotiable behaviour

- Every response reports each backend (`status`, `count`, `error_code`, `detail`). Never return
  an empty list without a reason, and document every new error code in the README table.
- A block page or an API verdict is **not** a route failure. Backend detectors run before route
  handling: a CAPTCHA block page, and a keyed API's 401/402/403/429/5xx, are reported as backend
  errors instead of being retried or sent through the tunnel. A 5xx from a scraped page remains a
  transient route-level failure.
- Learn a domain only after a direct attempt failed **and** the same request succeeded through the
  tunnel.
- Keep a reserve in the request budget so the tunnel attempt is never starved
  (`egress.tunnel_reserve`).
- Bind loopback only; keep Bearer auth on every route except `/healthz`.
- Keep `trust_env=False` on HTTP clients: an ambient proxy must not silently reroute traffic.
- xray stays a dumb pipe — no domain rules in `gateway.json`. All routing policy lives in
  `egress/router.py`, next to the learned domain list.
- Backends never open sockets; they receive `fetch` from `Egress`.
- The service is a local singleton: cache, breaker and rate limiters are in memory. Do not add
  cross-process state without a reason.

## Changing things

- **New backend:** implement `name` and `async search(query, count, lang, fetch)`, register it in
  `[project.entry-points."websearch.backends"]`, enable it in `config.example.toml`. No core
  edits.
- **New config key:** add it to the dataclass and `DEFAULTS` in `config.py`, then to
  `config.example.toml`. Env overrides (`AGENT_WEBSEARCH_SECTION__KEY`) take their type from the
  default, so keep the default typed.
- **New error code:** classify it in the breaker (`core.py`) if it deserves an immediate cooldown,
  and add it to the README table.
- **Version:** bump `pyproject.toml` and the `MCPServer(..., version=...)` string in
  `server/app.py` together.

## Tests and checks

- `.venv/bin/python -m pytest -q` — the suite must stay offline (mocked transports and recorded
  fixtures). Do not add tests that call real backends.
- The DuckDuckGo HTML parser is the fragile part: record a fresh real page into
  `tests/fixtures/` before changing the parser.
- Exercise the HTTP surface for real: `websearch serve`, then `curl /healthz` and one `/search`
  with the token. `websearch doctor` checks config, token, xray, pool and key offline; `--live`
  adds an Mwmbl call and the tunnel canary.
- An editable install pins the checkout path; reinstall it after moving the repository.
- The service runs as a systemd user unit. After changing code it loads, restart it
  (`systemctl --user restart agent-websearch`) and check `/healthz`.

## Docs and releases

- The README is public-facing and in English. Keep it free of personal paths, machine-specific
  notes and references to private workspaces.
- Update the README and `config.example.toml` in the same change that alters setup, configuration,
  commands or behaviour.
- `README.md` and `LICENSE` ship inside the wheel via `pyproject.toml`; verify with `uv build`
  when packaging metadata moves.

## Git workflow

1. Commit coherent, verified changes.
2. **Push immediately after every commit:** `git push origin main`.
3. Never force-push; published history is append-only. Amend only before a commit is pushed.
4. `origin` has two push URLs — a private mirror and the public GitHub repository — so a plain
   `git push` publishes to both. If one host is unreachable, report it instead of retrying
   blindly.
5. Never commit secrets. The TinyFish key lives in `~/.config/agent-websearch/env`, not in the
   repository, tests, fixtures or reports.
