import asyncio
import time

import pytest

from websearch.backends.base import BackendError
from websearch.config import BackendConfig, Config, SearchConfig
from websearch.core import Breaker, Limiter, SearchService, canonical_key
from websearch.models import Hit


class FakeBackend:
    def __init__(self, name, error=None):
        self.name = name
        self.error = error
        self.calls = 0

    async def search(self, query, count, lang, fetch):
        self.calls += 1
        if self.error:
            raise self.error
        return [Hit(title=f"{self.name} hit", url=f"https://{self.name}.example/1", engine=self.name)]


class FakeEgress:
    async def fetch(self, *args, **kwargs):  # never used by FakeBackend
        raise AssertionError("FakeBackend does not fetch")

    async def aclose(self):
        pass


def make_service(tmp_path, backends, **search):
    config = Config(
        home=tmp_path,
        search=SearchConfig(backend_timeout=2.0, **search),
        backends={name: BackendConfig() for name in backends},
    )
    return SearchService(config, backends, FakeEgress(), None)


async def test_a_backend_disabled_in_config_reports_skipped(tmp_path):
    config = Config(
        home=tmp_path,
        search=SearchConfig(backend_timeout=2.0),
        backends={"ddg": BackendConfig(), "mwmbl": BackendConfig(enabled=False)},
    )
    service = SearchService(
        config, {"ddg": FakeBackend("ddg"), "mwmbl": FakeBackend("mwmbl")}, FakeEgress(), None
    )
    response = await service.search("q")

    statuses = {report.name: report.status for report in response.backends}
    assert statuses == {"ddg": "ok", "mwmbl": "skipped"}
    assert response.partial is False
    assert service.backends["mwmbl"].calls == 0


async def test_failures_are_not_cached(tmp_path):
    # http_error (not captcha) so the breaker does not skip the second call.
    backend = FakeBackend("ddg", BackendError("http_error"))
    service = make_service(tmp_path, {"ddg": backend}, cache_ttl=300)

    first = await service.search("q")
    second = await service.search("q")

    assert first.failed() and second.failed()
    assert backend.calls == 2  # a cached failure would leave this at 1
    assert second.cached is False


async def test_success_is_cached_and_keeps_the_current_spelling(tmp_path):
    backend = FakeBackend("ddg")
    service = make_service(tmp_path, {"ddg": backend}, cache_ttl=300)

    await service.search("Deep Research")
    second = await service.search("deep research")

    assert backend.calls == 1
    assert second.cached is True
    assert second.query == "deep research"


def test_circuit_open_does_not_shrink_a_captcha_cooldown():
    breaker = Breaker()
    breaker.record("ddg", BackendError("captcha"))
    start = breaker.open_seconds("ddg")
    assert start > 500

    for _ in range(5):
        breaker.record("ddg", BackendError("circuit_open"))

    assert breaker.open_seconds("ddg") >= start - 1


async def test_partial_results_are_not_cached(tmp_path):
    good = FakeBackend("ddg")
    bad = FakeBackend("mwmbl", BackendError("captcha"))
    service = make_service(tmp_path, {"ddg": good, "mwmbl": bad}, cache_ttl=300)

    first = await service.search("q")
    second = await service.search("q")

    assert first.partial is True
    assert second.cached is False
    assert good.calls == 2


async def test_circuit_open_is_reported_without_calling_the_backend(tmp_path):
    backend = FakeBackend("ddg", BackendError("captcha"))
    service = make_service(tmp_path, {"ddg": backend}, cache_ttl=0)

    await service.search("q")
    second = await service.search("other query")

    assert second.backends[0].error_code == "circuit_open"
    assert backend.calls == 1


def test_canonical_key_survives_a_malformed_url():
    assert canonical_key("http://[bad") == "http://[bad"
    assert canonical_key("https://example.com/a") == "example.com/a?"


async def test_hourly_cap_fails_fast_with_a_retry_hint():
    limiter = Limiter(0, 1, max_per_hour=2, window=3600)

    async with limiter:
        pass
    async with limiter:
        pass
    with pytest.raises(BackendError) as exc:
        async with limiter:
            pass

    assert exc.value.code == "rate_limited"
    assert "hourly cap of 2" in exc.value.detail


async def test_hourly_window_expires():
    limiter = Limiter(0, 1, max_per_hour=1, window=0.05)
    async with limiter:
        pass
    await asyncio.sleep(0.06)
    async with limiter:
        pass
