"""Explicit model-derived analysis pipeline.

Normal collection lives in :mod:`core.collection` and never imports this
module. Model work is tracked separately as an AnalysisRun, and derived
event/branch rows are written atomically. Replaying an identical source record
for the same analysis contract creates no duplicate Event.
"""

from __future__ import annotations

import json
import hashlib
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from analysis.branches import save_all_branches
from analysis.claude_cli import DEFAULT_MODEL, claude_available
from analysis.llm_processor import load_prompt_contract, process_leaf
from core.db import AnalysisRun, Event, Observation, database, make_analysis_key
from core.observations import get_or_create_observation
from core.settings import stream_path
from core.stream import StreamWriter


STREAM_PATH = stream_path()
ACTIVE_RUN_TTL = timedelta(minutes=15)


def _analysis_code_version() -> str:
    """Hash the local transformation/persistence code used by this process."""
    root = Path(__file__).resolve().parent.parent
    relative_paths = (
        "core/pipeline.py",
        "core/observations.py",
        "analysis/llm_processor.py",
        "analysis/schema.py",
        "analysis/branches.py",
        "analysis/claude_cli.py",
    )
    digest = hashlib.sha256()
    for relative in relative_paths:
        digest.update(relative.encode("utf-8"))
        digest.update((root / relative).read_bytes())
    return f"observation-pipeline-v1:{digest.hexdigest()}"


CODE_VERSION = _analysis_code_version()


class AnalysisLeaseLost(RuntimeError):
    """Raised when a stale worker no longer owns an analysis attempt."""


@dataclass(frozen=True)
class AnalysisClaim:
    run_id: int
    token: str


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _analysis_leaf(observation: Observation) -> dict:
    """Expose only the immutable observation fields needed by the analyser."""
    return {
        "source": observation.source,
        "source_record_id": observation.source_record_id,
        "type": observation.type,
        "raw_text": observation.raw_text,
        "canonical_url": observation.canonical_url,
        "published_at": observation.published_at,
        "fetched_at": observation.fetched_at,
        "geo_lat": observation.geo_lat,
        "geo_lon": observation.geo_lon,
        "location_name": observation.location_name,
        "source_confidence": observation.source_confidence,
        "synthetic": observation.synthetic,
    }


def _claim_analysis(
    observation: Observation,
    *,
    current_prompt: str | None = None,
) -> AnalysisClaim | None:
    """Acquire a fenced lease for this exact analysis version."""
    now = _utcnow_naive()
    if current_prompt is None:
        _, current_prompt = load_prompt_contract()
    analysis_key = make_analysis_key(
        observation.dedupe_key,
        DEFAULT_MODEL,
        current_prompt,
        CODE_VERSION,
    )
    with database.atomic():
        run, _ = AnalysisRun.get_or_create(
            analysis_key=analysis_key,
            defaults={
                "observation": observation,
                "model_name": DEFAULT_MODEL,
                "prompt_version": current_prompt,
                "code_version": CODE_VERSION,
                "status": "pending",
            },
        )
        if run.status == "completed" and run.event_id is not None:
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


def _fail_run(claim: AnalysisClaim, exc: Exception, *, stage: str) -> None:
    """Record failure only while this worker still owns the analysis lease."""
    now = _utcnow_naive()
    (
        AnalysisRun.update(
            status="failed",
            claim_token=None,
            error=type(exc).__name__,
            error_stage=stage,
            completed_at=now,
            updated_at=now,
        )
        .where(
            (AnalysisRun.id == claim.run_id)
            & (AnalysisRun.claim_token == claim.token)
            & (AnalysisRun.status == "running")
        )
        .execute()
    )


def _event_date(observation: Observation):
    timestamp = observation.published_at or observation.fetched_at or _utcnow_naive()
    return timestamp.date()


def _persist_analysis(
    claim: AnalysisClaim,
    observation: Observation,
    result,
) -> tuple[Event, AnalysisRun]:
    """Atomically fence, persist derived rows, and complete the owned run."""
    model_output = asdict(result)
    classification = (
        "reported" if result.classification == "confirmed" else result.classification
    )
    output_json = json.dumps(
        {
            "model_output": model_output,
            "policy": {
                "single_observation_confirmation_capped": (
                    classification != result.classification
                ),
            },
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    now = _utcnow_naive()

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
            raise AnalysisLeaseLost("analysis lease is no longer owned")

        event = Event.create(
            title=result.event_title,
            date=_event_date(observation),
            location_name=observation.location_name,
            lat=observation.geo_lat,
            lon=observation.geo_lon,
            category=result.event_category,
            classification=classification,
            significance=result.significance,
            summary=result.summary,
        )
        save_all_branches(
            event,
            result,
            source_url=observation.canonical_url,
            source_title=result.event_title,
            published_at=observation.published_at,
        )
        completed = (
            AnalysisRun.update(
                event=event,
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
            raise AnalysisLeaseLost("analysis lease changed before commit")

    return event, AnalysisRun.get_by_id(claim.run_id)


def _write_derived_stream(event: Event, observation: Observation, run: AnalysisRun) -> None:
    """Write the compatibility event stream; SQLite remains the source of truth."""
    path = stream_path()
    writer = StreamWriter(path)
    writer.write({
        "observation_id": observation.id,
        "analysis_run_id": run.id,
        "event_id": event.id,
        "title": event.title,
        "category": event.category,
        "processed": True,
    })


def analyze_observation(observation: Observation):
    """Explicitly derive one model-backed Event from a stored Observation.

    Collection does not call this function.  A completed matching analysis, an
    active lease, or a failed attempt returns ``None``; a failed attempt remains
    inspectable and can be retried by explicitly analysing the observation
    again.
    """
    if not claude_available():
        print("[pipeline] Optional model analysis is disabled or unavailable; no run claimed.")
        return None
    try:
        prompt_template, current_prompt = load_prompt_contract()
        claim = _claim_analysis(observation, current_prompt=current_prompt)
        if claim is None:
            return None

        try:
            result = process_leaf(
                _analysis_leaf(observation),
                prompt_template=prompt_template,
            )
        except Exception as exc:
            _fail_run(claim, exc, stage="analysis")
            print(f"[pipeline] Analysis failed ({type(exc).__name__}); observation retained.")
            return None

        try:
            event, run = _persist_analysis(claim, observation, result)
        except Exception as exc:
            _fail_run(claim, exc, stage="persistence")
            print(f"[pipeline] Persistence failed ({type(exc).__name__}); observation retained.")
            return None

        try:
            _write_derived_stream(event, observation, run)
        except Exception as exc:
            # The event and its provenance are already committed.  The JSONL
            # stream is a compatibility view and can be rebuilt later.
            print(f"[pipeline] Stream write failed ({type(exc).__name__}); event retained.")
        return event
    except Exception as exc:
        print(f"[pipeline] Analysis setup failed ({type(exc).__name__}); observation retained.")
        return None


def ingest_leaf(leaf: dict):
    """Compatibility wrapper that captures and explicitly analyses one leaf.

    New collection code should use :mod:`core.collection`; this combined path
    is retained for existing callers and the legacy migration command.
    """
    try:
        observation, _ = get_or_create_observation(leaf)
    except Exception as exc:
        print(f"[pipeline] Observation capture failed ({type(exc).__name__}).")
        return None
    return analyze_observation(observation)


def run_pipeline(leaves: list) -> list:
    """Compatibility batch wrapper for explicit capture-plus-analysis callers."""
    events = []
    for leaf in leaves:
        event = ingest_leaf(leaf)
        if event:
            events.append(event)
    return events
