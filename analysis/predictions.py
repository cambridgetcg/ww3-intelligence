from datetime import datetime, timezone
from core.db import Event, HORIZON_CODES, ScenarioNode, Prediction


def compute_brier(probability: int, occurred: bool) -> float:
    """Compute Brier score: (p - o)^2. Lower is better. 0=perfect, 1=worst."""
    p = probability / 100.0
    o = 1.0 if occurred else 0.0
    return (p - o) ** 2


def create_prediction(
    event: Event,
    probability: int,
    reasoning: str,
    confidence: str = "medium",
    timeframe: str = None,
    horizon: str = None,        # NEW — None = legacy
    scenario_node: ScenarioNode = None,
) -> Prediction:
    if isinstance(probability, bool) or not isinstance(probability, int):
        raise ValueError("probability must be an integer between 0 and 100")
    if not 0 <= probability <= 100:
        raise ValueError("probability must be between 0 and 100")
    if confidence not in {"low", "medium", "high"}:
        raise ValueError("confidence must be low, medium, or high")
    if horizon is not None and horizon not in HORIZON_CODES:
        raise ValueError(f"unsupported prediction horizon: {horizon}")
    return Prediction.create(
        event=event,
        scenario_node=scenario_node,
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
