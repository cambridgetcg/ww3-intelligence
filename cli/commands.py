import json
import os
import re
from collections import Counter
import click
from datetime import datetime, timezone
from pathlib import Path
from cli.rendering import terminal_line, terminal_text
from core.db import init_db, Event, Observation, Prediction


DEFAULT_INGEST_LIMIT = 200
MAX_INGEST_LIMIT = 1_000
MAX_CAPTURE_INPUT_BYTES = 16 * 1024 * 1024
MAX_CAPTURE_RECORDS = 1_000
MAX_ASSESSMENT_INPUT_BYTES = 2 * 1024 * 1024


@click.group()
@click.pass_context
def cli(ctx):
    """WW3 observation and interpretation framework."""
    # ``ingest --dry-run`` is a true storage preview.  It still performs the
    # selected source fetch, but the command itself decides whether to open the
    # database after it knows the flag value.
    if ctx.invoked_subcommand not in {"ingest", "feed", "sources"}:
        init_db()


def _event_lineage(event):
    runs = list(event.analysis_runs)
    if not runs:
        return "NO-PROVENANCE", runs
    if any(run.observation.synthetic for run in runs):
        return "SYNTHETIC", runs
    return "SOURCED", runs


def _collection_payload(batch) -> dict:
    return {
        "received": batch.received,
        "new": batch.new,
        "known": batch.known,
        "revision": batch.revision,
        "invalid": batch.invalid,
        "outcomes": [
            {
                "status": outcome.status,
                "observation_id": (
                    outcome.observation.id if outcome.observation is not None else None
                ),
                "error_type": outcome.error_type,
            }
            for outcome in batch.outcomes
        ],
    }


def _echo_collection_counts(*, received, new, known, revision, invalid) -> None:
    click.echo(f"Received {received} source record(s).")
    click.echo(
        f"Captured {new + revision} new observation(s): "
        f"{new} first-seen, {revision} revision(s)."
    )
    click.echo(f"Known unchanged: {known}. Invalid: {invalid}.")


def _analysis_preflight() -> None:
    from analysis.claude_cli import claude_available

    if not claude_available():
        raise click.ClickException(
            "Optional model analysis is disabled or the local Claude CLI is unavailable. "
            "No AnalysisRun was claimed. Collection remains available without it."
        )


def _not_found(resource: str, identifier: int, *, as_json: bool = False) -> None:
    message = f"{resource} {identifier} not found."
    if as_json:
        click.echo(json.dumps({
            "error": {
                "code": "not_found",
                "resource": resource.lower(),
                "id": identifier,
            }
        }, sort_keys=True))
        click.get_current_context().exit(1)
    raise click.ClickException(message)


def _analysis_states(observations) -> dict[int, dict]:
    from core.db import AnalysisRun

    observation_ids = [observation.id for observation in observations]
    states = {
        observation_id: {
            "status": "not-requested",
            "run_count": 0,
            "latest_run_id": None,
            "event_id": None,
        }
        for observation_id in observation_ids
    }
    if not observation_ids:
        return states
    runs = (
        AnalysisRun.select()
        .where(AnalysisRun.observation.in_(observation_ids))
        .order_by(AnalysisRun.created_at.desc(), AnalysisRun.id.desc())
    )
    for run in runs:
        state = states[run.observation_id]
        state["run_count"] += 1
        if state["latest_run_id"] is None:
            state.update({
                "status": run.status,
                "latest_run_id": run.id,
                "event_id": run.event_id,
            })
    return states


def _analysis_state(observation: Observation) -> dict:
    return _analysis_states([observation])[observation.id]


def _projected_record(observation: Observation, analysis_state: dict | None = None) -> dict:
    from core.projections import project_observation

    record = project_observation(observation).as_dict()
    record["analysis"] = analysis_state or _analysis_state(observation)
    return record


@cli.command("sources")
@click.option("--json", "as_json", is_flag=True, help="Emit agent-readable JSON.")
def sources(as_json):
    """List source and actor identity registries without opening the database."""
    from core.registry import registry_payload

    payload = registry_payload()
    if as_json:
        click.echo(json.dumps(payload, ensure_ascii=True, sort_keys=True))
        return
    click.echo(
        f"Source registry {payload['source_contract']} — "
        f"{len(payload['sources'])} source(s)"
    )
    for source in payload["sources"]:
        state = "active" if source["collection_enabled"] else "discovery-only"
        click.echo(
            f"  {source['source_id']:16} | {state:14} | "
            f"{source['content_role']:21} | {terminal_line(source['label'])}"
        )
        click.echo(
            f"    control={terminal_line(source['editorial_control_group'])} | "
            f"publisher={terminal_line(source['publisher_actor_id'] or 'unresolved')}"
        )
    click.echo(payload["scope"])


