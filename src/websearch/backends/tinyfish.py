"""TinyFish TinySearch backend: free keyed web search.

API: ``GET https://api.search.tinyfish.ai?query=...`` with ``X-API-Key``.
Free tier: 30 requests/min, 500/hour, 12,000/day. The service enforces the
first two via ``BackendConfig``; this module makes one request per search and
returns the API's first page (10 results).
"""

from __future__ import annotations

import os
import urllib.parse

from ..models import Hit
from .base import BackendError

TINYFISH_SEARCH = "https://api.search.tinyfish.ai"
API_KEY_ENV = "TINYFISH_API_KEY"
# TinyFish resolves language -> country automatically; make the intended locale
# explicit for the two languages this tool is used with.
DEFAULT_LOCATIONS = {"fa": "IR", "en": "US"}


def locale(lang: str) -> tuple[str, str | None]:
    """Map the tool's lang value (en, fa, de-de) to TinyFish language/location."""
    parts = [part for part in (lang or "").lower().replace("_", "-").split("-") if part]
    language = parts[0] if parts else "en"
    if len(parts) > 1 and len(parts[1]) == 2:
        return language, parts[1].upper()
    return language, DEFAULT_LOCATIONS.get(language)


def _text(value: object) -> str:
    return " ".join(str(value or "").split())


def parse_results(payload: object, count: int) -> list[Hit]:
    if not isinstance(payload, dict):
        raise BackendError("parse_error", "expected a JSON object")
    raw = payload.get("results")
    if not isinstance(raw, list):
        raise BackendError("parse_error", "missing results list")
    hits: list[Hit] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url.startswith("http"):
            continue
        title = _text(item.get("title")) or url
        snippet = _text(item.get("snippet"))
        if not snippet:
            # domain_type=news returns publisher/date instead of snippets.
            snippet = " — ".join(
                part
                for part in (_text(item.get("publisher")), _text(item.get("date")))
                if part
            )
        hits.append(Hit(title=title, url=url, snippet=snippet, engine="tinyfish"))
    return hits[:count]


def _response_error(response) -> BackendError:
    status = response.status_code
    detail = response.text[:200].replace("\n", " ").strip()
    if status == 401:
        return BackendError("unauthorized", "TinyFish API key missing or invalid")
    if status == 402:
        return BackendError("quota", "TinyFish daily free allowance exhausted")
    if status == 403:
        return BackendError("blocked", "TinyFish rejected the search upstream")
    if status == 429:
        return BackendError("rate_limited", "TinyFish rate limit exceeded")
    return BackendError("http_error", f"HTTP {status} from TinyFish: {detail}")


class TinyFishBackend:
    name = "tinyfish"

    async def search(self, query: str, count: int, lang: str, fetch) -> list[Hit]:
        api_key = (os.environ.get(API_KEY_ENV) or "").strip()
        if not api_key:
            raise BackendError("unauthorized", f"{API_KEY_ENV} is not set")
        language, location = locale(lang)
        params = {"query": query, "language": language}
        if location:
            params["location"] = location
        response = await fetch(
            TINYFISH_SEARCH + "?" + urllib.parse.urlencode(params),
            headers={"X-API-Key": api_key, "Accept": "application/json"},
        )
        if response.status_code != 200:
            raise _response_error(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise BackendError("parse_error", f"invalid JSON: {exc}") from exc
        return parse_results(payload, count)
