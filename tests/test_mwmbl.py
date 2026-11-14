from websearch.backends.mwmbl import parse_results


def test_flattens_segment_lists():
    payload = [
        {
            "url": "https://en.wikipedia.org/wiki/Deep_Research",
            "title": [{"value": "Deep", "is_bold": True}, {"value": " Research", "is_bold": False}],
            "extract": [{"value": "may refer to", "is_bold": False}, {"value": " ChatGPT", "is_bold": True}],
            "source": "wikipedia",
        },
        {"url": "not-a-url", "title": "skip me", "extract": "x"},
        {"url": "https://example.org/a", "title": "", "extract": []},
    ]
    hits = parse_results(payload)
    assert len(hits) == 2
    assert hits[0].title == "Deep Research"
    assert hits[0].snippet == "may refer to ChatGPT"
    assert hits[0].engine == "mwmbl"
    assert hits[1].title == "https://example.org/a"  # empty title falls back to the URL


def test_empty_list_is_a_valid_answer():
    assert parse_results([]) == []
