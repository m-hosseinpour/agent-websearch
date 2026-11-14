"""Search orchestration: cache, breaker, rate limit, fan-out, merge.

Everything here is in-memory on purpose. The service is a local singleton; a
disk cache would add stale-data debugging for no user-visible gain.
"""

from __future__ import annotations

import asyncio
import time
import urllib.parse
from collections import deque

from .backends.base import Backend, BackendError
from .config import BackendConfig, Config
from .egress.router import Egress
from .egress.tunnel import Tunnel
from .models import BackendReport, Hit, SearchResponse

PRIORITY = {"tinyfish": 0, "ddg": 1, "mwmbl": 2}
IMMEDIATE_OPEN = {
    "captcha": 600.0,
    "blocked": 600.0,
    "proxy_unavailable": 120.0,
    "rate_limited": 60.0,
    "unauthorized": 600.0,
    "quota": 3600.0,
}
FAILURE_THRESHOLD = 2
FAILURE_COOLDOWN = 60.0


def canonical_key(url: str) -> str:
    """Comparison key that ignores scheme, ``www.``, fragments and trackers."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        # A malformed URL in one result must not fail the whole merge.
        return url
    if host.startswith("www."):
        host = host[4:]
    params = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
        and k.lower() not in {"fbclid", "gclid", "ref", "ref_src"}
    ]
    query = urllib.parse.urlencode(sorted(params))
    path = parts.path.rstrip("/") or "/"
    return f"{host}{path}?{query}"


def merge_hits(per_backend: list[tuple[str, list[Hit]]], count: int) -> list[Hit]:
    """Concatenate in priority order and drop duplicates, keeping the first."""
    merged: list[Hit] = []
    seen: set[str] = set()
    for _, hits in per_backend:
        for hit in hits:
            key = canonical_key(hit.url)
            if key in seen:
                continue
            seen.add(key)
            merged.append(hit)
            if len(merged) >= count:
                return merged
    return merged


class Breaker:
    def __init__(self) -> None:
        self._open_until: dict[str, float] = {}
        self._failures: dict[str, int] = {}

    def check(self, name: str) -> None:
        until = self._open_until.get(name, 0.0)
        if until > time.time():
            raise BackendError("circuit_open", f"cooling down for {int(until - time.time())}s")

    def record(self, name: str, error: BackendError | None) -> None:
        if error is None:
            self._failures.pop(name, None)
            self._open_until.pop(name, None)
            return
        if error.code in ("circuit_open", "proxy_busy"):
            # Neither is new evidence about the backend: the breaker rejected us,
            # or a rotation was still running. Counting them would shrink a
            # 10-minute captcha cooldown or punish a healthy backend.
            return
        now = time.time()
        if error.code in IMMEDIATE_OPEN:
            self._open_until[name] = max(
                self._open_until.get(name, 0.0), now + IMMEDIATE_OPEN[error.code]
            )
            self._failures[name] = 0
            return
        failures = self._failures.get(name, 0) + 1
        self._failures[name] = failures
        if failures >= FAILURE_THRESHOLD:
            self._open_until[name] = max(
                self._open_until.get(name, 0.0), now + FAILURE_COOLDOWN
            )
            self._failures[name] = 0

    def state(self) -> dict[str, str]:
        return {
            name: f"open {int(self.open_seconds(name))}s" if self.open_seconds(name) > 0 else "closed"
            for name in self._open_until
        }

    def open_seconds(self, name: str) -> float:
        return max(0.0, self._open_until.get(name, 0.0) - time.time())


class Limiter:
    """Enforce a minimum gap and a concurrency cap per backend.

    A request that would wait longer than ``max_wait`` fails fast instead of
    silently eating the caller's whole timeout budget.
    """

    def __init__(
        self,
        min_interval: float,
        concurrency: int,
        max_wait: float = 5.0,
        max_per_hour: int = 0,
        window: float = 3600.0,
    ) -> None:
        self.min_interval = max(0.0, min_interval)
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._lock = asyncio.Lock()
        self._last = 0.0
        self.max_wait = max_wait
        self.max_per_hour = max(0, max_per_hour)
        self.window = window
        self._hourly: deque[float] = deque()

    async def __aenter__(self) -> "Limiter":
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self.max_wait)
        except asyncio.TimeoutError as exc:
            raise BackendError("rate_limited", "too many in-flight requests") from exc
        try:
            async with self._lock:
                now = time.monotonic()
                if self.max_per_hour:
                    while self._hourly and now - self._hourly[0] >= self.window:
                        self._hourly.popleft()
                    if len(self._hourly) >= self.max_per_hour:
                        retry = max(1, int(self.window - (now - self._hourly[0])) + 1)
                        raise BackendError(
                            "rate_limited",
                            f"hourly cap of {self.max_per_hour} reached; retry in {retry}s",
                        )
                wait = self._last + self.min_interval - now
                if wait > self.max_wait:
                    raise BackendError("rate_limited", f"next slot in {wait:.1f}s")
                if wait > 0:
                    await asyncio.sleep(wait)
                    now = time.monotonic()
                self._last = now
                if self.max_per_hour:
                    self._hourly.append(now)
        except BaseException:
            self._semaphore.release()
            raise
        return self

    async def __aexit__(self, *exc_info) -> None:
        self._semaphore.release()


class SearchService:
    def __init__(
        self,
        config: Config,
        backends: dict[str, Backend],
        egress: Egress,
        tunnel: Tunnel | None,
    ) -> None:
        self.config = config
        self.backends = backends
        self.egress = egress
        self.tunnel = tunnel
        self.breaker = Breaker()
        self._cache: dict[tuple[str, int, str], tuple[float, SearchResponse]] = {}
        self._cache_lock = asyncio.Lock()
        self._limiters = {
            name: Limiter(
                cfg.min_interval,
                cfg.max_concurrency,
                max_per_hour=cfg.max_per_hour,
            )
            for name in backends
            for cfg in [self.config.backends.get(name) or BackendConfig()]
            if cfg.enabled
        }

    # ------------------------------------------------------------------ api
    def enabled(self) -> list[str]:
        # Disabled backends still appear in the report (status=skipped) so the
        # output always explains why a source contributed nothing.
        names = list(self.backends)
        return sorted(names, key=lambda n: (PRIORITY.get(n, 99), n))

    async def search(
        self, query: str, count: int | None = None, lang: str | None = None
    ) -> SearchResponse:
        normalized = " ".join((query or "").split())
        if not normalized:
            raise ValueError("query is empty")
        requested = count if count is not None else self.config.search.default_count
        requested = max(1, min(int(requested), self.config.search.max_count))
        resolved_lang = self._resolve_lang(normalized, lang)

        key = (normalized.casefold(), requested, resolved_lang)
        cached = await self._cache_get(key, normalized)
        if cached is not None:
            return cached

        names = self.enabled()
        tasks = [self._run_backend(name, normalized, requested, resolved_lang) for name in names]
        outcomes = await asyncio.gather(*tasks) if tasks else []

        reports = [report for report, _ in outcomes]
        merged = merge_hits([(report.name, hits) for report, hits in outcomes], requested)
        response = SearchResponse(
            query=normalized,
            lang=resolved_lang,
            count=requested,
            results=merged,
            backends=reports,
            partial=any(report.status == "error" for report in reports),
            cached=False,
        )
        if response.failed():
            response.error_code = "no_backends_enabled" if not self._limiters else "all_backends_failed"
            response.partial = True
        await self._cache_put(key, response)
        return response

    async def close(self) -> None:
        await self.egress.aclose()
        if self.tunnel is not None:
            await self.tunnel.aclose()

    def health(self) -> dict:
        return {
            "ok": True,
            "backends": {
                name: {
                    "enabled": name in self._limiters,
                    "circuit": self.breaker.state().get(name, "closed"),
                }
                for name in self.backends
            },
            "tunnel": self.tunnel.health() if self.tunnel is not None else None,
            "learned_domains": self.egress.learned.entries(),
            "egress_mode": self.config.egress.mode,
        }

    # ------------------------------------------------------------- internals
    def _resolve_lang(self, query: str, lang: str | None) -> str:
        value = (lang or self.config.search.default_lang or "auto").lower()
        if value == "auto":
            from .backends.ddg import auto_lang

            return auto_lang(query)
        return value

    async def _run_backend(
        self, name: str, query: str, count: int, lang: str
    ) -> tuple[BackendReport, list[Hit]]:
        backend = self.backends[name]
        started = time.perf_counter()
        limiter = self._limiters.get(name)
        if limiter is None:
            return BackendReport(name=name, status="skipped", detail="disabled in config"), []
        try:
            self.breaker.check(name)
            async with limiter:
                hits = await asyncio.wait_for(
                    backend.search(query, count, lang, self.egress.fetch),
                    timeout=max(
                        self.config.search.backend_timeout + 3,
                        self.config.egress.request_timeout + 6,
                    ),
                )
        except BackendError as exc:
            self.breaker.record(name, exc)
            return self._report(name, started, error=exc), []
        except asyncio.TimeoutError:
            error = BackendError("timeout", "backend exceeded its time budget")
            self.breaker.record(name, error)
            return self._report(name, started, error=error), []
        except Exception as exc:  # noqa: BLE001 - a backend bug must not kill the query
            error = BackendError("parse_error", f"{type(exc).__name__}: {exc}")
            self.breaker.record(name, error)
            return self._report(name, started, error=error), []

        self.breaker.record(name, None)
        return (
            BackendReport(
                name=name,
                status="ok",
                count=len(hits),
                latency_ms=int((time.perf_counter() - started) * 1000),
            ),
            hits,
        )

    def _report(self, name: str, started: float, error: BackendError) -> BackendReport:
        return BackendReport(
            name=name,
            status="error",
            count=0,
            latency_ms=int((time.perf_counter() - started) * 1000),
            error_code=error.code,
            detail=error.detail or None,
        )

    async def _cache_get(self, key: tuple[str, int, str], query: str) -> SearchResponse | None:
        ttl = self.config.search.cache_ttl
        if ttl <= 0:
            return None
        async with self._cache_lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            expires, response = entry
            if expires < time.monotonic():
                self._cache.pop(key, None)
                return None
            return response.model_copy(update={"cached": True, "query": query}, deep=True)

    async def _cache_put(self, key: tuple[str, int, str], response: SearchResponse) -> None:
        ttl = self.config.search.cache_ttl
        if ttl <= 0 or response.partial:
            # Negative caching would keep telling the caller "captcha" for five
            # minutes after the backend recovered, and would hide a partial
            # result behind a stale error.
            return
        async with self._cache_lock:
            now = time.monotonic()
            for stale in [k for k, (expires, _) in self._cache.items() if expires < now]:
                self._cache.pop(stale, None)
            self._cache[key] = (now + ttl, response.model_copy(deep=True))
