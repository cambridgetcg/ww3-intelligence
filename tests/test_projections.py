import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from core.observations import get_or_create_observation
from core.projections import (
    MAX_FACT_TEXT_CHARS,
    MAX_SUMMARY_CHARS,
    MAX_TITLE_CHARS,
    NEUTRAL_TITLE,
    PROJECTION_VERSION,
    project_observation,
)


def _observation(**overrides):
    leaf = {
        "source": "example-feed",
        "source_record_id": "record-1",
        "type": "news",
        "raw_text": json.dumps({"title": "Source title", "summary": "Source summary"}),
        "canonical_url": "https://example.test/record-1",
        "published_at": "2026-07-17T09:15:00Z",
        "fetched_at": "2026-07-17T09:20:00Z",
    }
    leaf.update(overrides)
    return get_or_create_observation(leaf)[0]


@pytest.mark.parametrize("source", ["reuters_world", "brave_search"])
def test_news_projection_preserves_title_and_bounds_summary(source):
    title = "  Exact source title  "
    summary = "s" * (MAX_SUMMARY_CHARS + 17)
    observation = _observation(
        source=source,
        raw_text=json.dumps({
            "title": title,
            "summary": summary,
            "link": "https://example.test/story",
        }),
    )

    projection = project_observation(observation)
    value = projection.as_dict()

    assert projection.title == title
    assert projection.title_truncated is False
    assert projection.summary == summary[:MAX_SUMMARY_CHARS]
    assert projection.summary_truncated is True
    assert value["facts"] == {}
    assert value["truncated_fact_fields"] == []
    assert value["observation"]["source"] == source
    assert value["observation"]["canonical_url"] == "https://example.test/record-1"


def test_usgs_projection_extracts_only_exact_structured_facts():
    raw = {
        "title": "M 4.3 - Test Region",
        "mag": 4.3,
        "depth_km": 12.5,
        "event_type": "earthquake",
        "place": "Test Region",
        # Analytical-looking source keys are deliberately not projected.
        "significance": 10,
        "actors": ["Example"],
        "probability": 99,
    }
    observation = _observation(
        source="usgs",
        source_record_id="usgs-1",
        type="seismic",
        raw_text=json.dumps(raw),
        location_name="Test Region",
        geo_lat=33.5,
        geo_lon=51.9,
        source_confidence="high",
    )

    value = project_observation(observation).as_dict()

    assert value["title"] == raw["title"]
    assert value["summary"] is None
    assert value["facts"] == {
        "mag": 4.3,
        "depth_km": 12.5,
        "event_type": "earthquake",
        "place": "Test Region",
    }
    assert value["observation"]["location_name"] == "Test Region"
    assert value["observation"]["geo_lat"] == 33.5
    assert value["observation"]["geo_lon"] == 51.9


def test_gdacs_projection_exposes_only_source_alert_fields():
    observation = _observation(
        source="gdacs",
        source_record_id="VO:1000144:1",
        type="hazard",
        raw_text=json.dumps({
            "title": "Eruption Mayon",
            "summary": "GDACS alert record.",
            "event_type": "VO",
            "alert_level": "Orange",
            "alert_score": 2,
            "severity": 0,
            "country": "Philippines",
            "is_current": "true",
            "intent": "must not project",
        }),
    )

    value = project_observation(observation).as_dict()

    assert value["title"] == "Eruption Mayon"
    assert value["summary"] == "GDACS alert record."
    assert value["facts"]["event_type"] == "VO"
    assert value["facts"]["alert_level"] == "Orange"
    assert value["facts"]["country"] == "Philippines"
    assert "intent" not in value["facts"]


def test_projection_bounds_all_source_text_and_labels_truncation():
    title = "t" * (MAX_TITLE_CHARS + 1)
    place = "p" * (MAX_FACT_TEXT_CHARS + 1)
    observation = _observation(
        source="usgs",
        type="seismic",
        raw_text=json.dumps({"title": title, "place": place}),
    )

    value = project_observation(observation).as_dict()

    assert value["title"] == title[:MAX_TITLE_CHARS]
    assert value["title_truncated"] is True
    assert value["facts"]["place"] == place[:MAX_FACT_TEXT_CHARS]
    assert value["truncated_fact_fields"] == ["place"]


