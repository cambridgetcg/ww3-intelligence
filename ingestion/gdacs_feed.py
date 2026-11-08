"""Bounded GDACS GeoJSON collection adapter."""

from __future__ import annotations

import json
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
MAX_GDACS_EVENTS = 100
DEFAULT_GDACS_EVENTS = 50
DEFAULT_GDACS_DAYS = 3
GDACS_EVENT_TYPES = ("EQ", "TC", "FL", "VO", "DR", "WF")
GDACS_ALERT_LEVELS = ("Green", "Orange", "Red")


def parse_gdacs_feature(feature: dict) -> dict:
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
) -> dict:
    """Ask GDACS's SEARCH list for recent events at every alert level.

    What this selects, as far as it can be seen from outside: GDACS documents
    ``eventlist``, ``alertlevel``, ``fromDate``, ``toDate`` and ``pageSize``
    (at most 100), and orders the list by each event's ``todate``, newest
    first. It has no ``limit`` parameter; the one this adapter used to send
    was ignored, so it received GDACS's default list, which on 2026-11-08 was
    82 Orange and Red events whose ``todate`` reached back a year, none of
    them current.

    ``fromDate`` is a UTC time ``days`` back. That the window is applied to
    ``todate`` is observed, not documented: on 2026-11-08 every event
    returned had a ``todate`` inside it, while some ``fromdate`` values were
    two weeks older. ``toDate`` is left unset; that day the first page was the
    same with it set to the present moment. A full page means more events
    matched than were read: that day a three-day window filled all 100 places
    with events whose ``todate`` fell in the 22 hours before the query, 89 of
    them Green wildfire alerts. Green is GDACS's lowest alert level, not an
    absence of a hazard.
    """
    now = now or datetime.now(timezone.utc)
    start = now.astimezone(timezone.utc) - timedelta(days=days)
    return {
        "eventlist": ";".join(GDACS_EVENT_TYPES),
        "alertlevel": ";".join(GDACS_ALERT_LEVELS),
        "fromDate": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "pageSize": limit,
    }


def fetch_gdacs_events(
    limit: int = DEFAULT_GDACS_EVENTS,
    *,
    days: int = DEFAULT_GDACS_DAYS,
) -> list[dict]:
    if not 1 <= limit <= MAX_GDACS_EVENTS:
        raise ValueError(f"GDACS event limit must be between 1 and {MAX_GDACS_EVENTS}")
    content = fetch_limited(
        GDACS_URL,
        request_get=requests.get,
        max_bytes=MAX_GDACS_RESPONSE_BYTES,
        total_timeout=30,
        params=gdacs_query(limit=limit, days=days),
        timeout=(3.05, 15),
        headers={"User-Agent": "ww3-intelligence/observation-ledger"},
    )
    data = json.loads(content.decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("features"), list):
        raise ValueError("GDACS response must be a GeoJSON feature collection")
    results = []
    for feature in data["features"][:limit]:
        try:
            results.append(parse_gdacs_feature(feature))
        except (KeyError, TypeError, ValueError, IndexError, OverflowError) as exc:
            print(f"[gdacs_feed] Skipped invalid feature ({type(exc).__name__}).")
    return results


__all__ = [
    "DEFAULT_GDACS_DAYS", "DEFAULT_GDACS_EVENTS", "GDACS_ALERT_LEVELS",
    "GDACS_EVENT_TYPES", "GDACS_URL", "MAX_GDACS_EVENTS",
    "fetch_gdacs_events", "gdacs_query", "parse_gdacs_feature",
]