@cli.command("add-event")
@click.option("--title", required=True)
@click.option("--date", default=lambda: str(datetime.now(timezone.utc).date()))
@click.option("--category", default="other")
@click.option("--location", default=None)
@click.option("--significance", default=5, type=int)
@click.option("--summary", default=None)
@click.option("--classification", default="speculation")
def add_event(title, date, category, location, significance, summary, classification):
    """Manually add a new event."""
    event = Event.create(
        title=title, date=date, category=category,
        location_name=location, significance=significance,
        summary=summary, classification=classification,
    )
    click.echo(f"Created event [{event.id}]: {terminal_line(event.title)}")


@cli.command("list")
@click.option("--category", default=None)
def list_events(category):
    """List all events."""
    query = Event.select().order_by(Event.date.desc())
    if category:
        query = query.where(Event.category == category)
    events = list(query)
    if not events:
        click.echo("No events found.")
        return
    for e in events:
        lineage, _ = _event_lineage(e)
        click.echo(
            f"[{e.id}] {e.date} | {lineage:13} | {terminal_line(e.category):10} "
            f"| sig={e.significance} | {terminal_line(e.title)}"
        )


@cli.command("show")
@click.argument("event_id", type=int)
def show_event(event_id):
    """Show full event details."""
    try:
        e = Event.get_by_id(event_id)
    except Event.DoesNotExist:
        click.echo(f"Event {event_id} not found.")
        return
    click.echo(f"\n{'='*60}")
    click.echo(f"[{e.id}] {terminal_line(e.title)}")
    lineage, runs = _event_lineage(e)
    if lineage == "NO-PROVENANCE":
        click.echo("EVIDENCE STATUS: NO PROVENANCE — legacy/manual event; scores are unverified.")
    elif lineage == "SYNTHETIC":
        click.echo("EVIDENCE STATUS: SYNTHETIC FIXTURE — do not treat as intelligence.")
    else:
        observation_ids = ", ".join(str(run.observation_id) for run in runs)
        click.echo(f"EVIDENCE STATUS: SOURCED — observation(s): {observation_ids}")
    click.echo(
        f"Date: {e.date} | Category: {terminal_line(e.category)} "
        f"| Significance: {e.significance}/10"
    )
    click.echo(f"Classification: {terminal_line(e.classification)}")
    if e.summary:
        click.echo(f"\nSummary:\n{terminal_text(e.summary)}")
    actors = list(e.event_actors)
    if actors:
        click.echo(f"\nActors ({len(actors)}):")
        for ea in actors:
            click.echo(
                f"  - {terminal_line(ea.actor.name)} "
                f"({terminal_line(ea.actor.type)}) [{terminal_line(ea.role)}]"
            )
    narratives = list(e.narratives)
    if narratives:
        click.echo(f"\nNarratives ({len(narratives)}):")
        for n in narratives:
            click.echo(
                f"  [{terminal_line(n.narrative_type)}] "
                f"credibility={n.credibility_score}/10: "
                f"{terminal_line(n.content[:80])}"
            )
    root_scenarios = [s for s in e.scenario_nodes if s.parent_id is None]
    if root_scenarios:
        click.echo(f"\nScenario Tree:")
        _print_scenarios(root_scenarios, indent=0)
    escalations = list(e.escalation_states)
    if escalations:
        latest = escalations[-1]
        click.echo(
            f"\nEscalation: {terminal_line(latest.region)} — "
            f"Level {latest.current_level}/10"
        )
    impact_assessments = list(e.impact_assessments)
    if impact_assessments:
        click.echo(f"\nImpact Scores:")
        for ia in impact_assessments:
            click.echo(f"  Military:    regional={ia.military_regional}/10  global={ia.military_global}/10")
            click.echo(f"  Economic:    regional={ia.economic_regional}/10  global={ia.economic_global}/10")
            click.echo(f"  Humanitarian: regional={ia.humanitarian_regional}/10  global={ia.humanitarian_global}/10")
            click.echo(f"  Diplomatic:  regional={ia.diplomatic_regional}/10  global={ia.diplomatic_global}/10")
    predictions = list(e.predictions)
    if predictions:
        click.echo(f"\nPredictions ({len(predictions)}):")
        for p in predictions:
            resolved = f" [resolved: brier={p.brier_score:.4f}]" if p.brier_score is not None else ""
            click.echo(
                f"  [{p.id}] {p.probability}% conf={terminal_line(p.confidence)}"
                f"{resolved}: {terminal_line(p.reasoning[:80])}"
            )


def _print_scenarios(nodes, indent):
    for node in nodes:
        prefix = "  " * indent + ("└─ " if indent > 0 else "")
        click.echo(
            f"{prefix}[{node.probability}%] {terminal_line(node.scenario_text)}"
        )
        children = list(node.children)
        if children:
            _print_scenarios(children, indent + 1)


def _make_slug(title: str) -> str:
    """Generate a URL-friendly slug from a title."""
    slug = title.lower()
    slug = slug.replace(" ", "-")
    slug = re.sub(r"[^a-z0-9\-]", "", slug)
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")
    return slug


