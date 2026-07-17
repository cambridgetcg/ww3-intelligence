# tests/test_ingestion.py
import pytest

from ingestion.seismic_feed import fetch_seismic_events, parse_usgs_feature
from ingestion.gdacs_feed import fetch_gdacs_events, parse_gdacs_feature


def test_parse_usgs_feature():
    feature = {
        "id": "us7000n0k0",
        "properties": {
            "mag": 4.3,
            "place": "15 km NW of Test Region",
            "time": 1740998400000,
            "depth": 2.1,
            "type": "earthquake",
            "title": "M 4.3 - Near Test Region",
        },
        "geometry": {
            "coordinates": [51.9, 33.5, 2.1]
        }
    }
    result = parse_usgs_feature(feature)
    assert result["source"] == "usgs"
    assert result["type"] == "seismic"
    assert result["geo_lat"] == 33.5
    assert result["geo_lon"] == 51.9
    assert '"mag": 4.3' in result["raw_text"]
    assert result["location_name"] == "15 km NW of Test Region"
    assert result["confidence"] == "not-assessed"


def test_parse_usgs_feature_missing_depth():
    feature = {
        "id": "us7000n0k1",
        "properties": {
            "mag": 3.1,
            "place": "Test Region",
            "time": 1740998400000,
            "type": "earthquake",
            "title": "M 3.1 - Tehran",
        },
        "geometry": {
            "coordinates": [51.4, 35.7]
        }
    }
    result = parse_usgs_feature(feature)
    assert result["source"] == "usgs"
    assert result["geo_lat"] == 35.7


def test_fetch_seismic_events_returns_list(requests_mock):
    requests_mock.get(
        "https://earthquake.usgs.gov/fdsnws/event/1/query",
        json={"features": []}
    )
    results = fetch_seismic_events(min_magnitude=4.0, days=1)
    assert isinstance(results, list)
    assert len(results) == 0


def test_fetch_seismic_events_parses_features(requests_mock):
    requests_mock.get(
        "https://earthquake.usgs.gov/fdsnws/event/1/query",
        json={
            "features": [
                {
                    "id": "us7000n0k0",
                    "properties": {
                        "mag": 4.3,
                        "place": "Near Test Region",
                        "time": 1740998400000,
                        "type": "earthquake",
                        "title": "M 4.3",
                    },
                    "geometry": {"coordinates": [51.9, 33.5, 2.1]}
                }
            ]
        }
    )
    results = fetch_seismic_events(min_magnitude=4.0, days=1)
    assert len(results) == 1
    assert results[0]["type"] == "seismic"


def test_fetch_seismic_events_is_limited_and_skips_invalid_siblings(requests_mock):
    requests_mock.get(
        "https://earthquake.usgs.gov/fdsnws/event/1/query",
        json={
            "features": [
                {"broken": True},
                {
                    "id": "invalid-properties",
                    "properties": [],
                    "geometry": {"coordinates": [1.0, 2.0, 3.0]},
                },
                {
                    "id": "valid-1",
                    "properties": {
                        "mag": 4.8, "place": "Test Region",
                        "time": 1740998400000, "type": "earthquake",
                        "title": "M 4.8",
                    },
                    "geometry": {"coordinates": [1.0, 2.0, 3.0]},
                },
            ]
        },
    )

    results = fetch_seismic_events(limit=3)

    assert [record["source_record_id"] for record in results] == ["valid-1"]
    assert requests_mock.request_history[0].qs["limit"] == ["3"]


def _gdacs_feature(**property_overrides):
    properties = {
        "eventid": 1000144,
        "episodeid": 1,
        "eventtype": "VO",
        "eventname": "Eruption Mayon",
        "description": "Eruption Mayon",
        "fromdate": "2026-07-16T01:59:00",
        "todate": "2026-07-16T01:59:00",
        "alertlevel": "Orange",
        "alertscore": 2,
        "country": "Philippines",
        "iscurrent": "true",
        "severitydata": {"severity": 0, "severitytext": "", "severityunit": ""},
        "url": {"report": "https://www.gdacs.org/report.aspx?eventid=1000144"},
    }
    properties.update(property_overrides)
    return {
        "type": "Feature",
        "properties": properties,
        "geometry": {"type": "Point", "coordinates": [123.68, 13.25]},
    }


