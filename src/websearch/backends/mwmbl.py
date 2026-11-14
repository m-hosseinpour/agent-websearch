"""Mwmbl backend: a small, independent, keyless index.

Response shape (observed 2026-11-14): a JSON list where titles and extracts are
arrays of ``{"value": ..., "is_bold": ...}`` segments rather than plain strings.
"""

from __future__ import annotations

from typing import Any

from ..models import Hit
from .base import BackendError

MWMBL_SEARCH = "https://api.mwmbl.org/api/v1/search/"


def _flatten(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, list):
        # Segments carry their own spacing (a bare " " segment separates words),
        # so concatenate first and normalise afterwards.
        joined = "".join(
            str(part.get("value", "")) for part in value if isinstance(part, dict)
        )
        return " ".join(joined.split())
    return ""


def parse_results(payload: Any) -> list[Hit]:
    if not isinstance(payload, list):
        raise BackendError("parse_error", "expected a JSON list")
    hits: list[Hit] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        title = _flatten(item.get("title"))
        if not url.startswith("http"):
            continue
        hits.append(
            Hit(title=title or url, url=url, snippet=_flatten(item.get("extract")), engine="mwmbl")
        )
    return hits


class MwmblBackend:
    name = "mwmbl"

    def __init__(self, url: str = MWMBL_SEARCH) -> None:
        self.url = url

    async def search(self, query: str, count: int, lang: str, fetch) -> list[Hit]:
        import urllib.parse

        response = await fetch(
            self.url + "?" + urllib.parse.urlencode({"s": query}),
            headers={"Accept": "application/json", "User-Agent": "websearch-mcp/0.1"},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise BackendError("parse_error", f"invalid JSON: {exc}") from exc
        hits = parse_results(payload)
        # An empty list is a legitimate "no matches" for this index (it is
        # English-heavy), not a parsing failure. Malformed payloads raised above.
        return hits[:count]