@cli.command("ingest")
@click.argument("source", type=click.Choice(["usgs", "gdacs", "news"]))
@click.option(
    "--limit", default=DEFAULT_INGEST_LIMIT, show_default=True,
    type=click.IntRange(1, MAX_INGEST_LIMIT),
    help="Maximum source records returned across this fetch.",
)
@click.option(
    "--analyze", is_flag=True,
    help="Explicitly run the optional model-derived Event layer after capture.",
)
@click.option(
    "--dry-run", is_flag=True,
    help="Fetch and count only; do not open or modify the database.",
)
def ingest(source, limit, analyze, dry_run):
    """Fetch a bounded source batch and capture immutable observations."""
    from core.collection import collect_batch
    from ingestion.seismic_feed import fetch_seismic_events
    from ingestion.gdacs_feed import MAX_GDACS_EVENTS, fetch_gdacs_events
    from ingestion.news_feed import MAX_FEED_ENTRIES, RSS_FEEDS, iter_feed_batches

    def source_batches():
        if source == "usgs":
            click.echo("Fetching bounded USGS seismic records...")
            yield "usgs", fetch_seismic_events(limit=limit)
        elif source == "gdacs":
            click.echo("Fetching bounded GDACS hazard records...")
            yield "gdacs", fetch_gdacs_events(limit=min(limit, MAX_GDACS_EVENTS))
        else:
            click.echo("Fetching bounded news feed records...")
            yield from iter_feed_batches(
                limit_per_feed=MAX_FEED_ENTRIES,
                total_limit=limit,
            )

    if not dry_run:
        init_db()

    totals = Counter(received=0, new=0, known=0, revision=0, invalid=0)
    observations = []
    invalid_types = Counter()
    successful_batches = 0
    for _, leaves in source_batches():
        successful_batches += 1
        totals["received"] += len(leaves)
        if dry_run:
            continue
        batch = collect_batch(leaves)
        for field in ("new", "known", "revision", "invalid"):
            totals[field] += getattr(batch, field)
        observations.extend(batch.observations)
        invalid_types.update(
            outcome.error_type
            for outcome in batch.outcomes
            if outcome.error_type is not None
        )

    if source == "news":
        expected_batches = min(len(RSS_FEEDS), limit)
        click.echo(
            f"News feed fetches succeeded: {successful_batches}/{expected_batches}."
        )
        if expected_batches == 0:
            raise click.ClickException("No news feeds are configured.")
        if successful_batches == 0:
            raise click.ClickException(
                "All configured news feed fetches failed; no records were returned."
            )

    if dry_run:
        click.echo(f"Fetched {totals['received']} source record(s).")
        click.echo("[dry-run] No database open, storage write, or model analysis occurred.")
        return

    _echo_collection_counts(**totals)
    if invalid_types:
        codes = ", ".join(
            f"{name}={count}" for name, count in sorted(invalid_types.items())
        )
        click.echo(f"Invalid record types: {codes}.")

    if not analyze:
        click.echo("Collection complete. No model analysis was requested.")
        return

    _analysis_preflight()
    from core.pipeline import analyze_observation

    events = []
    seen = set()
    analysis_states = Counter()
    for observation in observations:
        if observation.id in seen:
            continue
        seen.add(observation.id)
        event = analyze_observation(observation)
        if event is not None:
            events.append(event)
        else:
            analysis_states[_analysis_state(observation)["status"]] += 1
    click.echo(f"Created {len(events)} new model-derived Event(s).")
    click.echo("Inspect exact analysis state with: python ww3.py runs")
    failed = analysis_states["failed"] + analysis_states["not-requested"]
    if failed:
        raise click.ClickException(
            f"{failed} explicit analysis request(s) failed or did not start; "
            "captured Observations were retained."
        )


@cli.command("capture")
@click.argument("input_file", type=click.File("rb"), default="-")
@click.option("--json", "as_json", is_flag=True, help="Emit agent-readable JSON.")
def capture(input_file, as_json):
    """Capture one JSON record or a JSON array from INPUT_FILE (default: stdin)."""
    from core.collection import collect_batch

    source_bytes = input_file.read(MAX_CAPTURE_INPUT_BYTES + 1)
    if len(source_bytes) > MAX_CAPTURE_INPUT_BYTES:
        raise click.ClickException(
            f"Capture input exceeds {MAX_CAPTURE_INPUT_BYTES} bytes."
        )
    try:
        source_text = source_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise click.ClickException("Capture input must be UTF-8 JSON.") from None
    try:
        payload = json.loads(source_text)
    except json.JSONDecodeError as exc:
        raise click.ClickException(
            f"Capture input is not valid JSON (line {exc.lineno}, column {exc.colno})."
        ) from None
    except (ValueError, RecursionError):
        raise click.ClickException("Capture input is not valid bounded JSON.") from None
    leaves = payload if isinstance(payload, list) else [payload]
    if len(leaves) > MAX_CAPTURE_RECORDS:
        raise click.ClickException(
            f"Capture input exceeds the {MAX_CAPTURE_RECORDS:,}-record batch limit."
        )
    batch = collect_batch(leaves)
    if as_json:
        click.echo(json.dumps({"collection": _collection_payload(batch)}, sort_keys=True))
        return
    _echo_collection_counts(
        received=batch.received,
        new=batch.new,
        known=batch.known,
        revision=batch.revision,
        invalid=batch.invalid,
    )
    click.echo("No model analysis was requested.")


