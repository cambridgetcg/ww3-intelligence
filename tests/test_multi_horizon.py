import json
from datetime import timedelta
from unittest.mock import patch

import pytest

from analysis.claude_cli import DEFAULT_MODEL
from analysis.multi_horizon import (
    CODE_VERSION,
    _claim_forecast,
    _persist_forecast,
    forecast_prompt,
    forecast_source_observation,
    forecast_subject_key,
    generate_multi_horizon_predictions,
    load_forecast_contract,
)
from analysis.predictions import create_prediction
from analysis.schema import validate_horizon_payload
from core.pipeline import AnalysisLeaseLost, _utcnow_naive
from core.db import (
    HORIZONS,
    HORIZON_CODES,
    HORIZON_FORECAST_CONTRACT,
    HORIZON_LABELS,
    OBSERVATION_ANALYSIS_CONTRACT,
    PREDICTION_AUTHORSHIP_MANUAL,
    PREDICTION_AUTHORSHIP_MODEL,
    AnalysisRun,
    Event,
    Prediction,
)
from core.observations import get_or_create_observation

MOCK_LLM_RESPONSE = {
    "event_assessment": "Rapid diplomatic escalation expected.",
    "horizons": [
        {"horizon": "1h",  "outcome": "Source publishes an initial statement", "probability": 90, "confidence": "high",   "reasoning": "Synthetic short-horizon response."},
        {"horizon": "3h",  "outcome": "Newsrooms report the statement",       "probability": 75, "confidence": "high",   "reasoning": "Synthetic media cycle."},
        {"horizon": "12h", "outcome": "Inspectors request source data",        "probability": 45, "confidence": "medium", "reasoning": "Synthetic protocol step."},
        {"horizon": "1d",  "outcome": "Institutions publish responses",       "probability": 65, "confidence": "medium", "reasoning": "Synthetic coordination."},
        {"horizon": "3d",  "outcome": "A formal consultation occurs",         "probability": 35, "confidence": "medium", "reasoning": "Synthetic process."},
        {"horizon": "1w",  "outcome": "Independent measurements arrive",      "probability": 50, "confidence": "medium", "reasoning": "Synthetic sampling delay."},
        {"horizon": "2w",  "outcome": "International consensus",           "probability": 30, "confidence": "low",    "reasoning": "Consensus is slow."},
        {"horizon": "1m",  "outcome": "A negotiation channel opens",        "probability": 20, "confidence": "low",    "reasoning": "Synthetic diplomatic track."},
        {"horizon": "3m",  "outcome": "A capability report is published",   "probability": 15, "confidence": "low",    "reasoning": "Synthetic long-term assessment."},
        {"horizon": "6m",  "outcome": "Regional security realignment",     "probability": 10, "confidence": "low",    "reasoning": "Structural shift."},
    ]
}


def _sourced_event(title, **fields):
    """Build an event with the observation lineage a recorded forecast needs."""
    observation, _ = get_or_create_observation({
        "source": "example-feed",
        "source_record_id": f"forecast-subject-{title}",
        "type": "news",
        "raw_text": f"Source record behind {title}.",
    })
    event = Event.create(title=title, **{
        "date": "2026-03-03", "category": "seismic",
        "classification": "speculation", "significance": 5, **fields,
    })
    AnalysisRun.create(
        observation=observation, event=event, status="completed",
        model_name="test-model", prompt_version="prompt-sha",
        code_version="test-code",
    )
    return event


def _forecast_runs():
    return list(
        AnalysisRun.select()
        .where(AnalysisRun.contract == HORIZON_FORECAST_CONTRACT)
        .order_by(AnalysisRun.id)
    )


def test_horizons_constant():
    assert len(HORIZONS) == 10
    assert HORIZON_CODES == ["1h", "3h", "12h", "1d", "3d", "1w", "2w", "1m", "3m", "6m"]
    assert HORIZON_LABELS["1h"] == "1 Hour"
    assert HORIZON_LABELS["6m"] == "6 Months"


def test_prediction_horizon_field():
    event = Event.create(title="T", date="2026-03-03", category="seismic",
                         classification="speculation", significance=5)
    pred = create_prediction(event, 90, "Denial", "high", "1 Hour", horizon="1h")
    assert Prediction.get_by_id(pred.id).horizon == "1h"


