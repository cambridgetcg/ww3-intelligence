# tests/test_pipeline.py
import json
from datetime import timedelta
from unittest.mock import patch

import pytest

from core.pipeline import (
    AnalysisLeaseLost,
    _claim_analysis,
    _persist_analysis,
    _utcnow_naive,
    analyze_observation,
    ingest_leaf,
    run_pipeline,
)
from core.db import AnalysisRun, Event, Observation
from core.observations import get_or_create_observation
from analysis.llm_processor import LLMResult


@pytest.fixture(autouse=True)
def model_boundary_available():
    """Pipeline tests deliberately exercise the explicit model layer."""
    with patch("core.pipeline.claude_available", return_value=True):
        yield


def _mock_result(title="Synthetic Seismic M4.3"):
    return LLMResult(
        event_title=title,
        event_category="seismic",
        classification="speculation",
        significance=8,
        summary="Synthetic seismic event used to exercise the model boundary.",
        actors=[],
        evidence=[],
        narratives=[],
        escalation_region="Test Region",
        escalation_level=5,
        technical={},
        impact={},
        scenarios=[],
    )


def test_ingest_leaf_creates_event():
    leaf = {
        "source": "usgs",
        "type": "seismic",
        "raw_text": '{"mag": 4.3, "place": "Test Region"}',
        "location_name": "Test Region",
        "geo_lat": 33.5,
        "geo_lon": 51.9,
    }
    with patch("core.pipeline.process_leaf", return_value=_mock_result()):
        event = ingest_leaf(leaf)
    assert event is not None
    assert Event.select().count() == 1
    assert event.title == "Synthetic Seismic M4.3"
    assert event.category == "seismic"
    assert event.significance == 8
    assert Observation.select().count() == 1
    run = AnalysisRun.get()
    assert run.status == "completed"
    assert run.event == event


def test_ingest_leaf_stores_lat_lon():
    leaf = {
        "source": "usgs",
        "type": "seismic",
        "raw_text": '{"mag": 4.3}',
        "location_name": "Test Region",
        "geo_lat": 33.5,
        "geo_lon": 51.9,
    }
    with patch("core.pipeline.process_leaf", return_value=_mock_result()):
        event = ingest_leaf(leaf)
    assert event.lat == 33.5
    assert event.lon == 51.9


def test_ingest_leaf_returns_none_on_llm_failure():
    leaf = {"source": "usgs", "type": "seismic", "raw_text": "bad data"}
    with patch("core.pipeline.process_leaf", side_effect=Exception("LLM failed")):
        event = ingest_leaf(leaf)
    assert event is None
    assert Event.select().count() == 0
    assert Observation.select().count() == 1
    run = AnalysisRun.get()
    assert run.status == "failed"
    assert run.error == "Exception"
    assert run.error_stage == "analysis"


def test_analyze_observation_disabled_stops_before_run_claim():
    observation, _ = get_or_create_observation({
        "source": "test",
        "source_record_id": "disabled-analysis",
        "type": "news",
        "raw_text": "source body",
    })

    with patch("core.pipeline.claude_available", return_value=False), \
         patch("core.pipeline.process_leaf") as process:
        event = analyze_observation(observation)

    assert event is None
    assert AnalysisRun.select().count() == 0
    assert Event.select().count() == 0
    process.assert_not_called()


def test_identical_observation_does_not_repeat_model_work_or_event():
    leaf = {
        "source": "usgs",
        "source_record_id": "stable-event-id",
        "type": "seismic",
        "raw_text": '{"mag": 4.3}',
    }
    with patch("core.pipeline.process_leaf", return_value=_mock_result()) as process:
        first = ingest_leaf(leaf)
        duplicate = ingest_leaf(dict(leaf))

    assert first is not None
    assert duplicate is None
    assert process.call_count == 1
    assert Observation.select().count() == 1
    assert AnalysisRun.select().count() == 1
    assert Event.select().count() == 1