def test_unstructured_projection_has_neutral_fallback_without_excerpt():
    observation = _observation(
        source="manual-source",
        source_record_id="manual-1",
        type="statement",
        raw_text="not valid JSON and not a derived claim",
    )

    value = project_observation(observation).as_dict()

    assert value["title"] == NEUTRAL_TITLE
    assert value["summary"] is None
    assert value["facts"] == {}
    assert "not valid JSON" not in json.dumps(value)


def test_generic_structured_record_uses_source_title_and_description():
    observation = _observation(
        source="agent-source",
        source_record_id="statement-1",
        type="statement",
        raw_text=json.dumps({
            "headline": "Source-provided statement title",
            "description": "Source-provided statement text.",
        }),
    )

    value = project_observation(observation).as_dict()

    assert value["title"] == "Source-provided statement title"
    assert value["summary"] == "Source-provided statement text."
    assert value["facts"] == {}


def test_projection_contains_provenance_synthetic_and_revision_link():
    first = _observation(
        source="revision-feed",
        source_record_id="stable-id",
        raw_text=json.dumps({"title": "First", "summary": "First version"}),
    )
    revision = _observation(
        source="revision-feed",
        source_record_id="stable-id",
        raw_text=json.dumps({"title": "Correction", "summary": "Corrected version"}),
        synthetic=True,
    )

    value = project_observation(revision).as_dict()
    provenance = value["observation"]

    assert value["projection_version"] == PROJECTION_VERSION
    assert provenance["id"] == revision.id
    assert provenance["dedupe_key"] == revision.dedupe_key
    assert provenance["content_sha256"] == revision.content_hash
    assert provenance["source_record_id"] == "stable-id"
    assert provenance["published_at"] == "2026-07-17T09:15:00Z"
    assert provenance["fetched_at"] == "2026-07-17T09:20:00Z"
    assert provenance["synthetic"] is True
    assert provenance["supersedes_id"] == first.id


def test_projection_is_frozen_stable_and_strict_json_safe():
    leaf = {
        "source": "brave_search",
        "source_record_id": "story-1",
        "type": "news",
        "raw_text": json.dumps({"title": "Original", "summary": "Original summary"}),
    }
    observation, _ = get_or_create_observation(leaf)
    leaf["raw_text"] = json.dumps({"title": "Mutated input", "summary": "Changed"})

    projection = project_observation(observation)
    first = projection.as_dict()
    second = projection.as_dict()

    assert first == second
    assert first is not second
    assert first["title"] == "Original"
    json.dumps(first, allow_nan=False, sort_keys=True)
    with pytest.raises(FrozenInstanceError):
        projection.title = "Changed"


def test_projection_normalizes_aware_timestamps_to_utc():
    from core.db import Observation

    local_time = datetime(
        2026, 7, 17, 12, 0, tzinfo=timezone(timedelta(hours=2))
    )
    observation = Observation.create(
        dedupe_key="aware-time",
        source="test",
        type="news",
        raw_text="{}",
        content_hash="hash",
        fetched_at=local_time,
    )

    assert project_observation(observation).fetched_at == "2026-07-17T10:00:00Z"


def test_projection_contract_has_no_analytical_fields():
    observation = _observation()
    value = project_observation(observation).as_dict()

    def keys(item):
        if isinstance(item, dict):
            result = set(item)
            for child in item.values():
                result.update(keys(child))
            return result
        if isinstance(item, list):
            result = set()
            for child in item:
                result.update(keys(child))
            return result
        return set()

    forbidden = {
        "significance", "category", "actors", "credibility", "risk",
        "probability", "classification", "escalation", "impact", "scenarios",
    }
    assert keys(value).isdisjoint(forbidden)


def test_projection_rejects_unsaved_or_non_observation_inputs():
    from core.db import Observation

    with pytest.raises(TypeError, match="stored Observation"):
        project_observation({"raw_text": "not an observation"})

    unsaved = Observation(
        dedupe_key="d",
        source="test",
        type="news",
        raw_text="{}",
        content_hash="h",
        fetched_at="2026-07-17T00:00:00",
    )
    with pytest.raises(ValueError, match="stored before projection"):
        project_observation(unsaved)
