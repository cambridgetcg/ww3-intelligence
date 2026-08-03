"""Normalize legacy leaves into immutable, deduplicated observations."""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from peewee import IntegrityError

from core.db import Observation
from core.text_safety import contains_unsafe_controls


UTC = timezone.utc
MAX_RAW_TEXT_BYTES = 2 * 1024 * 1024
CONTENT_EQUIVALENCE_VERSION = "ww3.content-equivalence/v1"

__all__ = [
    "CONTENT_EQUIVALENCE_VERSION",
    "compute_content_equivalence_key",
    "compute_content_hash",
    "compute_dedupe_key",
    "get_or_create_observation",
    "normalize_leaf",
    "observation_identity",
    "parse_timestamp",
]


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _as_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def parse_timestamp(value: Any) -> datetime | None:
    """Parse common feed timestamps into a naive UTC ``datetime``.

    Invalid or empty values return ``None``. Unix timestamps in seconds,
    milliseconds, microseconds, or nanoseconds are accepted.
    """

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return _as_utc_naive(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            return None
        seconds = float(value)
        while abs(seconds) >= 100_000_000_000:
            seconds /= 1000
        try:
            return datetime.fromtimestamp(seconds, UTC).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        try:
            parts = [int(part) for part in value[:6]]
            parts.extend([0] * (6 - len(parts)))
            return datetime(*parts)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    try:
        return parse_timestamp(float(text))
    except ValueError:
        pass

    iso_text = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        return _as_utc_naive(datetime.fromisoformat(iso_text))
    except ValueError:
        pass

    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    return _as_utc_naive(parsed)


def _stable_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_content_hash(raw_text: Any) -> str:
    """Return the SHA-256 hash of the exact text stored as source material."""

    # Hashes the bytes a publisher served, unnormalized. Every stored
    # observation was keyed this way, so altering this function would make each
    # unchanged record fetched afterwards look like a new content revision.
    return hashlib.sha256(_stable_text(raw_text).encode("utf-8")).hexdigest()


def compute_content_equivalence_key(raw_text: Any) -> str:
    """Return a canonical-equivalence digest of text that is stored verbatim.

    This reports only that two captured texts render as the same characters
    under Unicode canonical equivalence. It is not identity: it must never key
    deduplication, revision linkage, or storage, and it is never compared with
    ``content_hash``.
    """

    # Domain-separated so the two digests cannot collide even for text already
    # composed, because a value that could be mistaken for a content hash could
    # be substituted for one.
    digest = hashlib.sha256(CONTENT_EQUIVALENCE_VERSION.encode("utf-8") + b"\x00")
    digest.update(
        unicodedata.normalize("NFC", _stable_text(raw_text)).encode("utf-8")
    )
    return digest.hexdigest()


def compute_dedupe_key(
    *,
    source: str,
    type: str,
    content_hash: str,
    source_record_id: str | None = None,
) -> str:
    """Return a versioned deterministic identity hash for an observation."""

    if source_record_id:
        identity = [
            "observation-v2", "source-record-revision", source,
            source_record_id, content_hash,
        ]
    else:
        identity = ["observation-v2", "content", source, type, content_hash]
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _first_value(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return None


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _contains_control(value: str) -> bool:
    return contains_unsafe_controls(value)


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _canonicalize_url(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    if _contains_control(text):
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    default_port = (parsed.scheme.lower() == "http" and port == 80) or (
        parsed.scheme.lower() == "https" and port == 443
    )
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None and not default_port:
        netloc = f"{netloc}:{port}"
    sensitive_keys = {
        "auth", "authorization", "cookie", "credential", "key", "password",
        "secret", "signature", "signed", "token",
    }
    safe_query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in {"fbclid", "gclid"}:
            continue
        key_parts = {part for part in lowered.replace("-", "_").split("_") if part}
        if key_parts & sensitive_keys:
            continue
        safe_query.append((key, value))
    return urlunsplit(
        (parsed.scheme.lower(), netloc, parsed.path, urlencode(safe_query, doseq=True), "")
    )


def _raw_metadata(raw_text: str) -> Mapping[str, Any]:
    try:
        parsed = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def normalize_leaf(
    leaf: Mapping[str, Any],
    *,
    fetched_at: Any = None,
    synthetic: bool | None = None,
) -> dict[str, Any]:
    """Map legacy ingestion leaf shapes to fields accepted by ``Observation``."""

    source = _optional_text(leaf.get("source"))
    leaf_type = _optional_text(leaf.get("type"))
    if source is None:
        raise ValueError("leaf source is required")
    if leaf_type is None:
        raise ValueError("leaf type is required")
    if "raw_text" not in leaf:
        raise ValueError("leaf raw_text is required")

    source = source.lower()
    leaf_type = leaf_type.lower()
    if len(source) > 200:
        raise ValueError("leaf source exceeds 200 characters")
    if len(leaf_type) > 100:
        raise ValueError("leaf type exceeds 100 characters")
    if _contains_control(source) or _contains_control(leaf_type):
        raise ValueError("leaf source/type contains unsupported control characters")
    # Stored verbatim: a casefile quotes exact visible passages and reports
    # offsets into them, so rewriting a publisher's characters to any
    # normalization form would corrupt the evidence and move every span.
    raw_text = _stable_text(leaf["raw_text"])
    if len(raw_text.encode("utf-8")) > MAX_RAW_TEXT_BYTES:
        raise ValueError(f"leaf raw_text exceeds {MAX_RAW_TEXT_BYTES} bytes")
    raw_metadata = _raw_metadata(raw_text)

    source_record_id = _optional_text(
        _coalesce(
            _first_value(leaf, "source_record_id", "external_id"),
            _first_value(raw_metadata, "source_record_id", "external_id", "id"),
        )
    )
    if source_record_id and "://" in source_record_id:
        source_record_id = _canonicalize_url(source_record_id)
    if source_record_id and len(source_record_id) > 2_000:
        raise ValueError("leaf source_record_id exceeds 2,000 characters")
    if source_record_id and _contains_control(source_record_id):
        raise ValueError("leaf source_record_id contains unsupported control characters")
    canonical_url = _canonicalize_url(
        _coalesce(
            _first_value(leaf, "canonical_url", "source_url", "url", "link"),
            _first_value(raw_metadata, "canonical_url", "source_url", "url", "link"),
        )
    )
    if canonical_url and len(canonical_url) > 2_000:
        raise ValueError("leaf canonical_url exceeds 2,000 characters")
    published_at = parse_timestamp(
        _coalesce(
            _first_value(leaf, "published_at", "published", "date_published"),
            _first_value(
                raw_metadata, "published_at", "published", "date_published", "time_ms"
            ),
        )
    )
    normalized_fetched_at = parse_timestamp(
        fetched_at
        if fetched_at is not None
        else _first_value(leaf, "fetched_at", "fetched", "ingested_at", "ts")
    ) or _utcnow()

    geo = leaf.get("geo")
    if not isinstance(geo, Mapping):
        geo = {}
    content_hash = compute_content_hash(raw_text)
    normalized = {
        "source": source,
        "source_record_id": source_record_id,
        "type": leaf_type,
        "raw_text": raw_text,
        "content_hash": content_hash,
        "canonical_url": canonical_url,
        "published_at": published_at,
        "fetched_at": normalized_fetched_at,
        "geo_lat": _optional_float(
            _coalesce(
                _first_value(leaf, "geo_lat", "lat", "latitude"),
                _first_value(geo, "lat", "latitude"),
            )
        ),
        "geo_lon": _optional_float(
            _coalesce(
                _first_value(leaf, "geo_lon", "lon", "longitude"),
                _first_value(geo, "lon", "longitude"),
            )
        ),
        "location_name": _optional_text(
            _coalesce(
                _first_value(leaf, "location_name", "location"),
                _first_value(geo, "location_name", "name"),
            )
        ),
        "source_confidence": (
            _optional_text(_first_value(leaf, "source_confidence", "confidence"))
            or "not-assessed"
        ).lower(),
        "synthetic": _as_bool(leaf.get("synthetic", False) if synthetic is None else synthetic),
    }
    if normalized["source_confidence"] not in {
        "low", "medium", "high", "not-assessed",
    }:
        raise ValueError(
            "leaf source_confidence must be low, medium, high, or not-assessed"
        )
    if normalized["location_name"] and len(normalized["location_name"]) > 500:
        raise ValueError("leaf location_name exceeds 500 characters")
    if normalized["location_name"] and _contains_control(normalized["location_name"]):
        raise ValueError("leaf location_name contains unsupported control characters")
    normalized["dedupe_key"] = compute_dedupe_key(
        source=source,
        type=leaf_type,
        content_hash=content_hash,
        source_record_id=source_record_id,
    )
    return normalized


def observation_identity(leaf_or_normalized: Mapping[str, Any]) -> str:
    """Return the deterministic dedupe identity for a leaf or normalized fields."""

    return normalize_leaf(leaf_or_normalized)["dedupe_key"]


def get_or_create_observation(
    leaf: Mapping[str, Any],
    *,
    fetched_at: Any = None,
    synthetic: bool | None = None,
) -> tuple[Observation, bool]:
    """Return the existing immutable observation or atomically insert a new one."""

    fields = normalize_leaf(leaf, fetched_at=fetched_at, synthetic=synthetic)
    dedupe_key = fields["dedupe_key"]
    existing = Observation.get_or_none(Observation.dedupe_key == dedupe_key)
    if existing is not None:
        return existing, False

    try:
        with Observation._meta.database.atomic():
            if fields["source_record_id"]:
                fields["supersedes"] = (
                    Observation.select()
                    .where(
                        (Observation.source == fields["source"])
                        & (Observation.source_record_id == fields["source_record_id"])
                    )
                    .order_by(Observation.created_at.desc(), Observation.id.desc())
                    .first()
                )
            return Observation.create(**fields), True
    except IntegrityError:
        return Observation.get(Observation.dedupe_key == dedupe_key), False
