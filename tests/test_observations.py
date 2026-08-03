from datetime import datetime
import json
import unicodedata

import pytest

from peewee import IntegrityError

from core.db import AnalysisRun, Event, Observation, migrate_schema
from core.observations import (
    compute_content_equivalence_key,
    compute_content_hash,
    compute_dedupe_key,
    get_or_create_observation,
    normalize_leaf,
    observation_identity,
)


def _leaf(**overrides):
    leaf = {
        "source": "example-feed",
        "type": "news",
        "raw_text": '{"title":"A verified test report"}',
        "confidence": "medium",
    }
    leaf.update(overrides)
    return leaf


def test_identical_leaf_deduplicates():
    fetched_at = "2026-03-03T12:00:00Z"

    first, first_created = get_or_create_observation(_leaf(), fetched_at=fetched_at)
    second, second_created = get_or_create_observation(_leaf(), fetched_at=fetched_at)

    assert first_created is True
    assert second_created is False
    assert first.id == second.id
    assert Observation.select().count() == 1


def test_observation_identity_accepts_leaf_or_normalized_fields():
    leaf = _leaf(source_record_id="record-1")
    normalized = normalize_leaf(leaf, fetched_at="2026-03-03T12:00:00Z")

    assert observation_identity(leaf) == normalized["dedupe_key"]
    assert observation_identity(normalized) == normalized["dedupe_key"]


def test_unspecified_source_confidence_is_explicitly_not_assessed():
    normalized = normalize_leaf({
        "source": "manual",
        "type": "statement",
        "raw_text": "source text",
    })

    assert normalized["source_confidence"] == "not-assessed"


def test_source_record_ids_distinguish_identical_content():
    first, _ = get_or_create_observation(_leaf(source_record_id="record-1"))
    second, _ = get_or_create_observation(_leaf(source_record_id="record-2"))

    assert first.id != second.id
    assert first.content_hash == second.content_hash
    assert first.dedupe_key != second.dedupe_key


def test_changed_source_record_creates_linked_immutable_revision():
    first, _ = get_or_create_observation(
        _leaf(source_record_id="stable-id", canonical_url="https://example.test/original")
    )
    original_hash = first.content_hash

    revision, created = get_or_create_observation(
        _leaf(
            source_record_id="stable-id",
            raw_text="replacement content",
            canonical_url="https://example.test/replacement",
            confidence="low",
        )
    )

    assert created is True
    assert revision.id != first.id
    assert revision.supersedes == first
    assert revision.raw_text == "replacement content"
    assert revision.canonical_url == "https://example.test/replacement"
    assert revision.source_confidence == "low"
    stored = Observation.get_by_id(first.id)
    assert stored.raw_text == '{"title":"A verified test report"}'
    assert stored.content_hash == original_hash
    assert stored.canonical_url == "https://example.test/original"
    assert stored.source_confidence == "medium"


def test_observation_model_rejects_updates():
    observation, _ = get_or_create_observation(_leaf())
    observation.raw_text = "changed"

    with pytest.raises(ValueError, match="immutable"):
        observation.save()

    assert Observation.get_by_id(observation.id).raw_text != "changed"


def test_database_guards_reject_bulk_update_and_delete():
    observation, _ = get_or_create_observation(_leaf())

    with pytest.raises(IntegrityError, match="immutable"):
        Observation.update(raw_text="changed").where(
            Observation.id == observation.id
        ).execute()
    with pytest.raises(IntegrityError, match="append-only"):
        Observation.delete().where(Observation.id == observation.id).execute()


def test_analysis_run_keeps_mutable_processing_state_separate():
    observation, _ = get_or_create_observation(_leaf())
    run = AnalysisRun.create(
        observation=observation,
        status="running",
        model_name="test-model-v1",
        prompt_version="prompt-v2",
        code_version="code-v3",
    )

    run.status = "completed"
    run.output_json = '{"classification":"reported"}'
    run.completed_at = datetime(2026, 3, 3, 12, 30)
    run.save()

    stored = AnalysisRun.get_by_id(run.id)
    assert stored.observation.id == observation.id
    assert stored.status == "completed"
    assert stored.output_json == '{"classification":"reported"}'
    assert stored.model_name == "test-model-v1"