def test_prediction_backward_compat():
    event = Event.create(title="T2", date="2026-03-03", category="political",
                         classification="reported", significance=3)
    pred = create_prediction(event, 50, "Legacy")
    assert pred.horizon is None


def test_create_prediction_with_horizon_kwarg():
    event = Event.create(title="T3", date="2026-03-03", category="seismic",
                         classification="speculation", significance=5)
    pred = create_prediction(event, 45, "Inspection likely", "medium", "12 Hours", horizon="12h")
    assert pred.horizon == "12h"
    assert pred.timeframe == "12 Hours"


def test_generate_returns_empty_when_llm_disabled():
    event = _sourced_event("Disabled")
    with patch("analysis.multi_horizon._call_claude_for_horizons") as call_claude:
        result = generate_multi_horizon_predictions(event)
    assert result == []
    call_claude.assert_not_called()
    assert _forecast_runs() == []


def test_generate_creates_predictions():
    event = _sourced_event("Synthetic M4.3", summary="Synthetic shallow seismic event.",
                           location_name="Test Region")
    with patch("analysis.claude_cli.claude_available", return_value=True), \
         patch("analysis.multi_horizon._call_claude_for_horizons", return_value=MOCK_LLM_RESPONSE):
        results = generate_multi_horizon_predictions(event)
    assert len(results) == 10
    assert results[0]["horizon"] == "1h"
    assert results[0]["probability"] == 90
    assert results[-1]["horizon"] == "6m"
    db_preds = list(Prediction.select().where(Prediction.event == event))
    assert len(db_preds) == 10


def test_forecast_records_one_run_with_model_prompt_and_code_version():
    event = _sourced_event("Recorded Forecast")
    with patch("analysis.claude_cli.claude_available", return_value=True), \
         patch("analysis.multi_horizon._call_claude_for_horizons", return_value=MOCK_LLM_RESPONSE):
        results = generate_multi_horizon_predictions(event)

    runs = _forecast_runs()
    assert len(runs) == 1
    run = runs[0]
    assert run.status == "completed"
    assert run.event_id == event.id
    assert run.observation_id == forecast_source_observation(event).id
    assert run.model_name == DEFAULT_MODEL
    assert run.prompt_version == load_forecast_contract()[1]
    assert run.code_version == CODE_VERSION
    assert run.code_version.startswith(f"{HORIZON_FORECAST_CONTRACT}:")
    assert run.attempt == 1
    assert run.claim_token is None
    stored = json.loads(run.output_json)["model_output"]
    assert [item["horizon"] for item in stored["horizons"]] == HORIZON_CODES

    assert {item["analysis_run_id"] for item in results} == {run.id}
    predictions = list(Prediction.select().where(Prediction.event == event))
    assert {prediction.analysis_run_id for prediction in predictions} == {run.id}
    assert {prediction.authorship for prediction in predictions} == {
        PREDICTION_AUTHORSHIP_MODEL
    }


def test_identical_forecast_request_repeats_no_model_work_or_rows():
    event = _sourced_event("Idempotent Forecast")
    with patch("analysis.claude_cli.claude_available", return_value=True), \
         patch("analysis.multi_horizon._call_claude_for_horizons",
               return_value=MOCK_LLM_RESPONSE) as call_claude:
        first = generate_multi_horizon_predictions(event)
        second = generate_multi_horizon_predictions(event)

    assert len(first) == 10
    assert second == []
    assert call_claude.call_count == 1
    assert len(_forecast_runs()) == 1
    assert Prediction.select().where(Prediction.event == event).count() == 10


def test_edited_event_text_is_a_separate_forecast_question():
    event = _sourced_event("Revised Forecast")
    with patch("analysis.claude_cli.claude_available", return_value=True), \
         patch("analysis.multi_horizon._call_claude_for_horizons",
               return_value=MOCK_LLM_RESPONSE) as call_claude:
        generate_multi_horizon_predictions(event)
        event.summary = "The event summary was corrected after publication."
        event.save()
        revised = generate_multi_horizon_predictions(event)

    assert len(revised) == 10
    assert call_claude.call_count == 2
    runs = _forecast_runs()
    assert len(runs) == 2
    assert runs[0].analysis_key != runs[1].analysis_key
    assert Prediction.select().where(Prediction.event == event).count() == 20


