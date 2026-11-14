from pathlib import Path

from websearch.backends.ddg import (
    auto_lang,
    detect_block,
    lang_code,
    parse_results,
    real_url,
)

REAL_FIXTURE = Path(__file__).parent / "fixtures" / "ddg_results.html"
SYNTHETIC_FIXTURE = Path(__file__).parent / "fixtures" / "ddg_results_synthetic.html"


def test_parses_synthetic_edge_cases():
    hits = parse_results(SYNTHETIC_FIXTURE.read_text(encoding="utf-8"))
    assert [hit.title for hit in hits] == [
        "Deep Research & Agents",
        "پژوهش عمیق",
        "Plain link",
    ]
    assert [hit.url for hit in hits] == [
        "https://example.com/deep?a=1",
        "https://fa.wikipedia.org/wiki/کار",
        "https://plain.example.org/page",
    ]
    assert hits[0].snippet == "A survey of deep research systems & benchmarks."
    assert hits[1].snippet == "توضیح فارسی دربارهٔ پژوهش."
    assert hits[2].snippet == "No redirect wrapper here."
    assert all(hit.engine == "ddg" for hit in hits)


def test_parses_real_recorded_page():
    body = REAL_FIXTURE.read_text(encoding="utf-8")
    hits = parse_results(body)

    assert len(hits) == 10
    assert hits[0].title == "Python's asyncio: A Hands-On Walkthrough - Real Python"
    assert hits[0].url == "https://realpython.com/async-io-python/"
    assert "write concurrent code using the async" in hits[0].snippet
    assert all(hit.url.startswith("http") for hit in hits)
    assert all("duckduckgo.com/l/" not in hit.url for hit in hits)
    assert all(hit.snippet for hit in hits)
    assert detect_block(200, body.encode("utf-8"), {}) is None


def test_real_url_handles_wrappers_and_plain_links():
    assert real_url("//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.b%2Fc%3Fx%3D1") == "https://a.b/c?x=1"
    assert real_url("//example.org/x") == "https://example.org/x"
    assert real_url("https://example.org/x") == "https://example.org/x"


def test_zero_result_page_with_a_marker_word_is_not_a_captcha():
    page = b'<input value="anomaly detection"><div class="no-results">No results.</div>'
    assert detect_block(200, page, {}) is None
    assert detect_block(200, b'<div id="anomaly-modal">unusual traffic</div>', {}) == "captcha"


def test_block_detection_needs_no_results():
    assert detect_block(202, b"<html>anomaly</html>", {}) == "captcha"
    assert detect_block(200, b"<html>If this error persists...</html>", {}) == "captcha"
    # A normal page that merely mentions the word must not be flagged.
    normal = b'<a class="result__a" href="x">anomaly detection</a>'
    assert detect_block(200, normal, {}) is None
    assert detect_block(200, b"<html>nothing here</html>", {}) is None


def test_language_codes():
    assert lang_code("fa") == "ir-fa"
    assert lang_code("en") == "us-en"
    assert lang_code("de-de") == "de-de"
    assert auto_lang("تحقیق عمیق") == "fa"
    assert auto_lang("deep research") == "en"
