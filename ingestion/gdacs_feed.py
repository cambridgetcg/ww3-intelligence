"""Bounded GDACS GeoJSON collection adapter.

Every read asks GDACS's SEARCH list two questions, one page each, and keeps
each record once (see :data:`GDACS_QUERIES`). A record's identity and stored
text come from the event alone, never from the question that returned it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

from core.registry import get_source_profile
from ingestion.http import fetch_limited


_PROFILE = get_source_profile("gdacs")
if _PROFILE is None:  # Registry validation should make this impossible.
    raise RuntimeError("GDACS source profile is missing")

GDACS_URL = _PROFILE.endpoint
MAX_GDACS_RESPONSE_BYTES = 5 * 1024 * 1024
# GDACS serves at most 100 events a page, so this is also the server's bound.
# It bounds each query, so one read holds at most twice this many records.
MAX_GDACS_EVENTS = 100
DEFAULT_GDACS_EVENTS = 50
DEFAULT_GDACS_DAYS = 3
GDACS_ORANGE_RED_DAYS = 30
GDACS_EVENT_TYPES = ("EQ", "TC", "FL", "VO", "DR", "WF")
GDACS_ALERT_LEVELS = ("Green", "Orange", "Red")
GDACS_ORANGE_RED_LEVELS = ("Orange", "Red")


def parse_gdacs_feature(feature: dict) -> dict:
    """One GDACS event as a leaf, built from the feature alone.

    Nothing here may depend on which query returned the feature: the same
    event read by the other query must keep the same ``source_record_id`` and
    ``raw_text``, or it would be filed as a revision of itself.
    """
    if not isinstance(feature, dict):
        raise TypeError("GDACS feature must be an object")
    properties = feature.get("properties")
    geometry = feature.get("geometry")
    if not isinstance(properties, dict) or not isinstance(geometry, dict):
        raise TypeError("GDACS properties and geometry must be objects")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
        raise ValueError("GDACS point coordinates are required")

    event_id = properties.get("eventid")
    event_type = properties.get("eventtype")
    episode_id = properties.get("episodeid")
    if event_id is None or not event_type:
        raise ValueError("GDACS event identity is required")
    source_record_id = f"{event_type}:{event_id}:{episode_id or 0}"
    urls = properties.get("url")
    canonical_url = urls.get("report") if isinstance(urls, dict) else None
    severity_data = properties.get("severitydata")
    if not isinstance(severity_data, dict):
        severity_data = {}
    title = properties.get("eventname") or properties.get("name") or source_record_id
    summary = properties.get("description") or properties.get("name")
    raw_text = json.dumps({
        "title": title,
        "summary": summary,
        "event_type": event_type,
        "alert_level": properties.get("alertlevel"),
        "alert_score": properties.get("alertscore"),
        "severity": severity_data.get("severity"),
        "severity_text": severity_data.get("severitytext"),
        "severity_unit": severity_data.get("severityunit"),
        "country": properties.get("country"),
        "affected_countries": properties.get("affectedcountries"),
        "from_date": properties.get("fromdate"),
        "to_date": properties.get("todate"),
        "is_current": properties.get("iscurrent"),
        "upstream_source": properties.get("source"),
    }, ensure_ascii=False)
    return {
        "source": "gdacs",
        "source_record_id": source_record_id,
        "type": "hazard",
        "raw_text": raw_text,
        "canonical_url": canonical_url,
        "published_at": properties.get("fromdate"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "geo_lat": coordinates[1],
        "geo_lon": coordinates[0],
        "location_name": properties.get("country") or properties.get("name"),
        "confidence": "not-assessed",
        "source_confidence": "not-assessed",
        "synthetic": False,
    }


def gdacs_query(
    *,
    limit: int,
    days: int = DEFAULT_GDACS_DAYS,
    now: datetime | None = None,
    alert_levels: tuple[str, ...] = GDACS_ALERT_LEVELS,
) -> dict:
    """One page of GDACS's SEARCH list: these alert levels, ``days`` back.

    What this selects, as far as it can be seen from outside: GDACS documents
    ``eventlist``, ``alertlevel``, ``fromDate``, ``toDate``, ``pageSize`` (at
    most 100) and ``pageNumber``, and orders the list by each event's
    ``todate``, newest first. It has no ``limit`` parameter; the one this
    adapter used to send was ignored, so it received GDACS's default list,
    which on 2026-11-08 was 82 Orange and Red events whose ``todate`` reached
    back a year, none of them current.

    ``fromDate`` is a UTC time ``days`` back. That the window is applied to
    ``todate`` is observed, not documented: on 2026-11-08 and 2026-11-09 every
    event returned had a ``todate`` inside it, while ``fromdate`` values
    reached back weeks, and for a drought nearly a year. ``toDate`` is left
    unset; on 2026-11-08 the first page was the same with it set to the
    present moment. ``alertlevel`` matched the event's overall level, which
    can be higher than its latest episode's: on 2026-11-09 the Orange and Red
    query returned four tropical cyclones whose latest episode was Orange or
    Green. Only the first page is read, so a page holding ``limit`` events
    means GDACS may hold more matches than were read.
    """
    now = now or datetime.now(timezone.utc)
    start = now.astimezone(timezone.utc) - timedelta(days=days)
    return {
        "eventlist": ";".join(GDACS_EVENT_TYPES),
        "alertlevel": ";".join(alert_levels),
        "fromDate": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "pageSize": limit,
    }


@dataclass(frozen=True)
class GdacsQuery:
    """One question a read asks GDACS: alert levels over a window of end dates."""

    name: str
    alert_levels: tuple[str, ...]
    days: int

    def params(self, *, limit: int, now: datetime) -> dict:
        return gdacs_query(
            limit=limit, days=self.days, now=now, alert_levels=self.alert_levels,
        )

    def describe(self) -> str:
        levels = (
            "every alert level" if self.alert_levels == GDACS_ALERT_LEVELS
            else " and ".join(self.alert_levels)
        )
        return f"{levels}, end date in the last {self.days} days"


# The two questions, asked and merged in this order.
#
# ``recent`` is the newest page at every level. On 2026-11-08 a three-day
# window filled all 100 places with events whose ``todate`` fell in the 22
# hours before the query, 89 of them Green wildfire alerts; a second page held
# 100 more, all Green; and three Orange drought alerts with a ``todate`` inside
# the window were on neither page. On 2026-11-09 the page was again 100 Green
# events, every one current, each with a ``todate`` on the previous day. So
# it covers about the newest day, not three, and is nearly always full. Green
# is GDACS's lowest alert level, not an absence of a hazard.
#
# ``orange-red`` reads the higher alerts that page leaves out. On 2026-11-09 it
# held 10 events (7 Orange, 3 Red: five tropical cyclones, three droughts, an
# earthquake and a volcano), none of them current, with a ``todate`` between
# 30 and three days back. The three droughts were Orange with a ``todate``
# three days back, likely the three the ``recent`` pages missed the day before
# (their identities were not compared). Nothing was on both pages. A full page
# here would mean GDACS may hold Orange and Red alerts that went unread.
RECENT_QUERY = GdacsQuery("recent", GDACS_ALERT_LEVELS, DEFAULT_GDACS_DAYS)
ORANGE_RED_QUERY = GdacsQuery(
    "orange-red", GDACS_ORANGE_RED_LEVELS, GDACS_ORANGE_RED_DAYS,
)
GDACS_QUERIES = (RECENT_QUERY, ORANGE_RED_QUERY)


@dataclass(frozen=True)
class GdacsPage:
    """What one query's page held."""

    query: GdacsQuery
    # The ``pageSize`` asked for, and the client-side cut.
    limit: int
    # Features GDACS sent, before the cut and before parsing.
    served: int
    # Parsed leaves in GDACS's order; malformed features are skipped.
    records: tuple[dict, ...]

    @property
    def full(self) -> bool:
        """The page reached its bound, so GDACS may hold more matches."""
        return self.served >= self.limit


