"""Collection-only boundary tests."""

import os
from pathlib import Path
from unittest.mock import patch

from core.collection import collect_batch, collect_leaf
from core.db import AnalysisRun, Event, Observation


def _leaf(*, raw_text="first report", source_record_id="record-1"):
    return {
        "source": "test-feed",
        "source_record_id": source_record_id,
        "type": "news",
        "raw_text": raw_text,
        "canonical_url": f"https://example.test/{source_record_id}",
        "published_at": "2026-07-17T10:00:00Z",
    }


def test_collect_leaf_never_analyses_even_when_llm_is_enabled():
    stream_path = Path(os.environ["WW3_STREAM_PATH"])
    assert not stream_path.exists()

    with patch.dict(os.environ, {"WW3_DISABLE_LLM": "0"}), \
         patch("analysis.claude_cli.subprocess.run") as model_process, \
         patch("core.pipeline.process_leaf") as process_leaf, \
         patch("core.stream.StreamWriter.write") as stream_write:
        outcome = collect_leaf(_leaf())

    assert outcome.status == "new"
    assert outcome.error_type is None
    assert outcome.observation.id == Observation.get().id
    assert AnalysisRun.select().count() == 0
    assert Event.select().count() == 0
    assert not stream_path.exists()
    model_process.assert_not_called()
    process_leaf.assert_not_called()
    stream_write.assert_not_called()


def test_collect_leaf_is_idempotent_for_an_exact_duplicate():
    first = collect_leaf(_leaf())
    duplicate = collect_leaf(_leaf())

    assert first.status == "new"
    assert duplicate.status == "known"
    assert duplicate.observation.id == first.observation.id
    assert Observation.select().count() == 1


def test_collect_leaf_identifies_and_links_content_revision():
    first = collect_leaf(_leaf(raw_text="version A"))
    revision = collect_leaf(_leaf(raw_text="version B"))

    assert first.status == "new"
    assert revision.status == "revision"
    assert revision.observation.id != first.observation.id
    assert revision.observation.supersedes_id == first.observation.id
    assert Observation.select().count() == 2


def test_collect_batch_reports_exact_mixed_outcomes_without_input_detail():
    secret_invalid_text = "private invalid source body"
    batch = collect_batch([
        _leaf(raw_text="version A"),
        _leaf(raw_text="version A"),
        _leaf(raw_text="version B"),
        {"type": "news", "raw_text": secret_invalid_text},
        None,
    ])

    assert batch.received == 5
    assert batch.new == 1
    assert batch.known == 1
    assert batch.revision == 1
    assert batch.invalid == 2
    assert [outcome.status for outcome in batch.outcomes] == [
        "new", "known", "revision", "invalid", "invalid",
    ]
    assert [outcome.error_type for outcome in batch.outcomes] == [
        None, None, None, "ValueError", "TypeError",
    ]
    assert len(batch.observations) == 3
    assert [item.id for item in batch.observations] == [1, 1, 2]
    assert all(outcome.observation is None for outcome in batch.outcomes[3:])
    assert secret_invalid_text not in repr(batch)
    assert not hasattr(batch.outcomes[3], "error")
    assert not hasattr(batch.outcomes[3], "raw_input")


def test_collect_batch_has_no_event_or_stream_side_effects():
    result = collect_batch([_leaf(source_record_id="alias-1")])

    assert result.received == 1
    assert result.new == 1
    assert AnalysisRun.select().count() == 0
    assert Event.select().count() == 0
    assert not Path(os.environ["WW3_STREAM_PATH"]).exists()