def test_changed_source_content_creates_a_revision_and_new_analysis():
    leaf = {
        "source": "example-feed",
        "source_record_id": "corrected-story",
        "type": "news",
        "raw_text": "first version",
    }
    with patch("core.pipeline.process_leaf", return_value=_mock_result()) as process:
        first_event = ingest_leaf(leaf)
        revised_event = ingest_leaf({**leaf, "raw_text": "corrected version"})

    assert first_event is not None
    assert revised_event is not None
    assert process.call_count == 2
    observations = list(Observation.select().order_by(Observation.id))
    assert len(observations) == 2
    assert observations[1].supersedes == observations[0]


def test_event_uses_source_date_and_caps_single_source_confirmation():
    leaf = {
        "source": "example-feed",
        "source_record_id": "proposal-1",
        "type": "news",
        "raw_text": '{"title": "Proposal"}',
        "published_at": "2026-04-05T23:45:00Z",
        "canonical_url": "https://example.test/proposal-1",
    }
    result = _mock_result()
    result.classification = "confirmed"
    result.evidence = [{
        "type": "source_claim",
        "description": "The source reports a proposal.",
        "confidence": "medium",
        "source_url": None,
    }]

    with patch("core.pipeline.process_leaf", return_value=result):
        event = ingest_leaf(leaf)

    assert str(event.date) == "2026-04-05"
    assert event.classification == "reported"
    assert event.evidence.get().source_url == "https://example.test/proposal-1"
    output = json.loads(AnalysisRun.get().output_json)
    assert output["model_output"]["classification"] == "confirmed"
    assert output["policy"]["single_observation_confirmation_capped"] is True


def test_branch_failure_rolls_back_derived_event_but_keeps_provenance():
    leaf = {"source": "usgs", "type": "seismic", "raw_text": '{"id":"rollback"}'}
    with patch("core.pipeline.process_leaf", return_value=_mock_result()), \
         patch("core.pipeline.save_all_branches", side_effect=RuntimeError("branch failed")):
        event = ingest_leaf(leaf)

    assert event is None
    assert Event.select().count() == 0
    assert Observation.select().count() == 1
    run = AnalysisRun.get()
    assert run.status == "failed"
    assert run.error_stage == "persistence"


def test_stale_worker_is_fenced_after_successor_reclaims_lease():
    observation, _ = get_or_create_observation({
        "source": "test",
        "source_record_id": "lease-test",
        "type": "news",
        "raw_text": "lease test",
    })
    first_claim = _claim_analysis(observation)
    assert first_claim is not None
    AnalysisRun.update(
        started_at=_utcnow_naive() - timedelta(hours=1)
    ).where(AnalysisRun.id == first_claim.run_id).execute()

    successor_claim = _claim_analysis(observation)
    assert successor_claim is not None
    assert successor_claim.token != first_claim.token
    event, _ = _persist_analysis(successor_claim, observation, _mock_result())

    with pytest.raises(AnalysisLeaseLost):
        _persist_analysis(first_claim, observation, _mock_result("Stale duplicate"))

    run = AnalysisRun.get_by_id(first_claim.run_id)
    assert run.status == "completed"
    assert run.event == event
    assert run.attempt == 2
    assert Event.select().count() == 1


def test_run_pipeline_processes_multiple_leaves():
    leaves = [
        {"source": "usgs", "type": "seismic", "raw_text": '{"mag": 4.3}', "location_name": "Test Region A"},
        {"source": "usgs", "type": "seismic", "raw_text": '{"mag": 3.1}', "location_name": "Test Region B"},
    ]
    results = [_mock_result("Event A"), _mock_result("Event B")]
    with patch("core.pipeline.process_leaf", side_effect=results):
        events = run_pipeline(leaves)
    assert len(events) == 2
    assert Event.select().count() == 2


def test_run_pipeline_skips_failed_leaves():
    leaves = [
        {"source": "usgs", "type": "seismic", "raw_text": "good"},
        {"source": "usgs", "type": "seismic", "raw_text": "bad"},
    ]
    def side_effect(leaf, **kwargs):
        if leaf["raw_text"] == "bad":
            raise Exception("Parse failed")
        return _mock_result()

    with patch("core.pipeline.process_leaf", side_effect=side_effect):
        events = run_pipeline(leaves)
    assert len(events) == 1
    assert Event.select().count() == 1
