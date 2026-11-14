import httpx
import pytest

from websearch.backends.base import BackendError
from websearch.config import Config, EgressConfig, SearchConfig
from websearch.egress.router import Egress


class FakeTunnel:
    def __init__(self) -> None:
        self.ensure_calls = 0

    async def ensure(self, force: bool = False, deadline: float | None = None) -> None:
        self.ensure_calls += 1

    def health(self) -> dict:
        return {"running": True}


def make_config(tmp_path) -> Config:
    return Config(
        home=tmp_path,
        search=SearchConfig(backend_timeout=3.0),
        egress=EgressConfig(
            learned_file="blocked.txt",
            request_timeout=5.0,
            connection_timeout=1.0,
            retry_transient=1,
        ),
    )


def make_egress(tmp_path, handler, tunnel=True):
    calls: list[tuple[bool, str]] = []

    def factory(via_tunnel: bool) -> httpx.AsyncClient:
        def respond(request: httpx.Request) -> httpx.Response:
            calls.append((via_tunnel, str(request.url)))
            return handler(via_tunnel, request)

        return httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False)

    egress = Egress(make_config(tmp_path), FakeTunnel() if tunnel else None, client_factory=factory)
    return egress, calls


async def test_403_direct_falls_back_to_tunnel_and_learns(tmp_path):
    def handler(via_tunnel, request):
        return httpx.Response(403 if not via_tunnel else 200, text="ok")

    egress, calls = make_egress(tmp_path, handler)
    response = await egress.fetch("https://blocked.example/x")

    assert response.status_code == 200
    assert [via for via, _ in calls] == [False, True]
    assert egress.learned.matches("blocked.example")
    assert egress.learned.matches("api.blocked.example")
    assert "blocked.example" in (tmp_path / "blocked.txt").read_text()


async def test_learned_domain_never_tries_direct(tmp_path):
    (tmp_path / "blocked.txt").write_text("known.example\n", encoding="utf-8")

    def handler(via_tunnel, request):
        return httpx.Response(200, text="ok")

    egress, calls = make_egress(tmp_path, handler)
    await egress.fetch("https://known.example/page")

    assert calls and all(via for via, _ in calls)


async def test_captcha_is_a_backend_error_not_a_route_problem(tmp_path):
    def detector(status, body, headers):
        return "captcha" if b"anomaly" in body else None

    def handler(via_tunnel, request):
        return httpx.Response(200, content=b"<html>anomaly</html>")

    egress, calls = make_egress(tmp_path, handler)
    with pytest.raises(BackendError) as caught:
        await egress.fetch("https://html.duckduckgo.com/html/", block_detector=detector)

    assert caught.value.code == "captcha"
    assert len(calls) == 1  # no direct->tunnel hop on a backend block
    assert not egress.learned.matches("html.duckduckgo.com")


async def test_transient_500_is_retried_then_tunnel_is_learned(tmp_path):
    def handler(via_tunnel, request):
        return httpx.Response(500 if not via_tunnel else 200, text="ok")

    egress, calls = make_egress(tmp_path, handler)
    response = await egress.fetch("https://flaky.example/")

    assert response.status_code == 200
    direct = [via for via, _ in calls if not via]
    assert len(direct) == 2  # one attempt + retry_transient
    assert egress.learned.matches("flaky.example")


async def test_direct_timeout_still_reaches_the_tunnel(tmp_path):
    def handler(via_tunnel, request):
        if not via_tunnel:
            raise httpx.ReadTimeout("hung", request=request)
        return httpx.Response(200, text="ok")

    egress, calls = make_egress(tmp_path, handler)
    response = await egress.fetch("https://censored.example/")

    assert response.status_code == 200
    assert [via for via, _ in calls] == [False, False, True]
    assert egress.learned.matches("censored.example")


def test_attempt_timeout_reserves_time_for_a_tunnel_attempt(tmp_path):
    config = Config(
        home=tmp_path,
        search=SearchConfig(backend_timeout=15.0),
        egress=EgressConfig(
            learned_file="blocked.txt",
            request_timeout=20.0,
            tunnel_reserve=6.0,
        ),
    )
    egress = Egress(config, FakeTunnel())

    first = egress._attempt_timeout(20.0, routes_left=1)
    last = egress._attempt_timeout(20.0, routes_left=0)

    assert first == 14.0  # 20 - 6 reserved, under the 15s ceiling
    assert last == 15.0


async def test_learning_appends_after_a_file_without_trailing_newline(tmp_path):
    (tmp_path / "blocked.txt").write_text("example.com", encoding="utf-8")

    def handler(via_tunnel, request):
        return httpx.Response(200 if via_tunnel else 403)

    egress, _ = make_egress(tmp_path, handler)
    await egress.fetch("https://other.example/")
    egress.learned.reload()

    assert egress.learned.matches("example.com")  # not glued onto the new comment
    assert egress.learned.matches("other.example")


async def test_a_read_only_learned_file_does_not_fail_the_request(tmp_path):
    (tmp_path / "blocked.txt").mkdir()  # a directory where the file should be

    def handler(via_tunnel, request):
        return httpx.Response(200 if via_tunnel else 403)

    egress, _ = make_egress(tmp_path, handler)
    response = await egress.fetch("https://blocked.example/")

    assert response.status_code == 200
    assert not egress.learned.matches("blocked.example")


async def test_429_is_rate_limited_without_retry(tmp_path):
    def handler(via_tunnel, request):
        return httpx.Response(429)

    egress, calls = make_egress(tmp_path, handler)
    with pytest.raises(BackendError) as caught:
        await egress.fetch("https://busy.example/")

    assert caught.value.code == "rate_limited"
    assert len(calls) == 1


async def test_both_routes_failing_reports_blocked(tmp_path):
    def handler(via_tunnel, request):
        return httpx.Response(403)

    egress, calls = make_egress(tmp_path, handler)
    with pytest.raises(BackendError) as caught:
        await egress.fetch("https://sanctioned.example/")

    assert caught.value.code == "blocked"
    assert not egress.learned.matches("sanctioned.example")  # tunnel did not work
