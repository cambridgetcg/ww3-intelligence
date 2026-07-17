import json
import requests
from datetime import datetime, timedelta, timezone

from ingestion.http import fetch_limited


USGS_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
MAX_USGS_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_USGS_EVENTS = 1_000
DEFAULT_USGS_EVENTS = 200


def fetch_seismic_events(
    min_magnitude: float = 4.0,
    days: int = 7,
    limit: int = DEFAULT_USGS_EVENTS,
) -> list:
    if not 1 <= limit <= MAX_USGS_EVENTS:
        raise ValueError(f"USGS event limit must be between 1 and {MAX_USGS_EVENTS}")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    params = {
        "format": "geojson",
        "starttime": start.strftime("%Y-%m-%d"),
        "endtime": end.strftime("%Y-%m-%d"),
        "minmagnitude": min_magnitude,
        "orderby": "time",
        "limit": limit,
    }
    content = fetch_limited(
        USGS_URL,
        request_get=requests.get,
        max_bytes=MAX_USGS_RESPONSE_BYTES,
        total_timeout=30,
        params=params,
        timeout=(3.05, 15),
    )
    data = json.loads(content.decode("utf-8"))
    results = []
    for feature in data.get("features", [])[:limit]:
        try:
            results.append(parse_usgs_feature(feature))
        except (KeyError, TypeError, ValueError, IndexError, OverflowError, OSError) as exc:
            print(f"[seismic_feed] Skipped invalid feature ({type(exc).__name__}).")
    return results


def parse_usgs_feature(feature: dict) -> dict:
    if not isinstance(feature, dict):
        raise TypeError("USGS feature must be an object")
    props = feature["properties"]
    geometry = feature["geometry"]
    if not isinstance(props, dict) or not isinstance(geometry, dict):
        raise TypeError("USGS properties and geometry must be objects")
    coords = geometry["coordinates"]
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        raise ValueError("USGS coordinates must contain longitude and latitude")
    source_record_id = feature["id"]
    canonical_url = props.get("url") or (
        f"https://earthquake.usgs.gov/earthquakes/eventpage/{source_record_id}"
    )
    event_time_ms = props.get("time")
    published_at = (
        datetime.fromtimestamp(event_time_ms / 1000, tz=timezone.utc).isoformat()
        if event_time_ms is not None else None
    )
    return {
        "source": "usgs",
        "source_record_id": source_record_id,
        "type": "seismic",
        "raw_text": json.dumps({
            "id": source_record_id,
            "mag": props.get("mag"),
            "place": props.get("place"),
            "depth_km": coords[2] if len(coords) > 2 else None,
            "time_ms": event_time_ms,
            "event_type": props.get("type"),
            "title": props.get("title"),
        }),
        "canonical_url": canonical_url,
        "published_at": published_at,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "geo_lat": coords[1],
        "geo_lon": coords[0],
        "location_name": props.get("place"),
        "confidence": "not-assessed",
        "source_confidence": "not-assessed",
        "synthetic": False,
    }