@cli.command("observations")
@click.option("--source", "source_name", default=None)
@click.option("--type", "record_type", default=None)
@click.option("--limit", default=20, show_default=True, type=click.IntRange(1, 1_000))
@click.option("--json", "as_json", is_flag=True, help="Emit agent-readable JSON.")
def list_observations(source_name, record_type, limit, as_json):
    """List the observation inbox; source text stays hidden by default."""
    query = Observation.select().order_by(
        Observation.fetched_at.desc(), Observation.id.desc()
    )
    if source_name:
        query = query.where(Observation.source == source_name.lower())
    if record_type:
        query = query.where(Observation.type == record_type.lower())
    observations = list(query.limit(limit))
    states = _analysis_states(observations)
    records = [
        _projected_record(observation, states[observation.id])
        for observation in observations
    ]
    if as_json:
        click.echo(json.dumps({"observations": records}, sort_keys=True))
        return
    if not records:
        click.echo("No observations found.")
        return
    for record in records:
        item = record["observation"]
        evidence = "SYNTHETIC" if item["synthetic"] else "SOURCE-RECORD"
        revision = (
            f" revision-of={item['supersedes_id']}" if item["supersedes_id"] else ""
        )
        click.echo(
            f"[{item['id']}] {item['fetched_at']} | {evidence:13} | "
            f"{terminal_line(item['source'])}/{terminal_line(item['record_type'])} | "
            f"analysis={terminal_line(record['analysis']['status'])}{revision} | "
            f"{terminal_line(record['title'])}"
        )


@cli.command("interpret")
@click.argument("observation_id", type=int)
@click.option("--json", "as_json", is_flag=True, help="Emit agent-readable JSON.")
def interpret(observation_id, as_json):
    """Render the deterministic, inference-free view of one observation."""
    try:
        observation = Observation.get_by_id(observation_id)
    except Observation.DoesNotExist:
        _not_found("Observation", observation_id, as_json=as_json)
    record = _projected_record(observation)
    if as_json:
        click.echo(json.dumps(record, sort_keys=True))
        return

    item = record["observation"]
    evidence = "SYNTHETIC FIXTURE" if item["synthetic"] else "SOURCE RECORD"
    title_suffix = " [truncated]" if record["title_truncated"] else ""
    click.echo(
        f"[{item['id']}] {terminal_line(record['title'])}{title_suffix}"
    )
    click.echo(f"EVIDENCE STATUS: {evidence}")
    click.echo(
        f"VIEW: {record['projection_version']} — deterministic source projection; "
        "not corroboration, a risk score, or a truth finding."
    )
    click.echo(
        f"Source: {terminal_line(item['source'])} | "
        f"Type: {terminal_line(item['record_type'])} | "
        f"Analysis: {terminal_line(record['analysis']['status'])}"
    )
    if item["supersedes_id"]:
        click.echo(f"Supersedes observation: {item['supersedes_id']}")
    click.echo(f"Published: {item['published_at'] or 'unavailable'}")
    click.echo(f"Fetched: {item['fetched_at']}")
    click.echo(
        f"Canonical URL: {terminal_line(item['canonical_url'] or 'unavailable')}"
    )
    click.echo(f"Content SHA-256: {item['content_sha256']}")
    if record["summary"]:
        suffix = " [truncated]" if record["summary_truncated"] else ""
        click.echo(
            f"Summary (source-provided){suffix}:\n"
            f"{terminal_text(record['summary'])}"
        )
    if record["facts"]:
        click.echo("Structured facts (source-provided):")
        for key, value in record["facts"].items():
            truncated = " [truncated]" if key in record["truncated_fact_fields"] else ""
            click.echo(f"  {key}: {terminal_line(value)}{truncated}")


@cli.command("rhetoric")
@click.argument("observation_id", type=int)
@click.option("--json", "as_json", is_flag=True, help="Emit agent-readable JSON.")
def rhetoric(observation_id, as_json):
    """Explicitly mark visible source-language patterns with local RhetorLint."""
    try:
        observation = Observation.get_by_id(observation_id)
    except Observation.DoesNotExist:
        _not_found("Observation", observation_id, as_json=as_json)

    from interpretation.rhetorlint import (
        RhetorLintError,
        analyze_observation_rhetoric,
    )

    try:
        result = analyze_observation_rhetoric(observation)
    except RhetorLintError as exc:
        if as_json:
            click.echo(json.dumps({
                "error": {"code": exc.code, "observation_id": observation_id}
            }, sort_keys=True))
            click.get_current_context().exit(1)
        raise click.ClickException(
            f"RhetorLint annotation failed ({exc.code}); no output was stored."
        ) from None

    if as_json:
        click.echo(json.dumps(result, ensure_ascii=True, sort_keys=True, allow_nan=False))
        return
    summary = result["summary"]
    click.echo(
        f"Observation [{observation_id}] · {summary['matched_tells']} matched tell(s) "
        f"across {summary['passages']} visible passage(s)"
    )
    for passage in result["passages"]:
        click.echo(
            f"\n{terminal_line(passage['field'])} · "
            f"{passage['result']['density']['tells']} tell(s)"
        )
        for mark in passage["result"]["marks"]:
            click.echo(
                f"  [{terminal_line(mark['family'])}] "
                f"{terminal_line(mark['actual'])} — {terminal_line(mark['note'])}"
            )
    click.echo(f"\nSCOPE: {result['scope']}")