def test_migration_preserves_duplicate_legacy_attempts_and_keys_canonical_run():
    observation, _ = get_or_create_observation(_leaf(source_record_id="legacy-version"))
    event = Event.create(
        title="Completed derivation", date="2026-03-03", category="political",
        classification="reported", significance=3,
    )
    failed = AnalysisRun.create(
        observation=observation,
        status="failed",
        model_name="legacy-model",
        prompt_version="legacy-prompt",
        code_version="legacy-code",
    )
    completed = AnalysisRun.create(
        observation=observation,
        event=event,
        status="completed",
        model_name="legacy-model",
        prompt_version="legacy-prompt",
        code_version="legacy-code",
    )

    migrate_schema()

    assert AnalysisRun.select().count() == 2
    assert AnalysisRun.get_by_id(completed.id).analysis_key is not None
    assert AnalysisRun.get_by_id(failed.id).analysis_key is None


def test_leaf_metadata_is_normalized_and_parsed():
    raw_text = json.dumps(
        {
            "id": "usgs-123",
            "link": "HTTPS://Example.COM:443/report?id=7#details",
            "time_ms": 1_772_531_100_000,
        }
    )
    normalized = normalize_leaf(
        {
            "source": " USGS ",
            "type": " SEISMIC ",
            "raw_text": raw_text,
            "ts": "2026-03-03T10:15:00+02:00",
            "geo": {"latitude": "33.5", "longitude": 51.9, "name": "Test Region"},
            "source_confidence": "HIGH",
            "synthetic": "true",
        }
    )

    assert normalized["source"] == "usgs"
    assert normalized["type"] == "seismic"
    assert normalized["source_record_id"] == "usgs-123"
    assert normalized["canonical_url"] == "https://example.com/report?id=7"
    assert normalized["published_at"] == datetime(2026, 3, 3, 9, 45)
    assert normalized["fetched_at"] == datetime(2026, 3, 3, 8, 15)
    assert normalized["geo_lat"] == 33.5
    assert normalized["geo_lon"] == 51.9
    assert normalized["location_name"] == "Test Region"
    assert normalized["source_confidence"] == "high"
    assert normalized["synthetic"] is True


def test_canonical_url_rejects_unsafe_schemes_and_redacts_sensitive_query_keys():
    unsafe = normalize_leaf(_leaf(
        source_record_id=None,
        canonical_url="file:///Users/example/private",
    ))
    assert unsafe["canonical_url"] is None

    safe = normalize_leaf(_leaf(
        source_record_id="https://EXAMPLE.test/story?id=7&token=secret&utm_source=x",
        canonical_url="https://EXAMPLE.test/story?id=7&signature=secret&utm_source=x",
    ))
    assert safe["source_record_id"] == "https://example.test/story?id=7"
    assert safe["canonical_url"] == "https://example.test/story?id=7"

    invalid_port = normalize_leaf(_leaf(
        source_record_id=None,
        canonical_url="https://example.test:99999/story?id=7&token=secret",
    ))
    assert invalid_port["canonical_url"] is None


@pytest.mark.parametrize("control", ["\u202e", "\u061c", "\u200e", "\u200f"])
def test_metadata_rejects_bidi_override_controls(control):
    with pytest.raises(ValueError, match="source/type contains unsupported"):
        normalize_leaf(_leaf(source=f"feed{control}exe"))

    with pytest.raises(ValueError, match="source_record_id contains unsupported"):
        normalize_leaf(_leaf(source_record_id=f"record{control}exe"))

    normalized = normalize_leaf(_leaf(
        source_record_id=None,
        canonical_url=f"https://example.test/story{control}exe",
    ))
    assert normalized["canonical_url"] is None


