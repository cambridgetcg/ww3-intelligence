"""Bounded GDACS GeoJSON collection adapter."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import requests

from core.registry import get_source_profile
from ingestion.http import fetch_limited


_PROFILE = get_source_profile("gdacs")
if _PROFILE is None:  # Registry validation should make this impossible.
    raise RuntimeError("GDACS source profile is missing")

GDACS_URL = _PROFILE.endpoint
MAX_GDACS_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_GDACS_EVENTS = 100
DEFAULT_GDACS_EVENTS = 50


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


def fetch_gdacs_events(limit: int = DEFAULT_GDACS_EVENTS) -> list[dict]:
    if not 1 <= limit <= MAX_GDACS_EVENTS:
        raise ValueError(f"GDACS event limit must be between 1 and {MAX_GDACS_EVENTS}")
    content = fetch_limited(
        GDACS_URL,
        request_get=requests.get,
        max_bytes=MAX_GDACS_RESPONSE_BYTES,
        total_timeout=30,
        params={"limit": limit},
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
    "DEFAULT_GDACS_EVENTS", "GDACS_URL", "MAX_GDACS_EVENTS",
    "fetch_gdacs_events", "parse_gdacs_feature",
]
