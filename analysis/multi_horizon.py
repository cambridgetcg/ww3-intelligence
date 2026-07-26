"""Explicit model-derived multi-horizon forecasts for one stored Event.

This is a compatibility wrapper around the AnalysisRun contract in
:mod:`core.pipeline`.  A forecast set is mutable model state — later resolved
in place with an outcome and a Brier score — so it claims a fenced, versioned
run of its own before any Prediction row exists.  Collection never reaches
this module.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from pathlib import Path

from analysis.claude_cli import DEFAULT_MODEL
from analysis.predictions import create_prediction
from analysis.schema import validate_horizon_payload
from core.db import (
    HORIZON_CODES,
    HORIZON_FORECAST_CONTRACT,
    HORIZON_LABELS,
    PREDICTION_AUTHORSHIP_MODEL,
    AnalysisRun,
    Event,
    Observation,
    database,
    make_forecast_key,
    observation_analysis_runs,
)

# The lease, fence token, and failure record are the pipeline's own primitives.
# A second implementation of them would be a second contract, not a wrapper.
from core.pipeline import (
    ACTIVE_RUN_TTL,
    AnalysisClaim,
    AnalysisLeaseLost,
    _fail_run,
    _utcnow_naive,
)

_log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "multi_horizon_predictions.txt"


def _forecast_code_version() -> str:
    """Hash the local forecast/persistence code used by this process."""
    root = Path(__file__).resolve().parent.parent
    relative_paths = (
        "analysis/multi_horizon.py",
        "analysis/predictions.py",
        "analysis/schema.py",
        "analysis/claude_cli.py",
    )
    digest = hashlib.sha256()
    for relative in relative_paths:
        digest.update(relative.encode("utf-8"))
        digest.update((root / relative).read_bytes())
    return f"{HORIZON_FORECAST_CONTRACT}:{digest.hexdigest()}"


CODE_VERSION = _forecast_code_version()


def _call_claude_for_horizons(prompt: str) -> dict:
    # Routed through the local Claude Code CLI (no API key) — see analysis/claude_cli.py
    from analysis.claude_cli import call_claude_json
    return call_claude_json(prompt)


def load_forecast_contract() -> tuple[str, str]:
    """Load the exact forecast prompt text and its SHA-256 identifier together."""
    template = _PROMPT_PATH.read_text()
    return template, hashlib.sha256(template.encode("utf-8")).hexdigest()


def forecast_prompt(template: str, event: Event) -> str:
    """Render the bounded forecast question this Event puts to the model."""
    return template.format(
        event_title=event.title,
        event_category=event.category,
        event_date=str(event.date),
        event_location=event.location_name or "unknown",
        event_summary=event.summary or "No summary available.",
        horizon_codes=", ".join(HORIZON_CODES),
    )


def forecast_subject_key(event: Event, prompt: str) -> str:
    """Identify one forecast question: this Event, exactly as the prompt states it.

    Events are mutable, so the rendered prompt carries the content identity an
    Observation dedupe key carries for the analysis contract.  The Event ID is
    digested separately because two distinct Events can read identically.
    """
    encoded = json.dumps(
        [event.id, prompt], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def forecast_source_observation(event: Event) -> Observation | None:
    """Return the observation whose sanctioned analysis produced this Event."""
    # AnalysisRun is anchored to an Observation, so an Event with no capture
    # lineage cannot host a recorded forecast run at all.  The forecast prompt
    # itself sees only the derived Event fields.
    run = (
        observation_analysis_runs()
        .where(AnalysisRun.event == event)
        .order_by(AnalysisRun.created_at.asc(), AnalysisRun.id.asc())
        .first()
    )
    return run.observation if run is not None else None


def forecast_run_state(event: Event) -> dict:
    """Report the latest recorded forecast run for one Event."""
    run = (
        AnalysisRun.select()
        .where(
            (AnalysisRun.event == event)
            & (AnalysisRun.contract == HORIZON_FORECAST_CONTRACT)
        )
        .order_by(AnalysisRun.created_at.desc(), AnalysisRun.id.desc())
        .first()
    )
    if run is None:
        return {"status": "not-requested", "run_id": None, "prediction_count": 0}
    return {
        "status": run.status,
        "run_id": run.id,
        "prediction_count": run.predictions.count(),
    }


def _claim_forecast(
    event: Event,
    observation: Observation,
    prompt: str,
    prompt_version: str,
) -> AnalysisClaim | None:
    """Acquire a fenced lease for this exact forecast version."""
    now = _utcnow_naive()
    analysis_key = make_forecast_key(
        forecast_subject_key(event, prompt),
        DEFAULT_MODEL,
        prompt_version,
        CODE_VERSION,
    )
    with database.atomic():
        run, _ = AnalysisRun.get_or_create(
            analysis_key=analysis_key,
            defaults={
                "observation": observation,
                # The subject Event exists before the model is called, so even
                # a failed forecast stays visible to ``ww3.py provenance``.
                "event": event,
                "contract": HORIZON_FORECAST_CONTRACT,
                "model_name": DEFAULT_MODEL,
                "prompt_version": prompt_version,
                "code_version": CODE_VERSION,
                "status": "pending",
            },
        )
        if run.status == "completed":
            return None
        if (
            run.status == "running"
            and run.started_at is not None
            and run.started_at >= now - ACTIVE_RUN_TTL
        ):
            return None

        token = uuid.uuid4().hex
        claimed = (
            AnalysisRun.update(
                status="running",
                claim_token=token,
                attempt=AnalysisRun.attempt + 1,
                output_json=None,
                error=None,
                error_stage=None,
                started_at=now,
                completed_at=None,
                updated_at=now,
            )
            .where(
                (AnalysisRun.id == run.id)
                & (AnalysisRun.attempt == run.attempt)
                & (AnalysisRun.status != "completed")
            )
            .execute()
        )
        if claimed != 1:
            return None
        return AnalysisClaim(run_id=run.id, token=token)


def _persist_forecast(claim: AnalysisClaim, event: Event, data: dict) -> list[dict]:
    """Atomically fence, persist the forecast set, and complete the owned run."""
    output_json = json.dumps({"model_output": data}, ensure_ascii=False, sort_keys=True)
    now = _utcnow_naive()
    results = []

    with database.atomic():
        fenced = (
            AnalysisRun.update(status="committing", updated_at=now)
            .where(
                (AnalysisRun.id == claim.run_id)
                & (AnalysisRun.claim_token == claim.token)
                & (AnalysisRun.status == "running")
            )
            .execute()
        )
        if fenced != 1:
            raise AnalysisLeaseLost("forecast lease is no longer owned")

        run = AnalysisRun.get_by_id(claim.run_id)
        for item in data["horizons"]:
            code = item["horizon"]
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
                analysis_run=run,
                authorship=PREDICTION_AUTHORSHIP_MODEL,
            )
            results.append({
                "horizon": code, "label": HORIZON_LABELS[code],
                "outcome": outcome, "probability": prob,
                "confidence": conf, "reasoning": reasoning,
                "prediction_id": pred.id,
                "analysis_run_id": run.id,
            })

        completed = (
            AnalysisRun.update(
                status="completed",
                claim_token=None,
                output_json=output_json,
                error=None,
                error_stage=None,
                completed_at=now,
                updated_at=now,
            )
            .where(
                (AnalysisRun.id == claim.run_id)
                & (AnalysisRun.claim_token == claim.token)
                & (AnalysisRun.status == "committing")
            )
            .execute()
        )
        if completed != 1:
            raise AnalysisLeaseLost("forecast lease changed before commit")

    return results


def generate_multi_horizon_predictions(event: Event) -> list:
    """Explicitly derive one fenced, model-authored forecast set for an Event.

    Returns ``[]`` without model work when the boundary is unavailable, when
    the Event carries no observation lineage to record a run against, or when
    an identical forecast is already complete.  A failed attempt records its
    stage and stays inspectable through ``ww3.py runs``.
    """
    from analysis.claude_cli import claude_available

    if not claude_available():
        return []
    try:
        observation = forecast_source_observation(event)
        if observation is None:
            _log.warning(
                "multi_horizon: event %s has no observation lineage for a run",
                event.id,
            )
            return []
        template, prompt_version = load_forecast_contract()
        prompt = forecast_prompt(template, event)
        claim = _claim_forecast(event, observation, prompt, prompt_version)
        if claim is None:
            return []

        try:
            data = validate_horizon_payload(
                _call_claude_for_horizons(prompt), HORIZON_CODES
            )
        except Exception as exc:
            _fail_run(claim, exc, stage="analysis")
            _log.warning(
                "multi_horizon: analysis failed for event %s (%s)",
                event.id,
                type(exc).__name__,
            )
            return []

        try:
            return _persist_forecast(claim, event, data)
        except Exception as exc:
            _fail_run(claim, exc, stage="persistence")
            _log.warning(
                "multi_horizon: persistence failed for event %s (%s)",
                event.id,
                type(exc).__name__,
            )
            return []
    except Exception as exc:
        _log.warning(
            "multi_horizon: forecast setup failed for event %s (%s)",
            event.id,
            type(exc).__name__,
        )
        return []