def test_forecast_subject_key_separates_identically_worded_events():
    first = _sourced_event("Twin A")
    second = _sourced_event("Twin B")
    template, _ = load_forecast_contract()
    shared_prompt = template.format(
        event_title="Same wording", event_category="seismic",
        event_date="2026-03-03", event_location="unknown",
        event_summary="No summary available.",
        horizon_codes=", ".join(HORIZON_CODES),
    )

    assert forecast_subject_key(first, shared_prompt) != forecast_subject_key(
        second, shared_prompt
    )


def test_forecast_refuses_an_event_without_observation_lineage():
    event = Event.create(title="Unsourced", date="2026-03-03", category="political",
                         classification="speculation", significance=3)
    with patch("analysis.claude_cli.claude_available", return_value=True), \
         patch("analysis.multi_horizon._call_claude_for_horizons") as call_claude:
        results = generate_multi_horizon_predictions(event)

    assert results == []
    call_claude.assert_not_called()
    assert AnalysisRun.select().count() == 0
    assert Prediction.select().count() == 0


def test_runs_predating_the_contract_column_stay_unrecorded_lineage():
    from core.db import _migrate_forecast_provenance, database, observation_analysis_runs

    event = _sourced_event("Legacy Run")
    database.execute_sql("ALTER TABLE analysisrun DROP COLUMN contract")
    _migrate_forecast_provenance()

    run = AnalysisRun.get()
    assert run.contract is None
    assert [item.id for item in observation_analysis_runs()] == [run.id]
    assert forecast_source_observation(event).id == run.observation_id
    assert _forecast_runs() == []


def test_generate_sorted_by_canonical_order():
    shuffled = dict(MOCK_LLM_RESPONSE)
    shuffled["horizons"] = list(reversed(MOCK_LLM_RESPONSE["horizons"]))
    event = _sourced_event("Sort Test")
    with patch("analysis.claude_cli.claude_available", return_value=True), \
         patch("analysis.multi_horizon._call_claude_for_horizons", return_value=shuffled):
        results = generate_multi_horizon_predictions(event)
    assert [r["horizon"] for r in results] == HORIZON_CODES


def test_generate_rejects_incomplete_or_invalid_forecast_sets():
    bad = {"event_assessment": "Test.", "horizons": [
        {"horizon": "5y", "outcome": "War", "probability": 50, "confidence": "low", "reasoning": "bad"},
        {"horizon": "1h", "outcome": "OK",  "probability": 90, "confidence": "high","reasoning": "ok"},
    ]}
    event = _sourced_event("Filter Test")
    with patch("analysis.claude_cli.claude_available", return_value=True), \
         patch("analysis.multi_horizon._call_claude_for_horizons", return_value=bad):
        results = generate_multi_horizon_predictions(event)
    assert results == []
    assert Prediction.select().where(Prediction.event == event).count() == 0
    run = _forecast_runs()[0]
    assert run.status == "failed"
    assert run.error == "LLMValidationError"
    assert run.error_stage == "analysis"


def test_generate_returns_empty_on_llm_error():
    event = _sourced_event("Err Test")
    with patch("analysis.claude_cli.claude_available", return_value=True), \
         patch("analysis.multi_horizon._call_claude_for_horizons", side_effect=Exception("network")):
        results = generate_multi_horizon_predictions(event)
    assert results == []
    run = _forecast_runs()[0]
    assert run.status == "failed"
    assert run.error_stage == "analysis"


def test_prediction_set_persistence_is_atomic():
    import analysis.multi_horizon as module

    event = _sourced_event("Atomic Forecast", category="political",
                           classification="reported")
    real_create = module.create_prediction
    calls = 0

    def fail_on_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated persistence failure")
        return real_create(*args, **kwargs)

    with patch("analysis.claude_cli.claude_available", return_value=True), \
         patch.object(module, "_call_claude_for_horizons", return_value=MOCK_LLM_RESPONSE), \
         patch.object(module, "create_prediction", side_effect=fail_on_second):
        results = module.generate_multi_horizon_predictions(event)

    assert results == []
    assert Prediction.select().where(Prediction.event == event).count() == 0
    run = _forecast_runs()[0]
    assert run.status == "failed"
    assert run.error == "RuntimeError"
    assert run.error_stage == "persistence"
    assert run.output_json is None


