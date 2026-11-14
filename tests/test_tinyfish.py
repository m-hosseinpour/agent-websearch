import httpx
import pytest

from websearch.backends.base import BackendError
from websearch.backends.tinyfish import TinyFishBackend, locale, parse_results


class FakeFetch:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def payload(*items, query="q"):
    return {"query": query, "results": list(items), "total_results": len(items), "page": 0}


def item(**overrides):
    base = {
        "position": 1,
        "site_name": "example.com",
        "title": "Example title",
        "snippet": "Example snippet",
        "url": "https://example.com/page",
    }
    base.update(overrides)
    return base


async def test_maps_fields_and_sends_locale_and_key(monkeypatch):
    monkeypatch.setenv("TINYFISH_API_KEY", "secret")
    fetch = FakeFetch(httpx.Response(200, json=payload(item(), item(url="https://b.example/2"))))

    hits = await TinyFishBackend().search("آموزش جنگو", 5, "fa", fetch)

    assert [hit.engine for hit in hits] == ["tinyfish", "tinyfish"]
    assert hits[0].title == "Example title"
    assert hits[0].url == "https://example.com/page"
    assert hits[0].snippet == "Example snippet"
    url, kwargs = fetch.calls[0]
    assert "query=%D8%A2%D9%85%D9%88%D8%B2%D8%B4" in url
    assert "language=fa" in url
    assert "location=IR" in url
    assert kwargs["headers"]["X-API-Key"] == "secret"


async def test_truncates_to_the_requested_count(monkeypatch):
    monkeypatch.setenv("TINYFISH_API_KEY", "secret")
    three = payload(*(item(position=n) for n in range(1, 4)))
    hits = await TinyFishBackend().search("q", 2, "en", FakeFetch(httpx.Response(200, json=three)))
    assert len(hits) == 2


def test_news_results_use_publisher_and_date_when_snippet_is_empty():
    hits = parse_results(
        payload(item(snippet="", publisher="WSJ", date="2026-11-14")),
        10,
    )
    assert hits[0].snippet == "WSJ — 2026-11-14"


async def test_missing_key_is_unauthorized_without_a_request(monkeypatch):
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    fetch = FakeFetch(httpx.Response(200, json=payload(item())))

    with pytest.raises(BackendError) as exc:
        await TinyFishBackend().search("q", 5, "en", fetch)

    assert exc.value.code == "unauthorized"
    assert fetch.calls == []


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "unauthorized"), (402, "quota"), (403, "blocked"), (429, "rate_limited"), (500, "http_error")],
)
async def test_maps_api_errors(monkeypatch, status, code):
    monkeypatch.setenv("TINYFISH_API_KEY", "secret")
    fetch = FakeFetch(httpx.Response(status, json={"error": {"code": "X"}}))

    with pytest.raises(BackendError) as exc:
        await TinyFishBackend().search("q", 5, "en", fetch)

    assert exc.value.code == code


async def test_invalid_json_and_missing_results_are_parse_errors(monkeypatch):
    monkeypatch.setenv("TINYFISH_API_KEY", "secret")
    with pytest.raises(BackendError) as exc:
        await TinyFishBackend().search("q", 5, "en", FakeFetch(httpx.Response(200, text="not json")))
    assert exc.value.code == "parse_error"

    with pytest.raises(BackendError) as exc:
        await TinyFishBackend().search(
            "q", 5, "en", FakeFetch(httpx.Response(200, json={"query": "q"}))
        )
    assert exc.value.code == "parse_error"


def test_locale_mapping():
    assert locale("en") == ("en", "US")
    assert locale("fa") == ("fa", "IR")
    assert locale("de-de") == ("de", "DE")
    assert locale("fr") == ("fr", None)
