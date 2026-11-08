# tests/test_ingestion.py
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from ingestion.http import FetchRefusal
from ingestion.seismic_feed import fetch_seismic_events, parse_usgs_feature, usgs_query
from ingestion.gdacs_feed import fetch_gdacs_events, gdacs_query, parse_gdacs_feature


def _query(request) -> dict:
    """The query exactly as sent; requests_mock's own ``qs`` lowercases it."""
    return {
        key: values[0] for key, values in parse_qs(urlsplit(request.url).query).items()
    }


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


def test_usgs_query_is_a_rolling_window_to_the_second_with_no_end():
    now = datetime(2026, 11, 8, 20, 30, 15, 900_000, tzinfo=timezone.utc)

    assert usgs_query(min_magnitude=4.0, days=7, limit=1000, now=now) == {
        "format": "geojson",
        "starttime": "2026-11-01T20:30:15",
        "minmagnitude": 4.0,
        "orderby": "time",
        "limit": 1000,
    }


def test_fetch_seismic_events_window_includes_events_from_today(requests_mock):
    requests_mock.get(
        "https://earthquake.usgs.gov/fdsnws/event/1/query", json={"features": []},
    )
    before = datetime.now(timezone.utc).replace(microsecond=0)
    fetch_seismic_events()
    after = datetime.now(timezone.utc)

    sent = _query(requests_mock.request_history[0])
    # No bare date: a time to the second, and no endtime, which FDSN reads as
    # the present moment.
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", sent["starttime"])
    assert "endtime" not in sent
    start = datetime.fromisoformat(sent["starttime"]).replace(tzinfo=timezone.utc)
    assert before - timedelta(days=7) <= start <= after - timedelta(days=7)
    midnight = after.replace(hour=0, minute=0, second=0, microsecond=0)
    for event_time in (midnight + timedelta(seconds=1), after - timedelta(minutes=1)):
        assert start <= event_time <= after


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
    sent = _query(requests_mock.request_history[0])
    assert sent["pageSize"] == "2"
    assert "limit" not in sent


def test_gdacs_query_asks_for_recent_events_at_every_alert_level():
    now = datetime(2026, 11, 8, 22, 0, 0, tzinfo=timezone.utc)

    assert gdacs_query(limit=100, now=now) == {
        "eventlist": "EQ;TC;FL;VO;DR;WF",
        "alertlevel": "Green;Orange;Red",
        "fromDate": "2026-11-05T22:00:00",
        "pageSize": 100,
    }
    assert gdacs_query(limit=50, days=1, now=now)["fromDate"] == "2026-11-07T22:00:00"


def test_fetch_gdacs_events_keeps_its_client_side_bound(requests_mock):
    from ingestion.gdacs_feed import GDACS_URL

    # GDACS ignored the old ``limit`` parameter; a server that sends more than
    # was asked for is still cut to the bound here.
    features = [_gdacs_feature(eventid=index) for index in range(5)]
    requests_mock.get(GDACS_URL, json={"type": "FeatureCollection", "features": features})

    result = fetch_gdacs_events(limit=3)

    assert [item["source_record_id"] for item in result] == [
        "VO:0:1", "VO:1:1", "VO:2:1",
    ]
    sent = _query(requests_mock.request_history[0])
    assert sent["alertlevel"] == "Green;Orange;Red"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", sent["fromDate"])
    assert "toDate" not in sent


