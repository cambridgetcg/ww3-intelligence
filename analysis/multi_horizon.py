import logging
from pathlib import Path

from core.db import Event, HORIZON_CODES, HORIZON_LABELS, database
from analysis.predictions import create_prediction
from analysis.schema import validate_horizon_payload

_log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "multi_horizon_predictions.txt"
def _call_claude_for_horizons(prompt: str) -> dict:
    # Routed through the local Claude Code CLI (no API key) — see analysis/claude_cli.py
    from analysis.claude_cli import call_claude_json
    return call_claude_json(prompt)


def generate_multi_horizon_predictions(event: Event) -> list:
    """Generate one prediction per horizon for event via Claude. Returns [] on error or if claude CLI is unavailable."""
    from analysis.claude_cli import claude_available
    if not claude_available():
        return []
    template = _PROMPT_PATH.read_text()
    prompt = template.format(
        event_title=event.title,
        event_category=event.category,
        event_date=str(event.date),
        event_location=event.location_name or "unknown",
        event_summary=event.summary or "No summary available.",
        horizon_codes=", ".join(HORIZON_CODES),
    )
    try:
        data = validate_horizon_payload(
            _call_claude_for_horizons(prompt), HORIZON_CODES
        )
    except Exception as exc:
        _log.warning(
            "multi_horizon: analysis failed for event %s (%s)",
            event.id,
            type(exc).__name__,
        )
        return []
    results = []
    with database.atomic():
        for item in data["horizons"]:
            code = item.get("horizon")
            prob = item["probability"]
            conf = item["confidence"]
            outcome = item["outcome"]
            reasoning = item["reasoning"]
            pred = create_prediction(
                event=event,
                probability=prob,
                reasoning=f"{outcome}\n\n{reasoning}".strip(),
                confidence=conf,
                timeframe=HORIZON_LABELS[code],
                horizon=code,
            )
            results.append({
                "horizon": code, "label": HORIZON_LABELS[code],
                "outcome": outcome, "probability": prob,
                "confidence": conf, "reasoning": reasoning,
                "prediction_id": pred.id,
            })
    return results