def _fetch_page(query: GdacsQuery, *, limit: int, now: datetime) -> GdacsPage:
    content = fetch_limited(
        GDACS_URL,
        request_get=requests.get,
        max_bytes=MAX_GDACS_RESPONSE_BYTES,
        total_timeout=30,
        params=query.params(limit=limit, now=now),
        timeout=(3.05, 15),
        headers={"User-Agent": "ww3-intelligence/observation-ledger"},
    )
    data = json.loads(content.decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("features"), list):
        raise ValueError("GDACS response must be a GeoJSON feature collection")
    records = []
    for feature in data["features"][:limit]:
        try:
            records.append(parse_gdacs_feature(feature))
        except (KeyError, TypeError, ValueError, IndexError, OverflowError) as exc:
            print(f"[gdacs_feed] Skipped invalid feature ({type(exc).__name__}).")
    return GdacsPage(query, limit, len(data["features"]), tuple(records))


def fetch_gdacs_pages(
    limit: int = DEFAULT_GDACS_EVENTS,
    *,
    queries: tuple[GdacsQuery, ...] = GDACS_QUERIES,
    now: datetime | None = None,
) -> tuple[GdacsPage, ...]:
    """Read the first page of each query, at most ``limit`` events each.

    One GET per query, every window ending at the same moment. A failed GET
    fails the whole read: no page is returned, so a read never looks
    complete with one question missing.
    """
    if not 1 <= limit <= MAX_GDACS_EVENTS:
        raise ValueError(f"GDACS event limit must be between 1 and {MAX_GDACS_EVENTS}")
    now = now or datetime.now(timezone.utc)
    return tuple(_fetch_page(query, limit=limit, now=now) for query in queries)


def merge_gdacs_pages(pages) -> list[dict]:
    """Every record once, in page order, by ``source_record_id``.

    An event both queries returned is one record; the first copy is kept. The
    identity names the episode, so a new episode of an event is a new record,
    as it would be in the next run.
    """
    seen = set()
    merged = []
    for page in pages:
        for record in page.records:
            if record["source_record_id"] in seen:
                continue
            seen.add(record["source_record_id"])
            merged.append(record)
    return merged


def fetch_gdacs_events(
    limit: int = DEFAULT_GDACS_EVENTS,
    *,
    queries: tuple[GdacsQuery, ...] = GDACS_QUERIES,
) -> list[dict]:
    """Both queries' pages merged: at most ``limit`` from each, each event once."""
    return merge_gdacs_pages(fetch_gdacs_pages(limit, queries=queries))


__all__ = [
    "DEFAULT_GDACS_DAYS", "DEFAULT_GDACS_EVENTS", "GDACS_ALERT_LEVELS",
    "GDACS_EVENT_TYPES", "GDACS_ORANGE_RED_DAYS", "GDACS_ORANGE_RED_LEVELS",
    "GDACS_QUERIES", "GDACS_URL", "GdacsPage", "GdacsQuery",
    "MAX_GDACS_EVENTS", "ORANGE_RED_QUERY", "RECENT_QUERY",
    "fetch_gdacs_events", "fetch_gdacs_pages", "gdacs_query",
    "merge_gdacs_pages", "parse_gdacs_feature",
]