def test_observation_rejects_oversized_source_material(monkeypatch):
    monkeypatch.setattr("core.observations.MAX_RAW_TEXT_BYTES", 3)
    with pytest.raises(ValueError, match="raw_text exceeds"):
        normalize_leaf(_leaf(raw_text="four"))


def test_stored_identity_of_an_ascii_record_is_pinned_to_its_shipped_digests():
    raw_text = '{"title":"A verified test report"}'
    content_hash = compute_content_hash(raw_text)

    normalized = normalize_leaf(_leaf())

    # Pinned, not derived: 4,588 stored observations were keyed by exactly
    # these functions, and a digest that moves makes every unchanged record
    # fetched afterwards look like a new content revision.
    assert content_hash == (
        "af14fb9acc4a7564c860ea10209dcca3c9522d4f4fbcc4246ba414271c4deeec"
    )
    assert compute_dedupe_key(
        source="example-feed", type="news", content_hash=content_hash,
    ) == "fe6e03509d4f8915ea372bf326b349162bf17061ffdb615a95d7d985ad38cf3e"
    assert compute_dedupe_key(
        source="example-feed", type="news", content_hash=content_hash,
        source_record_id="record-1",
    ) == "d75f9589dfd4fe531d6159edb52f02a4207f4eb17e5c2a7831c37055725ccca1"
    assert normalized["content_hash"] == content_hash
    assert normalized["dedupe_key"] == (
        "fe6e03509d4f8915ea372bf326b349162bf17061ffdb615a95d7d985ad38cf3e"
    )


def test_captured_content_is_stored_byte_identical_to_what_arrived():
    decomposed = unicodedata.normalize("NFD", '{"title":"러시아 Türkiye"}')
    composed = unicodedata.normalize("NFC", decomposed)

    observation, _ = get_or_create_observation(_leaf(raw_text=decomposed))

    stored = Observation.get_by_id(observation.id)
    assert stored.raw_text == decomposed
    assert stored.raw_text.encode("utf-8") == decomposed.encode("utf-8")
    assert stored.raw_text != composed
    assert stored.content_hash == compute_content_hash(decomposed)


def test_two_normalizations_stay_two_records_and_two_identities():
    decomposed = unicodedata.normalize("NFD", '{"title":"Türkiye"}')
    composed = unicodedata.normalize("NFC", decomposed)

    first, first_created = get_or_create_observation(_leaf(raw_text=composed))
    second, second_created = get_or_create_observation(_leaf(raw_text=decomposed))

    # Not a defect to fix by normalizing: identity is the bytes a publisher
    # served, and rewriting them would rewrite the evidence.
    assert (first_created, second_created) == (True, True)
    assert first.content_hash != second.content_hash
    assert first.dedupe_key != second.dedupe_key


def test_content_equivalence_is_a_signal_beside_identity_and_never_inside_it():
    decomposed = unicodedata.normalize("NFD", '{"title":"Türkiye"}')
    composed = unicodedata.normalize("NFC", decomposed)
    plain = '{"title":"A verified test report"}'

    equivalence = compute_content_equivalence_key(decomposed)

    assert equivalence == compute_content_equivalence_key(composed)
    assert compute_content_equivalence_key(plain) != compute_content_equivalence_key(
        '{"title":"A different report"}'
    )
    # Domain-separated, so an equivalence digest can never be read as, or
    # substituted for, the identity of an already-stored record.
    assert equivalence != compute_content_hash(composed)
    assert compute_content_equivalence_key(plain) != compute_content_hash(plain)
    # And it reaches nothing that keys storage.
    assert "equivalence" not in repr(normalize_leaf(_leaf()))
    assert set(normalize_leaf(_leaf(raw_text=decomposed))) == set(
        normalize_leaf(_leaf(raw_text=composed))
    )
    assert normalize_leaf(_leaf(raw_text=decomposed))["dedupe_key"] != normalize_leaf(
        _leaf(raw_text=composed)
    )["dedupe_key"]
