import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import feedparser
import requests

from core.registry import get_source_profile, rss_source_profiles
from core.settings import get_setting
from ingestion.http import (
    FETCH_FAILED,
    FetchRefusal,
    fetch_limited,
    fetch_limited_response,
    fetch_result,
)


RSS_FEEDS = {item.source_id: item.endpoint for item in rss_source_profiles()}

MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_FEED_ENTRIES = 500
DEFAULT_FEED_ENTRIES = 100
MAX_ENTRY_SUMMARY_CHARS = 100_000
MAX_ENTRY_CONTENT_CHARS = 100_000
MAX_ENTRY_CONTENT_PARTS = 16
MAX_SEARCH_RESPONSE_BYTES = 5 * 1024 * 1024

# Media types a feed is served under. A body with entries counts whatever
# its declared type; one with none is a feed only under one of these.
_FEED_MEDIA_TYPES = frozenset({"application/xml", "text/xml"})

_HTTPS_CANONICAL_HOSTS = {
    "iaea_topnews": {"iaea.org", "www.iaea.org"},
    "russia_mfa": {"mid.ru", "www.mid.ru"},
    "turkey_mfa": {"mfa.gov.tr", "www.mfa.gov.tr"},
}


def _canonical_link(value, source: str) -> str | None:
    if value is None:
        return None
    link = str(value)[:10_000]
    parsed = urlsplit(link)
    if (
        parsed.scheme == "http"
        and parsed.hostname in _HTTPS_CANONICAL_HOSTS.get(source, set())
    ):
        return urlunsplit(
            ("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment)
        )
    return link


def _first_party_content(entry, source: str) -> str:
    """Keep bounded full text only when an official feed supplies it directly."""
    profile = get_source_profile(source)
    if profile is None or profile.content_role != "first-party-statement":
        return ""
    content = entry.get("content")
    if not isinstance(content, (list, tuple)):
        return ""
    parts = []
    remaining = MAX_ENTRY_CONTENT_CHARS
    for item in content[:MAX_ENTRY_CONTENT_PARTS]:
        if not isinstance(item, Mapping):
            continue
        value = item.get("value")
        if not isinstance(value, str) or not value:
            continue
        separator_chars = 1 if parts else 0
        if remaining <= separator_chars:
            break
        parts.append(value[:remaining - separator_chars])
        remaining -= separator_chars
        remaining -= len(parts[-1])
        if remaining <= 0:
            break
    return "\n".join(parts)


def _is_feed_media_type(content_type: str | None) -> bool:
    return content_type in _FEED_MEDIA_TYPES or (
        content_type is not None and content_type.endswith("+xml")
    )


def _not_a_feed(feed, response) -> str | None:
    """Say why an answer with no entries is not an empty feed, or None.

    The reason is built from the declared media type and the byte count,
    never from the body: a block page's wording is the publisher's (or its
    firewall's) text, and stays out of the log.
    """
    declared = response.content_type or "no declared type"
    shape = f"{declared}, {len(response.body)} bytes"
    if response.content_type is not None and not _is_feed_media_type(
        response.content_type
    ):
        return f"not a feed: {shape}"
    if not feed.get("version"):
        return f"not a feed: {shape}"
    exception = feed.get("bozo_exception")
    if feed.get("bozo") and not isinstance(
        exception, feedparser.CharacterEncodingOverride
    ):
        # An encoding override is feedparser noting a mislabelled charset in a
        # document it parsed; anything else here is a document it could not.
        return f"not a feed: {shape}, unparsable"
    return None


def fetch_rss_feed(
    url: str,
    source: str,
    *,
    limit: int = DEFAULT_FEED_ENTRIES,
) -> list:
    """Return up to ``limit`` records from one feed.

    An empty list means a well-formed feed that listed no items. An answer
    that is not a feed (an HTML page served with status 200, a challenge,
    a body feedparser cannot read), or one whose every entry is unusable,
    raises :class:`FetchRefusal` so it counts as a failed fetch.
    """
    if not 1 <= limit <= MAX_FEED_ENTRIES:
        raise ValueError(f"feed entry limit must be between 1 and {MAX_FEED_ENTRIES}")
    response = fetch_limited_response(
        url,
        request_get=requests.get,
        max_bytes=MAX_FEED_BYTES,
        total_timeout=30,
        headers={"User-Agent": "ww3-intelligence/observation-ledger"},
        timeout=(3.05, 15),
    )
    feed = feedparser.parse(response.body)
    entries = feed.entries[:limit]
    if not entries:
        reason = _not_a_feed(feed, response)
        if reason is not None:
            raise FetchRefusal(reason)
        return []
    results = []
    for entry in entries:
        try:
            parsed = parse_rss_entry(entry, source)
        except (TypeError, ValueError, OverflowError) as exc:
            print(f"[news_feed] Skipped invalid {source} entry ({type(exc).__name__}).")
            continue
        if parsed:
            results.append(parsed)
    if not results:
        raise FetchRefusal(f"{len(entries)} entries, none usable")
    return results


# IAEA dates its items like "26-11-06  14:46": a two-digit year, two spaces, a
# time and no zone. feedparser keeps the date and drops the time, which stored
# every IAEA item at 00:00 UTC. Only this exact shape is read here, so no other
# feed's stored text changes.
_TWO_DIGIT_YEAR_AND_TIME = re.compile(r"\d\d-\d\d-\d\d\s+\d\d:\d\d", re.ASCII)


def _two_digit_year_and_time(value) -> str | None:
    """Read "YY-MM-DD  HH:MM" as a time with no zone, or return None.

    No zone is stated, so the time is read as UTC, as every zoneless time is
    in this ledger, and it is written without an offset so the stored record
    does not claim one. If IAEA means Vienna time, the stored time is one
    hour (CET) or two (CEST) late; which zone IAEA means is unverified.
    """
    if not isinstance(value, str) or len(value) > 32:
        return None
    text = value.strip()
    if not _TWO_DIGIT_YEAR_AND_TIME.fullmatch(text):
        return None
    try:
        return datetime.strptime(" ".join(text.split()), "%y-%m-%d %H:%M").isoformat()
    except ValueError:
        return None


def parse_rss_entry(entry, source: str) -> dict:
    title = entry.get("title", "")
    summary = entry.get("summary", entry.get("description", ""))
    title = str(title)[:1_000]
    summary = str(summary)[:MAX_ENTRY_SUMMARY_CHARS]
    canonical_url = _canonical_link(entry.get("link"), source)
    published_as_served = entry.get("published") or entry.get("updated")
    published_at = published_as_served
    zoneless_time = _two_digit_year_and_time(published_as_served)
    # A few official feeds publish two-digit years (for example 26-07-15).
    # feedparser exposes a normalized tuple even when the original string is
    # not understood by our generic timestamp normalizer.
    parsed_time = entry.get("published_parsed") or entry.get("updated_parsed")
    if zoneless_time is not None:
        published_at = zoneless_time
    elif parsed_time and len(parsed_time) >= 6:
        try:
            published_at = datetime(
                *[int(part) for part in parsed_time[:6]], tzinfo=timezone.utc
            ).isoformat()
        except (TypeError, ValueError, OverflowError):
            pass
    source_record_id = entry.get("id") or entry.get("guid") or canonical_url
    raw_payload = {
        "title": title,
        "summary": summary,
        "link": canonical_url,
        "published": published_at,
    }
    if zoneless_time is not None:
        # The publisher's own string, so the zone reading above can be undone.
        raw_payload["published_as_served"] = published_as_served
    content = _first_party_content(entry, source)
    if content:
        raw_payload["content"] = content
    raw = json.dumps(raw_payload)
    return {
        "source": source,
        "source_record_id": source_record_id,
        "type": "news",
        "raw_text": raw,
        "canonical_url": canonical_url,
        "published_at": published_at,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "confidence": "not-assessed",
        "source_confidence": "not-assessed",
        "geo_lat": None,
        "geo_lon": None,
        "location_name": None,
        "synthetic": False,
    }


def iter_feed_results(
    *,
    limit_per_feed: int = DEFAULT_FEED_ENTRIES,
    total_limit: int | None = None,
):
    """Yield one :class:`~ingestion.http.FetchResult` per registered feed read.

    Every enabled feed is read up to ``limit_per_feed`` entries. With a
    ``total_limit``, each feed in turn takes an even share of what the feeds
    before it left unused, so a short feed's share passes on instead of
    being lost; a feed whose share has fallen to nothing is not fetched.
    """
    if not 1 <= limit_per_feed <= MAX_FEED_ENTRIES:
        raise ValueError(f"feed entry limit must be between 1 and {MAX_FEED_ENTRIES}")
    if total_limit is not None and total_limit < 1:
        raise ValueError("total feed entry limit must be positive")
    feeds = list(RSS_FEEDS.items())
    remaining = total_limit
    for index, (source, url) in enumerate(feeds):
        batch_limit = limit_per_feed
        if remaining is not None:
            feeds_left = len(feeds) - index
            batch_limit = min(limit_per_feed, -(-remaining // feeds_left))
            if batch_limit == 0:
                continue
        result = fetch_result(
            source,
            lambda: fetch_rss_feed(url, source, limit=batch_limit)[:batch_limit],
            limit=batch_limit,
        )
        if remaining is not None:
            remaining -= len(result.records)
        yield result


def iter_feed_batches(
    *,
    limit_per_feed: int = DEFAULT_FEED_ENTRIES,
    total_limit: int | None = None,
):
    """Yield ``(source, records)`` for each feed that answered as a feed.

    A failed feed is named on stdout and skipped.
    """
    for result in iter_feed_results(
        limit_per_feed=limit_per_feed, total_limit=total_limit,
    ):
        if result.status == FETCH_FAILED:
            print(f"[news_feed] Failed {result.source} ({result.reason}).")
            continue
        yield result.source, result.records


def fetch_all_feeds(*, limit_per_feed: int = DEFAULT_FEED_ENTRIES) -> list:
    """Compatibility helper that materializes all bounded feed batches."""
    results = []
    for _, batch in iter_feed_batches(limit_per_feed=limit_per_feed):
        results.extend(batch)
    return results


def fetch_brave_search(query: str, count: int = 10) -> list[dict]:
    """Fetch news from Brave Search API for a given query.

    Returns a list of leaf dicts in the same format as fetch_all_feeds().
    Returns [] if `BRAVE_SEARCH_API_KEY` (or the legacy `BRAVE_API_KEY`) is not
    configured, or on any bounded-fetch/parse error.
    """
    api_key = get_setting("BRAVE_SEARCH_API_KEY") or get_setting("BRAVE_API_KEY", "")
    if not api_key:
        return []

    try:
        content = fetch_limited(
            "https://api.search.brave.com/res/v1/news/search",
            request_get=requests.get,
            max_bytes=MAX_SEARCH_RESPONSE_BYTES,
            total_timeout=20,
            headers={
                "X-Subscription-Token": api_key,
                "Accept": "application/json",
            },
            params={
                "q": query,
                "count": count,
                "freshness": "pd",
            },
            timeout=10,
        )
        results = json.loads(content.decode("utf-8")).get("results", [])
        entries = []
        for r in results:
            canonical_url = r.get("url", "")
            raw = json.dumps({
                "title": r.get("title", ""),
                "summary": r.get("description", ""),
                "link": canonical_url,
                "published": r.get("age", ""),
            })
            entries.append({
                "source": "brave_search",
                "source_record_id": canonical_url or None,
                "type": "news",
                "raw_text": raw,
                "canonical_url": canonical_url or None,
                "published_at": None,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "confidence": "not-assessed",
                "source_confidence": "not-assessed",
                "geo_lat": None,
                "geo_lon": None,
                "location_name": None,
                "synthetic": False,
            })
        return entries
    except Exception as e:
        print(f"[news_feed] Brave Search failed ({type(e).__name__}).")
        return []