def test_parse_gdacs_feature_preserves_alert_provenance_without_intent():
    result = parse_gdacs_feature(_gdacs_feature())
    raw = json.loads(result["raw_text"])

    assert result["source"] == "gdacs"
    assert result["source_record_id"] == "VO:1000144:1"
    assert result["type"] == "hazard"
    assert result["geo_lat"] == 13.25
    assert result["canonical_url"].startswith("https://www.gdacs.org/report")
    assert raw["alert_level"] == "Orange"
    assert "intent" not in raw


def test_fetch_gdacs_events_is_bounded_and_skips_malformed_sibling(requests_mock):
    from ingestion.gdacs_feed import GDACS_URL

    requests_mock.get(
        GDACS_URL,
        json={"type": "FeatureCollection", "features": [
            {"properties": []},
            _gdacs_feature(),
        ]},
    )

    result = fetch_gdacs_events(limit=2)

    assert [item["source_record_id"] for item in result] == ["VO:1000144:1"]
    assert requests_mock.request_history[0].qs["limit"] == ["2"]


# News feed tests
import json
from unittest.mock import patch, MagicMock
from ingestion.news_feed import (
    fetch_all_feeds,
    fetch_brave_search,
    fetch_rss_feed,
    iter_feed_batches,
    parse_rss_entry,
)


def test_parse_rss_entry():
    entry = {
        "title": "Synthetic earthquake report",
        "link": "https://reuters.com/article/123",
        "summary": "A synthetic 4.3 magnitude quake report...",
        "published": "Mon, 03 Mar 2026 12:00:00 GMT",
    }
    result = parse_rss_entry(entry, source="reuters")
    assert result["type"] == "news"
    assert result["source"] == "reuters"
    assert "Synthetic earthquake report" in result["raw_text"]
    assert result["confidence"] == "not-assessed"


def test_parse_rss_entry_returns_dict_keys():
    entry = {
        "title": "Test headline",
        "link": "https://example.com",
        "summary": "Test summary",
    }
    result = parse_rss_entry(entry, source="test")
    assert "source" in result
    assert "type" in result
    assert "raw_text" in result
    assert result["geo_lat"] is None
    assert result["geo_lon"] is None


def test_parse_rss_entry_keeps_bounded_first_party_feed_content():
    entry = {
        "title": "Official release",
        "link": "https://www.whitehouse.gov/releases/example/",
        "content": [{"value": "<p>Mistakes were made.</p>"}],
    }

    official = parse_rss_entry(entry, source="white_house")
    newsroom = parse_rss_entry(entry, source="bbc_world")

    assert json.loads(official["raw_text"])["content"] == "<p>Mistakes were made.</p>"
    assert "content" not in json.loads(newsroom["raw_text"])


@pytest.mark.parametrize("source, host", [
    ("russia_mfa", "mid.ru"),
    ("iaea_topnews", "www.iaea.org"),
])
def test_parse_rss_entry_normalizes_known_official_links_to_https(source, host):
    result = parse_rss_entry({
        "title": "Official index item",
        "link": f"http://{host}/example",
    }, source=source)

    assert result["canonical_url"] == f"https://{host}/example"
    assert json.loads(result["raw_text"])["link"] == f"https://{host}/example"


