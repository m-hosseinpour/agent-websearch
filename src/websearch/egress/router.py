"""Egress policy: which route a domain takes, and what a failure means.

xray is only a pipe here — its inbound has no routing rules. Every decision lives
in this module, because this is also where the learned domain list lives.
"""

from __future__ import annotations

import asyncio
import os
import time
import urllib.parse
from pathlib import Path

import httpx

from ..backends.base import BackendError
from ..config import Config
from .tunnel import Tunnel, TunnelBusy, TunnelUnavailable

TRANSIENT_STATUS = {500, 502, 503, 504}
ROUTE_DENIED_STATUS = {403, 451}


class LearnedDomains:
    """Hosts proven to need the tunnel, persisted one per line.

    A domain is added only after a direct attempt failed *and* the same request
    succeeded through the tunnel. Adding on failure alone would poison the list
    with hosts that are simply down (the tunnel does not fix a 404 or a sanction
    block — see research/services-catalog.md, Exa at 403 both ways).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._domains: set[str] = set()
        self._lock = asyncio.Lock()
        self.reload()

    def reload(self) -> None:
        domains: set[str] = set()
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            # Unreadable/missing: start empty rather than failing service startup.
            self._domains = domains
            return
        for line in text.splitlines():
            line = line.strip().lower()
            if not line or line.startswith("#"):
                continue
            domains.add(line.lstrip("."))
        self._domains = domains

    def matches(self, host: str) -> bool:
        host = (host or "").lower().strip(".")
        return any(host == d or host.endswith("." + d) for d in self._domains)

    def __len__(self) -> int:
        return len(self._domains)

    def entries(self) -> list[str]:
        return sorted(self._domains)

    async def add(self, host: str, reason: str) -> bool:
        host = (host or "").lower().strip(".")
        if not host:
            return False
        async with self._lock:
            if self.matches(host):
                return False
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            existing = ""
            try:
                if self.path.exists():
                    existing = self.path.read_text(encoding="utf-8")
            except OSError:
                return False
            prefix = ""
            if not existing:
                prefix = (
                    "# auto-learned domains; always fetched through the tunnel\n"
                    "# safe to edit: one domain per line, # starts a comment\n"
                )
            elif not existing.endswith("\n"):
                # A hand-edited file without a final newline would otherwise glue
                # the new comment onto the last domain and lose it on reload.
                prefix = "\n"
            try:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(f"{prefix}# {stamp} {host} reason={reason}\n{host}\n")
                os.chmod(self.path, 0o600)
            except OSError:
                # Learning is an optimisation; keep memory and file consistent.
                return False
            self._domains.add(host)
            return True


class Egress:
    def __init__(
        self,
        config: Config,
        tunnel: Tunnel | None,
        transport: httpx.AsyncBaseTransport | None = None,
        client_factory=None,
    ) -> None:
        self.config = config
        self.tunnel = tunnel
        self.learned = LearnedDomains(config.learned_path)
        self._transport = transport
        self._client_factory = client_factory
        self._clients: dict[bool, httpx.AsyncClient] = {}

    # ------------------------------------------------------------------ setup
    def _client(self, via_tunnel: bool) -> httpx.AsyncClient:
        client = self._clients.get(via_tunnel)
        if client is not None:
            return client
        if self._client_factory is not None:  # tests inject route-aware clients
            client = self._client_factory(via_tunnel)
            self._clients[via_tunnel] = client
            return client
        kwargs: dict = {
            "trust_env": False,  # ambient HTTPS_PROXY must not silently reroute us
            "timeout": httpx.Timeout(
                connect=self.config.egress.connection_timeout,
                read=self.config.search.backend_timeout,
                write=self.config.egress.connection_timeout,
                pool=self.config.egress.connection_timeout,
            ),
            "follow_redirects": True,
        }
        if via_tunnel:
            kwargs["proxy"] = f"http://127.0.0.1:{self.config.egress.proxy_port}"
        if self._transport is not None:
            kwargs["transport"] = self._transport
        client = httpx.AsyncClient(**kwargs)
        self._clients[via_tunnel] = client
        return client

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    # ---------------------------------------------------------------- routing
    def routes_for(self, host: str) -> list[bool]:
        """Ordered list of routes to try; ``True`` means through the tunnel."""
        mode = self.config.egress.mode
        if mode == "direct":
            return [False]
        if mode == "tunnel":
            return [True]
        if self.learned.matches(host):
            return [True]
        return [False, True]

    async def fetch(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        block_detector=None,
    ) -> httpx.Response:
        host = urllib.parse.urlparse(url).hostname or ""
        routes = self.routes_for(host)
        deadline = time.monotonic() + self.config.egress.request_timeout
        last: Exception | None = None
        denied = False
        tried_direct = not routes[0]

        for index, via_tunnel in enumerate(routes):
            routes_left = len(routes) - index - 1
            attempts = 1 + (self.config.egress.retry_transient if not via_tunnel else 0)
            for _ in range(attempts):
                remaining = deadline - time.monotonic()
                if remaining <= 0.2:
                    raise _error("timeout", "request budget exhausted") from last
                if via_tunnel:
                    if self.tunnel is None:
                        raise _error("proxy_unavailable", "tunnel is not configured")
                    try:
                        await self.tunnel.ensure(deadline=deadline)
                    except TunnelBusy as exc:
                        raise _error("proxy_busy", str(exc)) from exc
                    except TunnelUnavailable as exc:
                        raise _error("proxy_unavailable", str(exc)) from exc
                try:
                    budget = self._attempt_timeout(
                        max(0.5, deadline - time.monotonic()), routes_left
                    )
                    response = await self._client(via_tunnel).get(
                        url, headers=headers, timeout=budget
                    )
                except httpx.TimeoutException as exc:
                    last = exc
                    continue
                except httpx.HTTPError as exc:
                    last = exc
                    continue

                if block_detector is not None:
                    # A block page is a backend verdict, not a route problem:
                    # the same exit IP answered both ways (observed with DDG).
                    code = block_detector(response.status_code, response.content, response.headers)
                    if code:
                        # A block page is the engine's verdict; an API status is
                        # the service's verdict. Keep the detail honest for both.
                        detail = (
                            "block page from the backend"
                            if code == "captcha"
                            else f"HTTP {response.status_code} from the backend"
                        )
                        raise _error(code, detail)

                if response.status_code in ROUTE_DENIED_STATUS:
                    denied = True
                    last = httpx.HTTPStatusError(
                        f"HTTP {response.status_code}", request=response.request, response=response
                    )
                    break  # no retry on the same route; try the other one

                if response.status_code in TRANSIENT_STATUS:
                    last = httpx.HTTPStatusError(
                        f"HTTP {response.status_code}", request=response.request, response=response
                    )
                    continue

                if response.status_code == 429:
                    raise _error("rate_limited", f"HTTP 429 from {host}")

                if response.status_code >= 400:
                    raise _error("http_error", f"HTTP {response.status_code} from {host}")

                if via_tunnel and tried_direct:
                    reason = "denied_direct" if denied else "unreachable_direct"
                    try:
                        await self.learned.add(host, reason)
                    except OSError:
                        # Learning is an optimisation; never fail a good response
                        # because the list could not be written.
                        pass
                return response

            if via_tunnel:
                # One failed tunnel request may be a dead public config, not a
                # dead route: ask the tunnel to re-probe/rotate before giving up.
                if time.monotonic() + 2 < deadline:
                    try:
                        await self.tunnel.ensure(force=True, deadline=deadline)
                    except TunnelUnavailable:
                        pass

        code = "blocked" if denied else _classify(last)
        raise _error(code, str(last) if last else "no route answered")

    def _attempt_timeout(self, remaining: float, routes_left: int) -> float:
        """Budget one attempt, reserving time for a tunnel attempt that follows.

        Handing the whole remaining budget to the first attempt is the bug this
        guards: a hung direct connection would consume it and the tunnel fallback
        required by the design would never run.
        """
        reserve = self.config.egress.tunnel_reserve if routes_left else 0.0
        return max(0.5, min(self.config.search.backend_timeout, remaining - reserve))


def _classify(exc: Exception | None) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        return "unreachable"
    if isinstance(exc, httpx.HTTPStatusError):
        return "http_error"
    return "unreachable"


def _error(code: str, detail: str) -> BackendError:
    return BackendError(code, detail)
