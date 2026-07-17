import json
from collections.abc import Mapping
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import feedparser
import requests

from core.registry import get_source_profile, rss_source_profiles
from core.settings import get_setting
from ingestion.http import fetch_limited


RSS_FEEDS = {item.source_id: item.endpoint for item in rss_source_profiles()}

MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_FEED_ENTRIES = 500
DEFAULT_FEED_ENTRIES = 100
MAX_ENTRY_SUMMARY_CHARS = 100_000
MAX_ENTRY_CONTENT_CHARS = 100_000
MAX_ENTRY_CONTENT_PARTS = 16
MAX_SEARCH_RESPONSE_BYTES = 5 * 1024 * 1024

_HTTPS_CANONICAL_HOSTS = {
    "iaea_topnews": {"iaea.org", "www.iaea.org"},
    "russia_mfa": {"mid.ru", "www.mid.ru"},
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


def fetch_rss_feed(
    url: str,
    source: str,
    *,
    limit: int = DEFAULT_FEED_ENTRIES,
) -> list:
    if not 1 <= limit <= MAX_FEED_ENTRIES:
        raise ValueError(f"feed entry limit must be between 1 and {MAX_FEED_ENTRIES}")
    content = fetch_limited(
        url,
        request_get=requests.get,
        max_bytes=MAX_FEED_BYTES,
        total_timeout=30,
        headers={"User-Agent": "ww3-intelligence/observation-ledger"},
        timeout=(3.05, 15),
    )
    feed = feedparser.parse(content)
    results = []
    for entry in feed.entries[:limit]:
        try:
            parsed = parse_rss_entry(entry, source)
        except (TypeError, ValueError, OverflowError) as exc:
            print(f"[news_feed] Skipped invalid {source} entry ({type(exc).__name__}).")
            continue
        if parsed:
            results.append(parsed)
    return results


def parse_rss_entry(entry, source: str) -> dict:
    title = entry.get("title", "")
    summary = entry.get("summary", entry.get("description", ""))
    title = str(title)[:1_000]
    summary = str(summary)[:MAX_ENTRY_SUMMARY_CHARS]
    canonical_url = _canonical_link(entry.get("link"), source)
    published_at = entry.get("published") or entry.get("updated")
    # A few official feeds publish two-digit years (for example 26-07-15).
    # feedparser exposes a normalized tuple even when the original string is
    # not understood by our generic timestamp normalizer.
    parsed_time = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed_time and len(parsed_time) >= 6:
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


def iter_feed_batches(
    *,
    limit_per_feed: int = DEFAULT_FEED_ENTRIES,
    total_limit: int | None = None,
):
    """Yield bounded registered RSS batches, fairly splitting a total limit."""
    if not 1 <= limit_per_feed <= MAX_FEED_ENTRIES:
        raise ValueError(f"feed entry limit must be between 1 and {MAX_FEED_ENTRIES}")
    if total_limit is not None and total_limit < 1:
        raise ValueError("total feed entry limit must be positive")
    feeds = list(RSS_FEEDS.items())
    if total_limit is None:
        limits = [limit_per_feed] * len(feeds)
    else:
        base, extra = divmod(total_limit, len(feeds)) if feeds else (0, 0)
        limits = [
            min(limit_per_feed, base + (1 if index < extra else 0))
            for index in range(len(feeds))
        ]
    for (source, url), batch_limit in zip(feeds, limits):
        if batch_limit == 0:
            continue
        try:
            batch = fetch_rss_feed(url, source, limit=batch_limit)
            yield source, batch[:batch_limit]
        except Exception as e:
            print(f"[news_feed] Failed {source} ({type(e).__name__}).")


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
