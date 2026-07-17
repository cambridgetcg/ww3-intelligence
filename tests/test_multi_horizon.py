from unittest.mock import patch

import pytest

from core.db import HORIZONS, HORIZON_CODES, HORIZON_LABELS, Event, Prediction
from analysis.predictions import create_prediction

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
    from analysis.multi_horizon import generate_multi_horizon_predictions
    event = Event.create(title="T", date="2026-03-03", category="seismic",
                         classification="speculation", significance=5)
    with patch("analysis.multi_horizon._call_claude_for_horizons") as call_claude:
        result = generate_multi_horizon_predictions(event)
    assert result == []
    call_claude.assert_not_called()


def test_generate_creates_predictions():
    from analysis.multi_horizon import generate_multi_horizon_predictions
    event = Event.create(title="Synthetic M4.3", date="2026-03-03", category="seismic",
                         classification="speculation", significance=8,
                         summary="Synthetic shallow seismic event.", location_name="Test Region")
    with patch("analysis.claude_cli.claude_available", return_value=True), \
         patch("analysis.multi_horizon._call_claude_for_horizons", return_value=MOCK_LLM_RESPONSE):
        results = generate_multi_horizon_predictions(event)
    assert len(results) == 10
    assert results[0]["horizon"] == "1h"
    assert results[0]["probability"] == 90
    assert results[-1]["horizon"] == "6m"
    db_preds = list(Prediction.select().where(Prediction.event == event))
    assert len(db_preds) == 10


def test_generate_sorted_by_canonical_order():
    from analysis.multi_horizon import generate_multi_horizon_predictions
    shuffled = dict(MOCK_LLM_RESPONSE)
    shuffled["horizons"] = list(reversed(MOCK_LLM_RESPONSE["horizons"]))
    event = Event.create(title="Sort Test", date="2026-03-03", category="seismic",
                         classification="speculation", significance=5)
    with patch("analysis.claude_cli.claude_available", return_value=True), \
         patch("analysis.multi_horizon._call_claude_for_horizons", return_value=shuffled):
        results = generate_multi_horizon_predictions(event)
    assert [r["horizon"] for r in results] == HORIZON_CODES


def test_generate_rejects_incomplete_or_invalid_forecast_sets():
    from analysis.multi_horizon import generate_multi_horizon_predictions
    bad = {"event_assessment": "Test.", "horizons": [
        {"horizon": "5y", "outcome": "War", "probability": 50, "confidence": "low", "reasoning": "bad"},
        {"horizon": "1h", "outcome": "OK",  "probability": 90, "confidence": "high","reasoning": "ok"},
    ]}
    event = Event.create(title="Filter Test", date="2026-03-03", category="seismic",
                         classification="speculation", significance=5)
    with patch("analysis.claude_cli.claude_available", return_value=True), \
         patch("analysis.multi_horizon._call_claude_for_horizons", return_value=bad):
        results = generate_multi_horizon_predictions(event)
    assert results == []
    assert Prediction.select().where(Prediction.event == event).count() == 0


def test_generate_returns_empty_on_llm_error():
    from analysis.multi_horizon import generate_multi_horizon_predictions
    event = Event.create(title="Err Test", date="2026-03-03", category="seismic",
                         classification="speculation", significance=5)
    with patch("analysis.claude_cli.claude_available", return_value=True), \
         patch("analysis.multi_horizon._call_claude_for_horizons", side_effect=Exception("network")):
        results = generate_multi_horizon_predictions(event)
    assert results == []


def test_prediction_set_persistence_is_atomic():
    import analysis.multi_horizon as module

    event = Event.create(
        title="Atomic Forecast", date="2026-03-03", category="political",
        classification="reported", significance=4,
    )
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
        with pytest.raises(RuntimeError, match="persistence failure"):
            module.generate_multi_horizon_predictions(event)

    assert Prediction.select().where(Prediction.event == event).count() == 0


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


def test_cli_predict_horizons_llm_path():
    from click.testing import CliRunner
    from cli.commands import cli
    runner = CliRunner()
    runner.invoke(cli, ["add-event", "--title", "HorizonLLM", "--category", "seismic"])
    event = Event.select().where(Event.title == "HorizonLLM").get()
    fake_results = [
        {"horizon": "1h", "label": "1 Hour", "outcome": "Denial", "probability": 90,
         "confidence": "high", "reasoning": "Standard.", "prediction_id": 1},
    ]
    with patch("analysis.claude_cli.claude_available", return_value=True), \
         patch("analysis.multi_horizon.generate_multi_horizon_predictions", return_value=fake_results):
        result = runner.invoke(cli, ["predict-horizons", str(event.id)])
    assert result.exit_code == 0
    assert "1 horizon predictions" in result.output
    assert "1h" in result.output


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