def test_fetch_rss_feed_uses_bounded_http_fetch(requests_mock):
    url = "https://example.test/feed.xml"
    requests_mock.get(url, content=b"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>Test</title><item>
    <title>Bounded report</title><link>https://example.test/report/1</link>
    <description>Source summary.</description></item></channel></rss>""")

    results = fetch_rss_feed(url, "test-feed")

    assert len(results) == 1
    assert results[0]["source_record_id"] == "https://example.test/report/1"
    assert requests_mock.request_history[0].timeout == (3.05, 15)


def test_fetch_rss_feed_rejects_oversized_response(requests_mock, monkeypatch):
    url = "https://example.test/huge.xml"
    requests_mock.get(url, content=b"four")
    monkeypatch.setattr("ingestion.news_feed.MAX_FEED_BYTES", 3)

    with pytest.raises(ValueError, match="exceeds"):
        fetch_rss_feed(url, "test-feed")


def test_news_batches_apply_per_feed_and_total_limits():
    feeds = {
        "one": "https://example.test/one.xml",
        "two": "https://example.test/two.xml",
        "three": "https://example.test/three.xml",
    }

    def records(url, source, *, limit):
        return [{"source": source, "index": index} for index in range(limit)]

    with patch("ingestion.news_feed.RSS_FEEDS", feeds), \
         patch("ingestion.news_feed.fetch_rss_feed", side_effect=records) as fetch:
        batches = list(iter_feed_batches(limit_per_feed=4, total_limit=5))

    assert [(source, len(batch)) for source, batch in batches] == [
        ("one", 2), ("two", 2), ("three", 1),
    ]
    assert [call.kwargs["limit"] for call in fetch.call_args_list] == [2, 2, 1]


def test_rss_invalid_entry_does_not_discard_valid_sibling(requests_mock, capsys):
    url = "https://example.test/mixed.xml"
    requests_mock.get(url, content=b"<rss/>")
    parsed_feed = MagicMock(entries=[{"bad": True}, {"title": "valid"}])
    valid = {"source": "feed", "type": "news", "raw_text": "{}"}

    with patch("ingestion.news_feed.feedparser.parse", return_value=parsed_feed), \
         patch(
             "ingestion.news_feed.parse_rss_entry",
             side_effect=[ValueError("private entry"), valid],
         ):
        records = fetch_rss_feed(url, "feed")

    assert records == [valid]
    error = capsys.readouterr().out
    assert "Skipped invalid feed entry (ValueError)" in error
    assert "private entry" not in error


def test_news_batches_continue_after_sanitized_feed_failure(capsys):
    feeds = {
        "broken": "https://example.test/broken.xml",
        "working": "https://example.test/working.xml",
    }
    working = [{"source": "working", "type": "news", "raw_text": "{}"}]

    with patch("ingestion.news_feed.RSS_FEEDS", feeds), \
         patch(
             "ingestion.news_feed.fetch_rss_feed",
             side_effect=[RuntimeError("private failure"), working],
         ):
        batches = list(iter_feed_batches())

    assert batches == [("working", working)]
    error = capsys.readouterr().out
    assert "Failed broken (RuntimeError)" in error
    assert "private failure" not in error


def test_fetch_brave_search_returns_empty_when_no_key(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "")
    monkeypatch.setenv("BRAVE_API_KEY", "")
    result = fetch_brave_search("geopolitical conflict")
    assert result == []


def test_fetch_brave_search_calls_api_when_key_set(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key-123")
    mock_response = MagicMock()
    response_payload = {
        "results": [
            {
                "title": "Tensions escalate in disputed region",
                "description": "Military forces have been mobilized along the border...",
                "url": "https://example.com/article/1",
                "age": "2 hours ago",
            },
            {
                "title": "UN Security Council convenes emergency session",
                "description": "World leaders gather to address the crisis...",
                "url": "https://example.com/article/2",
                "age": "1 hour ago",
            },
        ]
    }
    mock_response.raise_for_status = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.iter_content.return_value = [json.dumps(response_payload).encode("utf-8")]
    mock_response.close = MagicMock()

    with patch("ingestion.news_feed.requests.get", return_value=mock_response) as mock_get:
        results = fetch_brave_search("geopolitical conflict", count=2)

    mock_get.assert_called_once()
    call_kwargs = mock_get.call_args
    assert call_kwargs[0][0] == "https://api.search.brave.com/res/v1/news/search"
    assert call_kwargs[1]["headers"]["X-Subscription-Token"] == "test-key-123"
    assert call_kwargs[1]["params"]["q"] == "geopolitical conflict"
    assert call_kwargs[1]["params"]["count"] == 2
    assert call_kwargs[1]["allow_redirects"] is False
    assert call_kwargs[1]["stream"] is True

    assert len(results) == 2
    for leaf in results:
        assert leaf["source"] == "brave_search"
        assert leaf["type"] == "news"
        assert leaf["confidence"] == "not-assessed"
        assert leaf["geo_lat"] is None
        assert leaf["geo_lon"] is None
        assert leaf["location_name"] is None
        raw = json.loads(leaf["raw_text"])
        assert "title" in raw
        assert "summary" in raw


def test_fetch_brave_search_returns_empty_on_api_error(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key-123")
    with patch("ingestion.news_feed.requests.get", side_effect=Exception("connection refused")):
        result = fetch_brave_search("war escalation")
    assert result == []
