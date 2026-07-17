"""Pure, deterministic views over stored source observations.

The projection layer makes captured records useful without treating them as
analysis.  It reads only immutable ``Observation`` fields, performs no I/O,
and exposes source-provided text and structured facts without assigning
significance, credibility, risk, probability, or geopolitical meaning.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from core.db import Observation


PROJECTION_VERSION = "observation-projection-v2"
MAX_TITLE_CHARS = 1_000
MAX_SUMMARY_CHARS = 2_000
MAX_FACT_TEXT_CHARS = 2_000
NEUTRAL_TITLE = "Unstructured source observation"

_JsonScalar = str | int | float | bool | None


def _isoformat(value: datetime | date | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    return value.isoformat()


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _json_scalar(value: Any) -> _JsonScalar:
    """Keep only strict-JSON scalar values from structured source data."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return None


def _source_text(value: Any, maximum: int) -> tuple[str | None, bool]:
    """Return a bounded non-empty source string and its truncation state."""

    if not isinstance(value, str) or not value.strip():
        return None, False
    return value[:maximum], len(value) > maximum


def _bounded_scalar(value: Any) -> tuple[_JsonScalar, bool]:
    scalar = _json_scalar(value)
    if isinstance(scalar, str) and len(scalar) > MAX_FACT_TEXT_CHARS:
        return scalar[:MAX_FACT_TEXT_CHARS], True
    return scalar, False


def _raw_mapping(observation: Observation) -> dict[str, Any]:
    try:
        value = json.loads(observation.raw_text)
    except (TypeError, ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True, slots=True)
class ObservationProjection:
    """A versioned, JSON-safe, non-analytical view of one Observation."""

    observation_id: int
    dedupe_key: str
    content_sha256: str
    source: str
    source_record_id: str | None
    record_type: str
    canonical_url: str | None
    published_at: str | None
    fetched_at: str
    ledger_created_at: str
    location_name: str | None
    geo_lat: float | None
    geo_lon: float | None
    synthetic: bool
    supersedes_id: int | None
    title: str
    title_truncated: bool
    summary: str | None
    summary_truncated: bool
    facts: tuple[tuple[str, _JsonScalar], ...] = ()
    truncated_fact_fields: tuple[str, ...] = ()
    projection_version: str = PROJECTION_VERSION

    def as_dict(self) -> dict[str, Any]:
        """Return a fresh dictionary with a stable, strict-JSON-safe shape."""

        return {
            "projection_version": self.projection_version,
            "observation": {
                "id": self.observation_id,
                "dedupe_key": self.dedupe_key,
                "content_sha256": self.content_sha256,
                "source": self.source,
                "source_record_id": self.source_record_id,
                "record_type": self.record_type,
                "canonical_url": self.canonical_url,
                "published_at": self.published_at,
                "fetched_at": self.fetched_at,
                "ledger_created_at": self.ledger_created_at,
                "location_name": self.location_name,
                "geo_lat": self.geo_lat,
                "geo_lon": self.geo_lon,
                "synthetic": self.synthetic,
                "supersedes_id": self.supersedes_id,
            },
            "title": self.title,
            "title_truncated": self.title_truncated,
            "summary": self.summary,
            "summary_truncated": self.summary_truncated,
            "facts": {key: value for key, value in self.facts},
            "truncated_fact_fields": list(self.truncated_fact_fields),
        }


def project_observation(observation: Observation) -> ObservationProjection:
    """Project one stored observation without model calls or external I/O."""

    if not isinstance(observation, Observation):
        raise TypeError("observation must be a stored Observation")
    if observation.id is None:
        raise ValueError("observation must be stored before projection")

    raw = _raw_mapping(observation)
    source = observation.source
    record_type = observation.type
    source_key = source.lower()

    title = NEUTRAL_TITLE
    title_truncated = False
    summary = None
    summary_truncated = False
    facts: tuple[tuple[str, _JsonScalar], ...] = ()
    truncated_fact_fields: tuple[str, ...] = ()

    if source_key == "usgs":
        source_title, title_truncated = _source_text(raw.get("title"), MAX_TITLE_CHARS)
        title = source_title or NEUTRAL_TITLE
        fact_values = []
        truncated = []
        for field in ("mag", "depth_km", "event_type", "place"):
            scalar, was_truncated = _bounded_scalar(raw.get(field))
            fact_values.append((field, scalar))
            if was_truncated:
                truncated.append(field)
        facts = tuple(fact_values)
        truncated_fact_fields = tuple(truncated)
    elif source_key == "gdacs":
        source_title, title_truncated = _source_text(raw.get("title"), MAX_TITLE_CHARS)
        title = source_title or NEUTRAL_TITLE
        summary, summary_truncated = _source_text(raw.get("summary"), MAX_SUMMARY_CHARS)
        fact_values = []
        truncated = []
        for field in (
            "event_type", "alert_level", "alert_score", "severity",
            "severity_text", "severity_unit", "country", "from_date",
            "to_date", "is_current", "upstream_source",
        ):
            scalar, was_truncated = _bounded_scalar(raw.get(field))
            fact_values.append((field, scalar))
            if was_truncated:
                truncated.append(field)
        facts = tuple(fact_values)
        truncated_fact_fields = tuple(truncated)
    else:
        source_title, title_truncated = _source_text(raw.get("title"), MAX_TITLE_CHARS)
        if source_title is None:
            source_title, title_truncated = _source_text(
                raw.get("headline"), MAX_TITLE_CHARS
            )
        title = source_title or NEUTRAL_TITLE
        source_summary, summary_truncated = _source_text(
            raw.get("summary"), MAX_SUMMARY_CHARS
        )
        if source_summary is None:
            source_summary, summary_truncated = _source_text(
                raw.get("description"), MAX_SUMMARY_CHARS
            )
        if source_summary is not None:
            summary = source_summary

    return ObservationProjection(
        observation_id=observation.id,
        dedupe_key=observation.dedupe_key,
        content_sha256=observation.content_hash,
        source=source,
        source_record_id=observation.source_record_id,
        record_type=record_type,
        canonical_url=observation.canonical_url,
        published_at=_isoformat(observation.published_at),
        fetched_at=_isoformat(observation.fetched_at) or "",
        ledger_created_at=_isoformat(observation.created_at) or "",
        location_name=observation.location_name,
        geo_lat=_finite_number(observation.geo_lat),
        geo_lon=_finite_number(observation.geo_lon),
        synthetic=bool(observation.synthetic),
        supersedes_id=observation.supersedes_id,
        title=title,
        title_truncated=title_truncated,
        summary=summary,
        summary_truncated=summary_truncated,
        facts=facts,
        truncated_fact_fields=truncated_fact_fields,
    )


__all__ = [
    "MAX_SUMMARY_CHARS",
    "MAX_TITLE_CHARS",
    "MAX_FACT_TEXT_CHARS",
    "NEUTRAL_TITLE",
    "ObservationProjection",
    "PROJECTION_VERSION",
    "project_observation",
]
