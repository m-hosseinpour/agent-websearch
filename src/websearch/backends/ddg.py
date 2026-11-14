"""DuckDuckGo HTML backend.

Port of the local ddg-relay logic: a plain GET of the HTML endpoint answers,
while POST (what SearXNG's built-in engine does) gets a CAPTCHA. Links come back
as redirect wrappers (``//duckduckgo.com/l/?uddg=...``) and are decoded here.

The parser is the fragile part of this whole service, which is why it lives in
its own module-level function and is tested against recorded HTML.
"""

from __future__ import annotations

import html
import re
import urllib.parse
from html.parser import HTMLParser

import httpx

from ..models import Hit
from .base import BackendError

DDG_HTML = "https://html.duckduckgo.com/html/"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)

# Persian/Arabic script decides the regional code when lang="auto".
_PERSIAN = re.compile(r"[\u0600-\u06FF\uFB50-\uFDFF\uFE70-\uFEFF]")
LANG_CODES = {
    "fa": "ir-fa",
    "en": "us-en",
}


class _ResultParser(HTMLParser):
    """Collect title/url/snippet triples from a DDG HTML results page.

    A dedicated parser instead of one regex: snippets differ (``a`` vs ``div``),
    titles contain nested ``<b>`` highlighting, and entities are common.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hits: list[dict[str, str]] = []
        self._mode: str | None = None
        self._buffer: list[str] = []
        self._href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a" and tag != "div":
            return
        classes = (dict(attrs).get("class") or "").split()
        if tag == "a" and "result__a" in classes:
            self.finish_snippet()
            self._mode = "title"
            self._buffer = []
            self._href = dict(attrs).get("href") or ""
        elif "result__snippet" in classes:
            self._mode = "snippet"
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if self._mode is None:
            return
        if self._mode == "title" and tag == "a":
            title = _clean("".join(self._buffer))
            url = real_url(self._href or "")
            if title and url.startswith("http"):
                self.hits.append({"title": title, "url": url, "snippet": ""})
            self._mode = None
            self._href = None
        elif self._mode == "snippet" and tag in ("a", "div"):
            if self.hits:
                self.hits[-1]["snippet"] = _clean("".join(self._buffer))
            self._mode = None

    def handle_data(self, data: str) -> None:
        if self._mode is not None:
            self._buffer.append(data)

    def finish_snippet(self) -> None:
        # A new title without a closing tag for the previous snippet: keep what
        # we have instead of attaching the next result's text to the old one.
        if self._mode == "snippet" and self.hits:
            self.hits[-1]["snippet"] = _clean("".join(self._buffer))
        self._mode = None


TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    return " ".join(html.unescape(TAG_RE.sub("", text or "")).split())


def real_url(href: str) -> str:
    href = html.unescape(href or "")
    match = re.search(r"[?&]uddg=([^&]+)", href)
    if match:
        return urllib.parse.unquote(match.group(1))
    if href.startswith("//"):
        return "https:" + href
    return href


def parse_results(body: str) -> list[Hit]:
    parser = _ResultParser()
    parser.feed(body)
    parser.close()
    parser.finish_snippet()
    return [Hit(title=h["title"], url=h["url"], snippet=h["snippet"], engine="ddg") for h in parser.hits]


# Markers of DDG's anomaly page. Checked only after parsing found nothing, so a
# normal page that happens to contain the word "anomaly" in a snippet is safe.
BLOCK_MARKERS = ("anomaly-modal", "challenge-form", "if this error persists", "bots use duckduckgo")


def detect_block(status: int, body: bytes, headers: httpx.Headers) -> str | None:
    if status == 202:
        return "captcha"
    text = body.decode("utf-8", "ignore").lower()
    if "result__a" not in text and any(marker in text for marker in BLOCK_MARKERS):
        return "captcha"
    return None


def lang_code(lang: str) -> str:
    normalized = (lang or "auto").lower()
    if normalized in LANG_CODES:
        return LANG_CODES[normalized]
    return normalized  # already a DDG regional code such as "de-de"


def accepts_language(lang: str) -> str:
    code = lang_code(lang).split("-")[-1]
    return f"{code}-{code.upper()},{code};q=0.9,en;q=0.5"


class DdgBackend:
    name = "ddg"

    def __init__(self, url: str = DDG_HTML) -> None:
        self.url = url

    async def search(self, query: str, count: int, lang: str, fetch) -> list[Hit]:
        params = {"q": query, "kl": lang_code(lang)}
        response = await fetch(
            self.url + "?" + urllib.parse.urlencode(params),
            headers={
                "User-Agent": UA,
                "Accept-Language": accepts_language(lang),
                "Accept": "text/html,application/xhtml+xml",
            },
            block_detector=detect_block,
        )
        body = response.text
        hits = parse_results(body)
        if not hits:
            code = detect_block(response.status_code, response.content, response.headers)
            if code:
                raise BackendError(code, "duckduckgo anomaly page")
            if "no-results" in body or "no results" in body.lower():
                return []  # the engine answered: nothing matches this query
            raise BackendError("parse_error", "no result__a elements in the response")
        return hits[:count]


def auto_lang(query: str) -> str:
    return "fa" if _PERSIAN.search(query) else "en"
