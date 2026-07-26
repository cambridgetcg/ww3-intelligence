# tests/test_predictions.py
import pytest

from analysis.predictions import (
    compute_brier,
    create_prediction,
    prediction_authorship,
    resolve_prediction,
)
from core.db import (
    PREDICTION_AUTHORSHIP_MANUAL,
    PREDICTION_AUTHORSHIP_MODEL,
    PREDICTION_AUTHORSHIP_UNRECORDED,
    AnalysisRun,
    Event,
    Prediction,
    ScenarioNode,
    _migrate_forecast_provenance,
    database,
)
from core.observations import get_or_create_observation


LEGACY_PREDICTION_TABLE = """
CREATE TABLE "prediction" (
    "id" INTEGER NOT NULL PRIMARY KEY,
    "event_id" INTEGER NOT NULL,
    "scenario_node_id" INTEGER,
    "probability" INTEGER NOT NULL,
    "confidence" VARCHAR(255) NOT NULL,
    "timeframe" VARCHAR(255),
    "horizon" VARCHAR(255),
    "reasoning" TEXT NOT NULL,
    "created_at" DATETIME NOT NULL,
    "resolved_at" DATETIME,
    "outcome" TEXT,
    "brier_score" REAL,
    FOREIGN KEY ("event_id") REFERENCES "event" ("id"),
    FOREIGN KEY ("scenario_node_id") REFERENCES "scenarionode" ("id")
)
"""


def _analysis_run() -> AnalysisRun:
    observation, _ = get_or_create_observation({
        "source": "example-feed",
        "source_record_id": "prediction-provenance",
        "type": "news",
        "raw_text": "Source record behind a forecast.",
    })
    return AnalysisRun.create(
        observation=observation, status="running", model_name="test-model",
        prompt_version="prompt-sha", code_version="test-code",
    )


def test_compute_brier_perfect():
    assert abs(compute_brier(probability=100, occurred=True) - 0.0) < 0.001


def test_compute_brier_worst():
    assert abs(compute_brier(probability=0, occurred=True) - 1.0) < 0.001


def test_compute_brier_fifty_false():
    assert abs(compute_brier(probability=50, occurred=False) - 0.25) < 0.001


def test_compute_brier_35_true():
    # (0.35 - 1.0)^2 = 0.4225
    assert abs(compute_brier(probability=35, occurred=True) - 0.4225) < 0.001


def test_create_prediction():
    event = Event.create(
        title="Test", date="2026-03-03", category="seismic",
        classification="speculation", significance=5
    )
    pred = create_prediction(
        event=event,
        probability=35,
        reasoning="Shallow depth + facility proximity",
        confidence="medium",
        timeframe="2 weeks",
    )
    assert pred.probability == 35
    assert pred.brier_score is None
    assert pred.resolved_at is None
    assert pred.outcome is None


def test_create_prediction_with_scenario_node():
    event = Event.create(
        title="Test", date="2026-03-03", category="seismic",
        classification="speculation", significance=5
    )
    node = ScenarioNode.create(
        event=event,
        scenario_text="Synthetic scenario confirmed",
        probability=35,
        confidence="medium",
    )
    pred = create_prediction(
        event=event,
        probability=35,
        reasoning="Synthetic evidence points to the scenario",
        scenario_node=node,
    )
    assert pred.scenario_node_id == node.id


@pytest.mark.parametrize("probability", [-1, 101])
def test_create_prediction_rejects_out_of_range_probability(probability):
    event = Event.create(
        title="Test", date="2026-03-03", category="seismic",
        classification="speculation", significance=5,
    )
    with pytest.raises(ValueError, match="between 0 and 100"):
        create_prediction(event=event, probability=probability, reasoning="Invalid")


def test_create_prediction_records_hand_entered_authorship():
    event = Event.create(
        title="Test", date="2026-03-03", category="seismic",
        classification="speculation", significance=5,
    )
    pred = create_prediction(
        event=event,
        probability=35,
        reasoning="Hand-entered horizon",
        authorship=PREDICTION_AUTHORSHIP_MANUAL,
    )
    assert Prediction.get_by_id(pred.id).authorship == PREDICTION_AUTHORSHIP_MANUAL
    assert pred.analysis_run is None
    assert prediction_authorship(pred) == PREDICTION_AUTHORSHIP_MANUAL