@cli.command("casefile")
@click.argument("observation_ids", type=int, nargs=-1)
@click.option("--limit", default=20, show_default=True, type=click.IntRange(1, 100))
@click.option(
    "--rhetorlint", "include_rhetoric", is_flag=True,
    help="Run the optional local rhetoric annotator (maximum 8 observations).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit agent-readable JSON.")
def casefile(observation_ids, limit, include_rhetoric, as_json):
    """Build an evidence packet with source context and visible actor mentions."""
    from interpretation.casefile import build_casefile

    if observation_ids:
        if len(observation_ids) > 100:
            raise click.ClickException("A casefile accepts at most 100 observations.")
        rows = {
            item.id: item
            for item in Observation.select().where(Observation.id.in_(observation_ids))
        }
        missing = [item for item in observation_ids if item not in rows]
        if missing:
            raise click.ClickException(
                f"Observation(s) not found: {', '.join(map(str, missing))}."
            )
        observations = [rows[item] for item in observation_ids]
    else:
        observations = list(
            Observation.select()
            .order_by(Observation.fetched_at.desc(), Observation.id.desc())
            .limit(limit)
        )
    try:
        payload = build_casefile(
            observations,
            include_rhetoric=include_rhetoric,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None

    if as_json:
        click.echo(json.dumps(payload, ensure_ascii=True, sort_keys=True, allow_nan=False))
        return
    diversity = payload["source_diversity"]
    click.echo(
        f"Casefile {payload['fingerprint_sha256']} · "
        f"{len(payload['observation_ids'])} observation(s) · "
        f"{len(diversity['source_ids'])} claimed source ID(s) · "
        f"{len(diversity['host_consistent_source_ids'])} host-consistent source ID(s) · "
        f"{len(diversity['known_editorial_control_groups'])} known control group(s)"
    )
    if payload["actor_visibility"]:
        click.echo("\nVisible registered actor mentions:")
        for item in payload["actor_visibility"]:
            click.echo(
                f"  {terminal_line(item['actor']['canonical_name'])}: "
                f"{item['mention_count']} mention(s) in "
                f"{len(item['observation_ids'])} observation(s)"
            )
    else:
        click.echo("\nNo registered actor alias matched the selected passages.")
    visibility_status = payload["actor_visibility_status"]
    if not visibility_status["complete"]:
        click.echo(
            "Actor visibility is incomplete for Observation ID(s): "
            + ", ".join(map(str, visibility_status["truncated_observation_ids"]))
            + "."
        )
    if include_rhetoric:
        completed = sum(
            record["rhetoric"].get("status") == "completed"
            for record in payload["observations"]
        )
        click.echo(f"RhetorLint completed for {completed}/{len(observations)} observation(s).")
    click.echo(
        "\nSCOPE: actor mentions are visibility, not power; publisher diversity is not "
        "corroboration; intent is not inferred. Use the ww3.assessment/v1 contract "
        "for evidence-linked, falsifiable intent hypotheses."
    )


@cli.command("validate-assessment")
@click.argument("input_file", type=click.File("rb"), default="-")
@click.option("--json", "as_json", is_flag=True, help="Emit normalized JSON.")
def validate_assessment_command(input_file, as_json):
    """Validate an agent/human claim, power-basis, and intent packet."""
    from interpretation.assessment import (
        AssessmentValidationError,
        validate_assessment,
    )

    source_bytes = input_file.read(MAX_ASSESSMENT_INPUT_BYTES + 1)
    if len(source_bytes) > MAX_ASSESSMENT_INPUT_BYTES:
        raise click.ClickException(
            f"Assessment input exceeds {MAX_ASSESSMENT_INPUT_BYTES} bytes."
        )
    try:
        payload = json.loads(source_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise click.ClickException("Assessment input must be bounded UTF-8 JSON.") from None
    try:
        normalized = validate_assessment(payload)
    except AssessmentValidationError as exc:
        if as_json:
            click.echo(json.dumps({
                "error": {"code": "invalid_assessment", "message": str(exc)}
            }, ensure_ascii=True, sort_keys=True))
            click.get_current_context().exit(1)
        raise click.ClickException(
            f"Invalid assessment: {terminal_line(str(exc))}"
        ) from None
    if as_json:
        click.echo(json.dumps(normalized, ensure_ascii=True, sort_keys=True, allow_nan=False))
        return
    click.echo(
        f"Valid {normalized['contract']} packet: {len(normalized['claims'])} claim(s), "
        f"{len(normalized['power_bases'])} power basis/bases, "
        f"{len(normalized['intent_hypotheses'])} intent hypothesis/hypotheses."
    )
    click.echo(normalized["scope"])


@cli.command("analyze-observation")
@click.argument("observation_id", type=int)
def analyze_observation_command(observation_id):
    """Explicitly run the optional model-derived Event layer for an observation."""
    try:
        observation = Observation.get_by_id(observation_id)
    except Observation.DoesNotExist:
        _not_found("Observation", observation_id)
    _analysis_preflight()
    from core.pipeline import analyze_observation

    event = analyze_observation(observation)
    if event is not None:
        click.echo(
            f"Created model-derived Event [{event.id}] from Observation [{observation.id}]."
        )
        return
    state = _analysis_state(observation)
    if state["status"] in {"failed", "not-requested"}:
        raise click.ClickException(
            f"Explicit analysis {state['status']} "
            f"(run={state['latest_run_id'] or '-'}); Observation retained."
        )
    click.echo(
        f"No new Event created. Latest analysis state: {state['status']} "
        f"(run={state['latest_run_id'] or '-'}, event={state['event_id'] or '-'})."
    )


@cli.command("provenance")
@click.argument("event_id", type=int)
@click.option("--json", "as_json", is_flag=True, help="Emit agent-readable JSON.")
def provenance(event_id, as_json):
    """Show observation and analysis lineage without printing source content."""
    from core.db import AnalysisRun

    try:
        event = Event.get_by_id(event_id)
    except Event.DoesNotExist:
        _not_found("Event", event_id, as_json=as_json)

    runs = list(
        AnalysisRun.select()
        .where(AnalysisRun.event == event)
        .order_by(AnalysisRun.created_at.asc())
    )
    if not runs and not as_json:
        click.echo(f"No observation provenance recorded for event {event_id}.")
        return

    if as_json:
        payload = {
            "event": {
                "id": event.id,
                "title": event.title,
                "date": str(event.date),
                "category": event.category,
                "classification": event.classification,
            },
            "analysis_runs": [],
        }
        for run in runs:
            observation = run.observation
            payload["analysis_runs"].append({
                "id": run.id,
                "status": run.status,
                "attempt": run.attempt,
                "model": run.model_name,
                "prompt_sha256": run.prompt_version,
                "code_version": run.code_version,
                "failure": (
                    {"stage": run.error_stage, "code": run.error}
                    if run.error else None
                ),
                "observation": {
                    "id": observation.id,
                    "supersedes_id": observation.supersedes_id,
                    "source": observation.source,
                    "source_record_id": observation.source_record_id,
                    "type": observation.type,
                    "canonical_url": observation.canonical_url,
                    "published_at": (
                        observation.published_at.isoformat()
                        if observation.published_at else None
                    ),
                    "fetched_at": observation.fetched_at.isoformat(),
                    "content_sha256": observation.content_hash,
                    "source_confidence": observation.source_confidence,
                    "synthetic": observation.synthetic,
                },
            })
        click.echo(json.dumps(payload, ensure_ascii=True, sort_keys=True))
        return

    click.echo(f"Provenance for [{event.id}] {terminal_line(event.title)}")
    for run in runs:
        observation = run.observation
        click.echo(
            f"\nRun {run.id} | {terminal_line(run.status)} "
            f"| model={terminal_line(run.model_name or 'unknown')} "
            f"| attempt={run.attempt} "
            f"| code={terminal_line(run.code_version or 'unknown')}"
        )
        if run.error:
            click.echo(
                f"  Failure: stage={terminal_line(run.error_stage or 'unknown')} "
                f"code={terminal_line(run.error)}"
            )
        click.echo(
            f"  Observation {observation.id} "
            f"| source={terminal_line(observation.source)} "
            f"| type={terminal_line(observation.type)} "
            f"| synthetic={observation.synthetic}"
        )
        if observation.supersedes_id:
            click.echo(f"  Supersedes observation: {observation.supersedes_id}")
        click.echo(
            f"  Source record: "
            f"{terminal_line(observation.source_record_id or 'unavailable')}"
        )
        click.echo(f"  Published: {observation.published_at or 'unavailable'}")
        click.echo(f"  Fetched: {observation.fetched_at}")
        click.echo(
            f"  Canonical URL: "
            f"{terminal_line(observation.canonical_url or 'unavailable')}"
        )
        click.echo(f"  Content SHA-256: {observation.content_hash}")
        click.echo(f"  Prompt SHA-256: {run.prompt_version or 'unavailable'}")


@cli.command("runs")
@click.option(
    "--status",
    "run_status",
    type=click.Choice(["pending", "running", "committing", "completed", "failed"]),
)
@click.option("--json", "as_json", is_flag=True, help="Emit agent-readable JSON.")
def list_runs(run_status, as_json):
    """List pipeline runs, including failures that have no Event."""
    from core.db import AnalysisRun

    query = AnalysisRun.select().order_by(AnalysisRun.created_at.desc())
    if run_status:
        query = query.where(AnalysisRun.status == run_status)
    records = []
    for run in query:
        observation = run.observation
        records.append({
            "id": run.id,
            "status": run.status,
            "attempt": run.attempt,
            "event_id": run.event_id,
            "observation_id": observation.id,
            "source": observation.source,
            "source_record_id": observation.source_record_id,
            "content_sha256": observation.content_hash,
            "synthetic": observation.synthetic,
            "model": run.model_name,
            "prompt_sha256": run.prompt_version,
            "code_version": run.code_version,
            "failure_stage": run.error_stage,
            "failure_code": run.error,
        })

    if as_json:
        click.echo(json.dumps({"analysis_runs": records}, ensure_ascii=True, sort_keys=True))
        return
    if not records:
        click.echo("No analysis runs found.")
        return
    for record in records:
        failure = ""
        if record["failure_code"]:
            failure = (
                f" | failure={terminal_line(record['failure_stage'] or 'unknown')}/"
                f"{terminal_line(record['failure_code'])}"
            )
        click.echo(
            f"[{record['id']}] {record['status']:10} | attempt={record['attempt']} "
            f"| observation={record['observation_id']} | event={record['event_id'] or '-'} "
            f"| source={terminal_line(record['source'])}{failure}"
        )


@cli.command("migrate-legacy-event")
@click.argument("event_id", type=int)
def migrate_legacy_event(event_id):
    """Migrate an unanalyzed legacy RawLeaf through the versioned pipeline."""
    try:
        event = Event.get_by_id(event_id)
    except Event.DoesNotExist:
        click.echo(f"Event {event_id} not found.")
        return

    if event.analysis_runs.count():
        click.echo(
            "In-place reanalysis is disabled: this event already has versioned provenance. "
            "Ingest a corrected source revision to create a new auditable analysis."
        )
        return

    leaves = [leaf for leaf in event.raw_leaves if not leaf.processed]
    if not leaves:
        click.echo("No unprocessed legacy raw leaves are attached to this event.")
        return

    from core.db import AnalysisRun
    from core.observations import get_or_create_observation, observation_identity

    def completed_event_for(observation):
        if observation is None:
            return None
        completed_run = (
            AnalysisRun.select()
            .where(
                (AnalysisRun.observation == observation)
                & (AnalysisRun.status == "completed")
                & AnalysisRun.event.is_null(False)
            )
            .order_by(AnalysisRun.completed_at.desc())
            .first()
        )
        return completed_run.event if completed_run is not None else None

    pending = []
    linked_events = []
    for raw_leaf in leaves:
        leaf = {
            "raw_text": raw_leaf.raw_text,
            "source": raw_leaf.source,
            "source_record_id": f"legacy-rawleaf:{raw_leaf.id}",
            "type": raw_leaf.type,
            "location_name": raw_leaf.location_name,
            "geo_lat": raw_leaf.geo_lat,
            "geo_lon": raw_leaf.geo_lon,
            "source_confidence": raw_leaf.confidence,
            "published_at": None,
            "fetched_at": raw_leaf.ts,
        }
        identity = observation_identity(leaf)
        existing = Observation.get_or_none(Observation.dedupe_key == identity)
        completed_event = completed_event_for(existing)
        if completed_event is not None:
            raw_leaf.processed = True
            raw_leaf.save(only=[type(raw_leaf).processed])
            linked_events.append(completed_event)
        else:
            pending.append((raw_leaf, leaf))

    if pending:
        # A completed post-commit retry can heal its legacy marker without a
        # model. Any genuinely new migration must pass the same two-key model
        # preflight as normal observation analysis before capture occurs.
        _analysis_preflight()
        from core.pipeline import ingest_leaf

    failed = 0
    for raw_leaf, leaf in pending:
        derived_event = ingest_leaf(leaf)
        completed_event = derived_event
        if completed_event is None:
            observation, _ = get_or_create_observation(leaf)
            completed_event = completed_event_for(observation)

        if completed_event is not None:
            raw_leaf.processed = True
            raw_leaf.save(only=[type(raw_leaf).processed])
            linked_events.append(completed_event)
        else:
            failed += 1

    if linked_events:
        ids = ", ".join(str(item.id) for item in linked_events)
        click.echo(
            f"Linked {len(linked_events)} legacy observations to auditable event(s): {ids}. "
            f"Original event [{event_id}] was not modified."
        )
    if failed:
        raise click.ClickException(
            f"{failed} legacy observation analysis request(s) failed; "
            "RawLeaf rows remain unprocessed."
        )


@cli.command("predict")
@click.argument("event_id", type=int)
@click.option("--probability", required=True, type=click.IntRange(0, 100))
@click.option("--reasoning", required=True)
@click.option("--timeframe", default=None)
@click.option(
    "--confidence",
    default="medium",
    type=click.Choice(["low", "medium", "high"]),
)
def predict(event_id, probability, reasoning, timeframe, confidence):
    """Add a prediction to an event."""
    try:
        event = Event.get_by_id(event_id)
    except Event.DoesNotExist:
        click.echo(f"Event {event_id} not found.")
        return
    from analysis.predictions import create_prediction
    pred = create_prediction(event, probability, reasoning, confidence=confidence, timeframe=timeframe)
    click.echo(
        f"Created prediction [{pred.id}]: {probability}% — "
        f"{terminal_line(reasoning[:60])}"
    )


@cli.command("predict-horizons")
@click.argument("event_id", type=int)
@click.option("--manual", is_flag=True, default=False,
              help="Enter each horizon manually instead of using LLM.")
def predict_horizons(event_id, manual):
    """Generate multi-horizon predictions for an event (all timescales at once)."""
    try:
        event = Event.get_by_id(event_id)
    except Event.DoesNotExist:
        click.echo(f"Event {event_id} not found.")
        return

    if manual:
        from core.db import HORIZON_CODES, HORIZON_LABELS
        from analysis.predictions import create_prediction as cp
        click.echo(
            f"Manual entry for event [{event_id}]: {terminal_line(event.title)}"
        )
        click.echo("Enter predictions per horizon. Press Enter to skip.\n")
        created = 0
        for code in HORIZON_CODES:
            label = HORIZON_LABELS[code]
            click.echo(f"--- {label} ({code}) ---")
            reasoning = click.prompt(
                "  Predicted outcome (or blank to skip)", default="", show_default=False
            )
            if not reasoning.strip():
                continue
            prob = click.prompt("  Probability (0-100)", type=click.IntRange(0, 100))
            conf = click.prompt("  Confidence", type=click.Choice(["low", "medium", "high"]),
                                default="medium")
            cp(event, prob, reasoning.strip(), conf, label, horizon=code)
            created += 1
        click.echo(f"\nCreated {created} horizon predictions.")
        return

    from analysis.claude_cli import claude_available
    if not claude_available():
        click.echo("Claude is unavailable or disabled. Use --manual for manual entry.")
        return

    click.echo(
        f"Generating multi-horizon predictions for [{event_id}]: "
        f"{terminal_line(event.title)}"
    )
    click.echo("Calling the constrained local Claude JSON boundary...")
    from analysis.multi_horizon import generate_multi_horizon_predictions
    results = generate_multi_horizon_predictions(event)
    if not results:
        click.echo("No predictions generated. Check Claude availability and event data.")
        return
    click.echo(f"\nGenerated {len(results)} horizon predictions:\n")
    for r in results:
        click.echo(
            f"  [{r['horizon']:4s}] {r['probability']:3d}%  conf={r['confidence']:6s}  "
            f"id={r['prediction_id']}  {terminal_line(r['outcome'])}"
        )
    click.echo(f"\nAll saved. View with: python ww3.py show {event_id}")


@cli.command("resolve")
@click.argument("prediction_id", type=int)
@click.option("--outcome", required=True)
@click.option("--occurred/--did-not-occur", required=True)
def resolve(prediction_id, outcome, occurred):
    """Resolve a prediction and compute Brier score."""
    from analysis.predictions import resolve_prediction
    try:
        pred = resolve_prediction(prediction_id, outcome, occurred)
    except Prediction.DoesNotExist:
        click.echo(f"Prediction {prediction_id} not found.")
        return
    click.echo(f"Resolved prediction [{pred.id}]: Brier score = {pred.brier_score:.4f}")


@cli.command("report")
@click.argument("event_id", type=int)
@click.option("--output", default=None, help="Override output path for markdown file")
def report(event_id, output):
    """Generate a markdown intelligence report for an event."""
    try:
        e = Event.get_by_id(event_id)
    except Event.DoesNotExist:
        click.echo(f"Event {event_id} not found.")
        return
    lines = [f"# Intelligence Report: {e.title}", ""]
    lineage, runs = _event_lineage(e)
    if lineage == "NO-PROVENANCE":
        lines.append(
            "> **NO PROVENANCE.** Legacy/manual event; analytical scores are unverified."
        )
    elif lineage == "SYNTHETIC":
        lines.append(
            "> **SYNTHETIC FIXTURE.** Do not publish or treat as factual intelligence."
        )
    else:
        observation_ids = ", ".join(str(run.observation_id) for run in runs)
        lines.append(f"**Evidence observations:** {observation_ids}")
    lines.extend([
        f"**Date:** {e.date} | **Category:** {e.category} | **Significance:** {e.significance}/10",
        f"**Classification:** {e.classification}",
        f"\n## Summary\n{e.summary or 'N/A'}",
    ])
    content = "\n".join(lines)
    if not output:
        date_str = str(e.date)
        slug = _make_slug(e.title)
        output = f"data/reports/{date_str}-{slug}.md"
    out_path = Path(output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.touch(mode=0o600, exist_ok=True)
    os.chmod(out_path, 0o600)
    out_path.write_text(content)
    click.echo(f"Report saved to {terminal_line(output)}")
    click.echo(terminal_text(content))


@cli.command("feed")
@click.option(
    "--watch", is_flag=True, default=False,
    help="Collect bounded USGS + RSS observations in background.",
)
def feed(watch):
    """Live observation feed; optional terminal dependency required."""
    try:
        from cli.feed import run_feed
    except ModuleNotFoundError as exc:
        if exc.name == "rich":
            raise click.ClickException(
                "The terminal feed is optional. Install requirements-tui.txt."
            ) from None
        raise
    run_feed(watch=watch)