def test_stale_forecast_worker_is_fenced_after_successor_persists():
    event = _sourced_event("Fenced Forecast")
    observation = forecast_source_observation(event)
    template, prompt_version = load_forecast_contract()
    prompt = forecast_prompt(template, event)
    data = validate_horizon_payload(MOCK_LLM_RESPONSE, HORIZON_CODES)

    first_claim = _claim_forecast(event, observation, prompt, prompt_version)
    assert first_claim is not None
    AnalysisRun.update(
        started_at=_utcnow_naive() - timedelta(hours=1)
    ).where(AnalysisRun.id == first_claim.run_id).execute()

    successor_claim = _claim_forecast(event, observation, prompt, prompt_version)
    assert successor_claim is not None
    assert successor_claim.token != first_claim.token
    _persist_forecast(successor_claim, event, data)

    with pytest.raises(AnalysisLeaseLost):
        _persist_forecast(first_claim, event, data)

    assert len(_forecast_runs()) == 1
    assert Prediction.select().where(Prediction.event == event).count() == 10


def test_forecast_run_is_not_the_observation_analysis_state():
    from cli.commands import _analysis_state

    event = _sourced_event("Observation State")
    observation = forecast_source_observation(event)
    with patch("analysis.claude_cli.claude_available", return_value=True), \
         patch("analysis.multi_horizon._call_claude_for_horizons", side_effect=Exception("network")):
        generate_multi_horizon_predictions(event)

    assert _forecast_runs()[0].status == "failed"
    state = _analysis_state(observation)
    assert state["status"] == "completed"
    assert state["run_count"] == 1


def test_cli_runs_and_provenance_surface_the_forecast_run():
    from click.testing import CliRunner
    from cli.commands import cli

    event = _sourced_event("Audited Forecast")
    with patch("analysis.claude_cli.claude_available", return_value=True), \
         patch("analysis.multi_horizon._call_claude_for_horizons", return_value=MOCK_LLM_RESPONSE):
        generate_multi_horizon_predictions(event)
    run = _forecast_runs()[0]

    runner = CliRunner()
    listing = runner.invoke(cli, ["runs", "--json"])
    assert listing.exit_code == 0, listing.output
    records = {
        record["id"]: record
        for record in json.loads(listing.output)["analysis_runs"]
    }
    assert records[run.id]["contract"] == HORIZON_FORECAST_CONTRACT
    assert records[run.id]["model"] == DEFAULT_MODEL
    assert records[run.id]["prompt_sha256"] == run.prompt_version
    assert records[run.id]["code_version"] == CODE_VERSION

    lineage = runner.invoke(cli, ["provenance", str(event.id), "--json"])
    assert lineage.exit_code == 0, lineage.output
    contracts = {
        record["contract"]
        for record in json.loads(lineage.output)["analysis_runs"]
    }
    assert contracts == {OBSERVATION_ANALYSIS_CONTRACT, HORIZON_FORECAST_CONTRACT}

    text = runner.invoke(cli, ["provenance", str(event.id)])
    assert text.exit_code == 0, text.output
    assert f"contract={HORIZON_FORECAST_CONTRACT}" in text.output
    assert run.prompt_version in text.output


def test_cli_show_distinguishes_model_and_hand_entered_predictions():
    from click.testing import CliRunner
    from cli.commands import cli

    event = _sourced_event("Authorship Display")
    with patch("analysis.claude_cli.claude_available", return_value=True), \
         patch("analysis.multi_horizon._call_claude_for_horizons", return_value=MOCK_LLM_RESPONSE):
        generate_multi_horizon_predictions(event)
    create_prediction(event, 20, "Hand-entered horizon", "low", "1 Day", horizon="1d",
                      authorship=PREDICTION_AUTHORSHIP_MANUAL)

    result = CliRunner().invoke(cli, ["show", str(event.id)])
    assert result.exit_code == 0, result.output
    assert PREDICTION_AUTHORSHIP_MODEL in result.output
    assert PREDICTION_AUTHORSHIP_MANUAL in result.output


