"""Prediction rows, their recorded authorship, and Brier resolution."""

from __future__ import annotations

from datetime import datetime, timezone

from core.db import (
    PREDICTION_AUTHORSHIP_MODEL,
    PREDICTION_AUTHORSHIP_UNRECORDED,
    PREDICTION_AUTHORSHIPS,
    AnalysisRun,
    Event,
    HORIZON_CODES,
    Prediction,
    ScenarioNode,
)


def compute_brier(probability: int, occurred: bool) -> float:
    """Compute Brier score: (p - o)^2. Lower is better. 0=perfect, 1=worst."""
    p = probability / 100.0
    o = 1.0 if occurred else 0.0
    return (p - o) ** 2


def prediction_authorship(prediction: Prediction) -> str:
    """Report stored authorship; rows predating capture stay unrecorded."""
    return prediction.authorship or PREDICTION_AUTHORSHIP_UNRECORDED


def create_prediction(
    event: Event,
    probability: int,
    reasoning: str,
    confidence: str = "medium",
    timeframe: str | None = None,
    horizon: str | None = None,
    scenario_node: ScenarioNode | None = None,
    analysis_run: AnalysisRun | None = None,
    authorship: str | None = None,
) -> Prediction:
    """Store one forecast with the authorship its caller can actually evidence."""
    if isinstance(probability, bool) or not isinstance(probability, int):
        raise ValueError("probability must be an integer between 0 and 100")
    if not 0 <= probability <= 100:
        raise ValueError("probability must be between 0 and 100")
    if confidence not in {"low", "medium", "high"}:
        raise ValueError("confidence must be low, medium, or high")
    if horizon is not None and horizon not in HORIZON_CODES:
        raise ValueError(f"unsupported prediction horizon: {horizon}")
    if authorship is not None and authorship not in PREDICTION_AUTHORSHIPS:
        raise ValueError(f"unsupported prediction authorship: {authorship}")
    # Model-authored numbers are mutable model state, so they may only be
    # stored against the AnalysisRun that recorded the model, prompt, and code
    # behind them.
    if authorship == PREDICTION_AUTHORSHIP_MODEL and analysis_run is None:
        raise ValueError("model-derived predictions require an analysis run")
    if analysis_run is not None and authorship != PREDICTION_AUTHORSHIP_MODEL:
        raise ValueError("an analysis run may only back model-derived predictions")
    return Prediction.create(
        event=event,
        scenario_node=scenario_node,
        analysis_run=analysis_run,
        authorship=authorship,
        probability=probability,
        confidence=confidence,
        timeframe=timeframe,
        reasoning=reasoning,
        horizon=horizon,
    )


def resolve_prediction(prediction_id: int, outcome: str, occurred: bool) -> Prediction:
    pred = Prediction.get_by_id(prediction_id)
    pred.outcome = outcome
    pred.resolved_at = datetime.now(timezone.utc).replace(tzinfo=None)
    pred.brier_score = compute_brier(pred.probability, occurred)
    pred.save()
    return pred
