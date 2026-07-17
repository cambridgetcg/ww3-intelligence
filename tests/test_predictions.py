# tests/test_predictions.py
import pytest

from analysis.predictions import create_prediction, resolve_prediction, compute_brier
from core.db import Event, ScenarioNode, Prediction


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