def test_cli_predict_horizons_missing_event():
    from click.testing import CliRunner
    from cli.commands import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["predict-horizons", "999"])
    assert result.exit_code == 0
    assert "not found" in result.output.lower()


def test_cli_predict_horizons_disabled_prompts_manual():
    from click.testing import CliRunner
    from cli.commands import cli
    runner = CliRunner()
    runner.invoke(cli, ["add-event", "--title", "HorizonTest", "--category", "seismic"])
    event = Event.select().where(Event.title == "HorizonTest").get()
    result = runner.invoke(cli, ["predict-horizons", str(event.id)])
    assert result.exit_code == 0
    assert "disabled" in result.output.lower()
    assert "--manual" in result.output


def test_cli_predict_horizons_refuses_event_without_lineage():
    from click.testing import CliRunner
    from cli.commands import cli
    runner = CliRunner()
    runner.invoke(cli, ["add-event", "--title", "HorizonUnsourced", "--category", "seismic"])
    event = Event.select().where(Event.title == "HorizonUnsourced").get()
    with patch("analysis.claude_cli.claude_available", return_value=True), \
         patch("analysis.multi_horizon._call_claude_for_horizons") as call_claude:
        result = runner.invoke(cli, ["predict-horizons", str(event.id)])
    assert result.exit_code == 0
    assert "no observation lineage" in result.output
    assert "--manual" in result.output
    call_claude.assert_not_called()
    assert Prediction.select().count() == 0


def test_cli_predict_horizons_llm_path():
    from click.testing import CliRunner
    from cli.commands import cli
    runner = CliRunner()
    event = _sourced_event("HorizonLLM")
    fake_results = [
        {"horizon": "1h", "label": "1 Hour", "outcome": "Denial", "probability": 90,
         "confidence": "high", "reasoning": "Standard.", "prediction_id": 1,
         "analysis_run_id": 7},
    ]
    with patch("analysis.claude_cli.claude_available", return_value=True), \
         patch("analysis.multi_horizon.generate_multi_horizon_predictions", return_value=fake_results):
        result = runner.invoke(cli, ["predict-horizons", str(event.id)])
    assert result.exit_code == 0
    assert "1 horizon predictions" in result.output
    assert "1h" in result.output
    assert "analysis run [7]" in result.output


def test_cli_predict_horizons_reports_the_completed_forecast_run():
    from click.testing import CliRunner
    from cli.commands import cli

    event = _sourced_event("HorizonRepeat")
    with patch("analysis.claude_cli.claude_available", return_value=True), \
         patch("analysis.multi_horizon._call_claude_for_horizons", return_value=MOCK_LLM_RESPONSE):
        generate_multi_horizon_predictions(event)
        result = CliRunner().invoke(cli, ["predict-horizons", str(event.id)])

    run = _forecast_runs()[0]
    assert result.exit_code == 0, result.output
    assert f"completed (run={run.id}, predictions=10)" in result.output
    assert Prediction.select().where(Prediction.event == event).count() == 10


def test_cli_predict_horizons_manual_creates_prediction():
    from click.testing import CliRunner
    from cli.commands import cli
    runner = CliRunner()
    runner.invoke(cli, ["add-event", "--title", "ManualTest", "--category", "seismic"])
    event = Event.select().where(Event.title == "ManualTest").get()
    # Provide input for first horizon (1h), skip remaining 9 (blank enter x9)
    # Each horizon prompts: outcome (text), then if non-blank: probability (int), confidence (choice)
    user_input = "Denial issued\n90\nmedium\n" + "\n" * 9
    result = runner.invoke(cli, ["predict-horizons", str(event.id), "--manual"],
                           input=user_input)
    assert result.exit_code == 0
    assert "Created 1 horizon predictions" in result.output
    db_preds = list(Prediction.select().where(Prediction.event == event))
    assert len(db_preds) == 1
    assert db_preds[0].horizon == "1h"
    assert db_preds[0].authorship == PREDICTION_AUTHORSHIP_MANUAL
    assert db_preds[0].analysis_run_id is None
