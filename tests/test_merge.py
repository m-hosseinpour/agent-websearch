from websearch.core import canonical_key, merge_hits
from websearch.models import Hit, SearchResponse


def test_canonical_ignores_trackers_www_fragment_and_trailing_slash():
    a = canonical_key("https://www.Example.com/post/?utm_source=x&b=2&a=1#top")
    b = canonical_key("http://example.com/post?a=1&b=2")
    assert a == b


def test_merge_keeps_first_source_order_and_drops_duplicates():
    ddg = [
        Hit(title="A", url="https://example.com/a?utm_medium=x", snippet="one", engine="ddg"),
        Hit(title="B", url="https://example.com/b", snippet="two", engine="ddg"),
    ]
    mwmbl = [
        Hit(title="A again", url="https://www.example.com/a", snippet="dup", engine="mwmbl"),
        Hit(title="C", url="https://example.org/c", snippet="three", engine="mwmbl"),
    ]
    merged = merge_hits([("ddg", ddg), ("mwmbl", mwmbl)], 10)
    assert [hit.title for hit in merged] == ["A", "B", "C"]
    assert [hit.engine for hit in merged] == ["ddg", "ddg", "mwmbl"]


def test_merge_respects_count():
    hits = [Hit(title=str(i), url=f"https://example.com/{i}", engine="ddg") for i in range(5)]
    assert len(merge_hits([("ddg", hits)], 2)) == 2


def test_a_response_with_no_backends_counts_as_failed():
    # Otherwise a misconfigured service would answer 200 with an empty list.
    assert SearchResponse(query="q", lang="en", count=10).failed()