def test_create_prediction_without_authorship_stays_unrecorded():
    event = Event.create(
        title="Test", date="2026-03-03", category="seismic",
        classification="speculation", significance=5,
    )
    pred = create_prediction(event=event, probability=35, reasoning="No authorship")
    assert pred.authorship is None
    assert prediction_authorship(pred) == PREDICTION_AUTHORSHIP_UNRECORDED


def test_create_prediction_rejects_unsupported_authorship():
    event = Event.create(
        title="Test", date="2026-03-03", category="seismic",
        classification="speculation", significance=5,
    )
    with pytest.raises(ValueError, match="unsupported prediction authorship"):
        create_prediction(
            event=event, probability=35, reasoning="Invalid", authorship="verified",
        )


def test_create_prediction_rejects_model_authorship_without_a_run():
    event = Event.create(
        title="Test", date="2026-03-03", category="seismic",
        classification="speculation", significance=5,
    )
    with pytest.raises(ValueError, match="require an analysis run"):
        create_prediction(
            event=event,
            probability=35,
            reasoning="Unrecorded model output",
            authorship=PREDICTION_AUTHORSHIP_MODEL,
        )
    assert Prediction.select().count() == 0


def test_create_prediction_rejects_a_run_behind_hand_entered_authorship():
    event = Event.create(
        title="Test", date="2026-03-03", category="seismic",
        classification="speculation", significance=5,
    )
    with pytest.raises(ValueError, match="only back model-derived"):
        create_prediction(
            event=event,
            probability=35,
            reasoning="Mislabelled",
            analysis_run=_analysis_run(),
            authorship=PREDICTION_AUTHORSHIP_MANUAL,
        )
    assert Prediction.select().count() == 0


def test_predictions_predating_provenance_columns_load_as_unrecorded():
    event = Event.create(
        title="Legacy", date="2026-03-03", category="political",
        classification="reported", significance=3,
    )
    # SQLite cannot drop a column named in a foreign key, so the table is
    # rebuilt exactly as a database written before these columns holds it.
    database.execute_sql("DROP TABLE prediction")
    database.execute_sql(LEGACY_PREDICTION_TABLE)
    database.execute_sql(
        "INSERT INTO prediction "
        "(event_id, probability, confidence, reasoning, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (event.id, 40, "medium", "Legacy forecast of unknown origin",
         "2026-03-03 00:00:00"),
    )

    _migrate_forecast_provenance()
    _migrate_forecast_provenance()

    legacy = Prediction.get()
    assert legacy.probability == 40
    assert legacy.reasoning == "Legacy forecast of unknown origin"
    assert legacy.analysis_run is None
    assert legacy.authorship is None
    assert prediction_authorship(legacy) == PREDICTION_AUTHORSHIP_UNRECORDED


def test_resolve_prediction_occurred():
    event = Event.create(
        title="Test", date="2026-03-03", category="seismic",
        classification="speculation", significance=5
    )
    pred = create_prediction(event=event, probability=35, reasoning="Test reasoning")
    resolved = resolve_prediction(pred.id, outcome="Synthetic scenario confirmed", occurred=True)
    assert resolved.outcome == "Synthetic scenario confirmed"
    assert resolved.resolved_at is not None
    assert abs(resolved.brier_score - 0.4225) < 0.001


def test_resolve_prediction_did_not_occur():
    event = Event.create(
        title="Test", date="2026-03-03", category="seismic",
        classification="speculation", significance=5
    )
    pred = create_prediction(event=event, probability=80, reasoning="High confidence wrong")
    resolved = resolve_prediction(pred.id, outcome="Natural earthquake", occurred=False)
    # (0.80 - 0.0)^2 = 0.64
    assert abs(resolved.brier_score - 0.64) < 0.001


def test_cli_predict_records_hand_entered_authorship():
    from click.testing import CliRunner
    from cli.commands import cli

    runner = CliRunner()
    runner.invoke(cli, ["add-event", "--title", "Manual", "--category", "seismic"])
    event = Event.select().where(Event.title == "Manual").get()
    result = runner.invoke(cli, [
        "predict", str(event.id),
        "--probability", "35",
        "--reasoning", "Hand-entered reasoning",
    ])

    assert result.exit_code == 0, result.output
    stored = Prediction.get()
    assert stored.authorship == PREDICTION_AUTHORSHIP_MANUAL
    assert stored.analysis_run_id is None