# News feed tests
import json
from unittest.mock import patch, MagicMock
from ingestion.news_feed import (
    fetch_all_feeds,
    fetch_brave_search,
    fetch_rss_feed,
    iter_feed_batches,
    iter_feed_results,
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


def test_parse_rss_entry_keeps_the_stored_text_of_a_dated_entry_unchanged():
    # The bytes every RSS record has been stored under. Reading IAEA's dates
    # must not alter them, or every live entry of every other feed would be
    # filed as a revision on the next run.
    entry = {
        "title": "T",
        "summary": "S",
        "link": "https://example.test/a",
        "published": "Mon, 03 Mar 2026 12:00:00 GMT",
        "published_parsed": time.struct_time((2026, 3, 3, 12, 0, 0, 0, 62, 0)),
    }

    result = parse_rss_entry(entry, source="bbc_world")

    assert result["raw_text"] == (
        '{"title": "T", "summary": "S", "link": "https://example.test/a", '
        '"published": "2026-03-03T12:00:00+00:00"}'
    )
    assert result["published_at"] == "2026-03-03T12:00:00+00:00"


def test_parse_rss_entry_reads_the_time_in_a_two_digit_year_date():
    entry = {
        "title": "Statement",
        "link": "https://www.iaea.org/newscenter/statements/example",
        "published": "26-11-06  14:46",
        # What feedparser makes of it: the date, with the time dropped.
        "published_parsed": time.struct_time((2026, 11, 6, 0, 0, 0, 4, 310, 0)),
    }

    result = parse_rss_entry(entry, source="iaea_topnews")
    raw = json.loads(result["raw_text"])

    # No zone is stated, so none is written; the ledger reads it as UTC.
    assert result["published_at"] == "2026-11-06T14:46:00"
    assert raw["published"] == "2026-11-06T14:46:00"
    assert raw["published_as_served"] == "26-11-06  14:46"
    from core.observations import parse_timestamp
    assert parse_timestamp(result["published_at"]) == datetime(2026, 11, 6, 14, 46)


@pytest.mark.parametrize("served", ["26-07-15", "26-13-06  14:46", "2026-11-06 14:46"])
def test_parse_rss_entry_leaves_other_dates_to_feedparser(served):
    entry = {
        "title": "Item",
        "published": served,
        "published_parsed": time.struct_time((2026, 7, 15, 0, 0, 0, 2, 196, 0)),
    }

    result = parse_rss_entry(entry, source="iaea_topnews")

    assert result["published_at"] == "2026-07-15T00:00:00+00:00"
    assert "published_as_served" not in json.loads(result["raw_text"])


def test_fetch_rss_feed_reads_iaea_times_from_the_document(requests_mock):
    url = "https://example.test/topnews"
    requests_mock.get(url, content=b"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>Test</title><item>
    <title>Synthetic statement</title><link>https://www.iaea.org/statements/1</link>
    <pubDate>26-11-06  14:46</pubDate></item></channel></rss>""")

    (record,) = fetch_rss_feed(url, "iaea_topnews")

    assert record["published_at"] == "2026-11-06T14:46:00"


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
    ("turkey_mfa", "www.mfa.gov.tr"),
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


def test_fetch_rss_feed_refuses_an_html_page_served_as_success(requests_mock):
    url = "https://example.test/rss/"
    page = b"<html><head><title>Request Rejected</title></head><body>No.</body></html>"
    requests_mock.get(url, content=page, headers={"Content-Type": "text/html"})

    with pytest.raises(FetchRefusal) as refused:
        fetch_rss_feed(url, "test-feed")

    assert refused.value.reason == f"not a feed: text/html, {len(page)} bytes"
    assert "Rejected" not in refused.value.reason


@pytest.mark.parametrize("content_type", [
    "application/rss+xml; charset=utf-8", "text/xml", "application/xml", None,
])
def test_fetch_rss_feed_returns_nothing_for_a_well_formed_empty_feed(
    requests_mock, content_type,
):
    url = "https://example.test/empty.xml"
    headers = {"Content-Type": content_type} if content_type else {}
    requests_mock.get(url, headers=headers, content=b"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>Quiet</title>
    <link>https://example.test/</link><description>d</description></channel></rss>""")

    assert fetch_rss_feed(url, "test-feed") == []


@pytest.mark.parametrize("content, content_type, reason", [
    (b"", None, "not a feed: no declared type, {size} bytes"),
    (b"<rss version='2.0'><channel><title>T", "text/xml",
     "not a feed: text/xml, {size} bytes, unparsable"),
    (b'{"items": []}', "application/json", "not a feed: application/json, {size} bytes"),
    (b"<rss version='2.0'><channel/></rss>", "text/plain",
     "not a feed: text/plain, {size} bytes"),
])
def test_fetch_rss_feed_refuses_answers_with_no_entries_that_are_not_feeds(
    requests_mock, content, content_type, reason,
):
    url = "https://example.test/odd"
    headers = {"Content-Type": content_type} if content_type else {}
    requests_mock.get(url, content=content, headers=headers)

    with pytest.raises(FetchRefusal) as refused:
        fetch_rss_feed(url, "test-feed")

    assert refused.value.reason == reason.format(size=len(content))


def test_fetch_rss_feed_reads_entries_whatever_the_declared_type(requests_mock):
    url = "https://example.test/mislabelled"
    requests_mock.get(url, headers={"Content-Type": "text/html"}, content=b"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>T</title><item><title>Kept</title>
    <link>https://example.test/kept</link></item></channel></rss>""")

    assert [r["canonical_url"] for r in fetch_rss_feed(url, "test-feed")] == [
        "https://example.test/kept",
    ]


def test_fetch_rss_feed_refuses_a_feed_whose_every_entry_is_unusable(requests_mock):
    url = "https://example.test/broken-entries.xml"
    requests_mock.get(url, content=b"<rss/>")
    parsed_feed = MagicMock(entries=[{"bad": True}, {"bad": True}])

    with patch("ingestion.news_feed.feedparser.parse", return_value=parsed_feed), \
         patch("ingestion.news_feed.parse_rss_entry", side_effect=ValueError("x")):
        with pytest.raises(FetchRefusal) as refused:
            fetch_rss_feed(url, "feed")

    assert refused.value.reason == "2 entries, none usable"


def test_feed_results_classify_each_feed_with_a_short_reason(requests_mock):
    feeds = {
        "fine": "https://example.test/fine.xml",
        "quiet": "https://example.test/quiet.xml",
        "moved": "https://example.test/moved.xml",
        "down": "https://example.test/down.xml",
        "blocked": "https://example.test/blocked",
    }
    requests_mock.get(feeds["fine"], content=b"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>T</title><item><title>One</title>
    <link>https://example.test/1</link></item></channel></rss>""")
    requests_mock.get(feeds["quiet"], content=b"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>T</title></channel></rss>""")
    requests_mock.get(
        feeds["moved"], status_code=301, headers={"Location": "https://example.test/new"},
    )
    requests_mock.get(feeds["down"], status_code=503, text="private body")
    requests_mock.get(
        feeds["blocked"], content=b"<html>x</html>", headers={"Content-Type": "text/html"},
    )

    with patch("ingestion.news_feed.RSS_FEEDS", feeds):
        results = list(iter_feed_results(limit_per_feed=10))

    assert [(r.source, r.status, len(r.records), r.reason, r.limit) for r in results] == [
        ("fine", "ok", 1, None, 10),
        ("quiet", "empty", 0, None, 10),
        ("moved", "failed", 0, "HTTP 301 redirect refused", 10),
        ("down", "failed", 0, "HTTP 503", 10),
        ("blocked", "failed", 0, "not a feed: text/html, 14 bytes", 10),
    ]
    assert len(requests_mock.request_history) == 5


def test_news_batches_pass_a_short_feeds_unused_share_on():
    feeds = {
        "short": "https://example.test/short.xml",
        "long": "https://example.test/long.xml",
        "last": "https://example.test/last.xml",
    }
    available = {"short": 0, "long": 9, "last": 9}

    def records(url, source, *, limit):
        return [{"source": source}] * min(limit, available[source])

    with patch("ingestion.news_feed.RSS_FEEDS", feeds), \
         patch("ingestion.news_feed.fetch_rss_feed", side_effect=records) as fetch:
        batches = list(iter_feed_batches(limit_per_feed=4, total_limit=6))

    assert [call.kwargs["limit"] for call in fetch.call_args_list] == [2, 3, 3]
    assert [(source, len(batch)) for source, batch in batches] == [
        ("short", 0), ("long", 3), ("last", 3),
    ]


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
