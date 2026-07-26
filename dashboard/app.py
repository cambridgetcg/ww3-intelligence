"""Optional observation-first dashboard over the local collection ledger.

This is the only publicly reachable surface, so every value it renders states
what wrote it: a bounded collector capture, an operator, or a specific
AnalysisRun.  The module itself computes no significance, credibility,
corroboration, or risk.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from core.db import database, init_db


OBSERVATION_VIEW_LIMIT = 500
LEGACY_CAPTURE_HINT_COLUMN = "Legacy Capture Hint"
LEGACY_CAPTURE_HINT_SCOPE = (
    "Legacy Capture Hint: legacy collector-supplied capture/parse hint only; it "
    "is not source credibility, factual confidence, corroboration, or truth, and "
    "new adapters default it to not-assessed."
)
COLLECTION_HEALTH_SCOPE = (
    "Collection cycles report whether each group last recorded a capture "
    "success recently. They are not the health, reliability, or credibility of "
    "a publisher, and a recent cycle is not evidence that any captured record "
    "is true."
)
WRITER_ENTERED_SCOPE = (
    "Significance, classification, credibility, escalation, impact, scenario, "
    "and prediction numbers on this page were written by the model layer or by "
    "an operator. This framework does not compute, corroborate, or verify them."
)
COLLECTION_STATE_MEANINGS = {
    "fresh": "a capture success was recorded within the expected interval",
    "stale": "no capture success was recorded within the expected interval",
    "never-observed": "no capture success has ever been recorded for this key",
    "unknown": "the collector heartbeat could not be read for this key",
}


@st.cache_resource(show_spinner=False)
def _initialize_database():
    """Run schema/integrity startup once per dashboard process."""
    init_db()
    database.close()
    return True


def _event_lineage(event):
    # A forecast run is attached to the same Event but never reads an
    # observation's text and never writes the Event's own fields, so counting
    # one here would credit it with provenance it did not produce.
    from core.db import AnalysisRun, observation_analysis_runs

    runs = list(observation_analysis_runs().where(AnalysisRun.event == event))
    if not runs:
        return "NO PROVENANCE", runs
    if any(run.observation.synthetic for run in runs):
        return "SYNTHETIC", runs
    return "SOURCED", runs


@dataclass(frozen=True, slots=True)
class FieldOrigin:
    """What wrote an Event's stored fields, stated wherever they are shown."""

    kind: str
    detail: str

    def metric_label(self, field: str) -> str:
        """Return a KPI label that carries its own provenance."""

        return f"{field} ({self.kind})"


def event_field_origin(analysis_runs) -> FieldOrigin:
    """Name what wrote an Event's stored fields: model runs or an operator."""

    runs = list(analysis_runs)
    if not runs:
        return FieldOrigin(
            "hand-entered",
            "No AnalysisRun wrote these fields: they were entered by an operator "
            "or carried over from a legacy record.",
        )
    identifiers = ", ".join(str(run.id) for run in runs)
    models = ", ".join(sorted({run.model_name or "unknown" for run in runs}))
    return FieldOrigin(
        "model-derived",
        f"Written through AnalysisRun {identifiers} ({models}). Model output is "
        "derived material about the linked observations, not a property of them.",
    )


def bounded_view_notice(shown: int, total: int) -> str:
    """State how much of the captured ledger a bounded page can display."""

    if shown >= total:
        return f"Showing all {total:,} captured observations."
    return (
        f"Showing the {shown:,} most recently fetched of {total:,} captured "
        "observations. This view is bounded: the oldest row below is not the "
        "oldest record held."
    )


def observation_page(limit: int = OBSERVATION_VIEW_LIMIT):
    """Return one bounded page of captures and the full captured total."""

    from core.db import Observation

    total = Observation.select().count()
    observations = list(
        Observation.select()
        .order_by(Observation.fetched_at.desc(), Observation.id.desc())
        .limit(limit)
    )
    return observations, total


def observation_row(observation, projection, analysis_status: str) -> dict:
    """Shape one captured observation and its projection into a table row."""

    return {
        "ID": observation.id,
        "Evidence Status": "SYNTHETIC" if observation.synthetic else "SOURCE-RECORD",
        "Source": observation.source,
        "Type": observation.type,
        "Title": projection.title,
        "Published": projection.published_at,
        "Fetched": projection.fetched_at,
        "Revision Of": observation.supersedes_id,
        "Analysis": analysis_status,
        LEGACY_CAPTURE_HINT_COLUMN: observation.source_confidence,
        "Canonical URL": observation.canonical_url,
        "Content SHA-256": observation.content_hash,
    }


def event_timeline_row(event, lineage: str, origin: FieldOrigin) -> dict:
    """Shape one derived Event into a timeline row that names its writer."""

    return {
        "ID": event.id,
        "Title": event.title,
        "Date": str(event.date),
        "Evidence Status": lineage,
        "Field Origin": origin.kind,
        "Category": event.category,
        "Claimed Significance": event.significance,
        "Claimed Classification": event.classification,
    }


def narrative_heading(narrative, origin: FieldOrigin) -> str:
    """Label one narrative so its credibility number keeps its writer."""

    return (
        f"{narrative.narrative_type} — claimed credibility "
        f"{narrative.credibility_score}/10 ({origin.kind})"
    )


def _format_duration(seconds) -> str | None:
    """Render a whole-unit duration, or None when it cannot be displayed."""

    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3_600:
        return f"{total // 60}m"
    if total < 86_400:
        return f"{total // 3_600}h"
    return f"{total // 86_400}d"


def collection_health_rows(health) -> list[dict]:
    """Shape collector health entries into per-key capture-cycle rows."""

    rows = []
    for entry in health.entries:
        age = _format_duration(entry.age_seconds)
        expected = _format_duration(entry.expected_max_age_seconds)
        rows.append({
            "Collection Key": entry.key,
            "Kind": entry.kind,
            "Cycle State": entry.state,
            "Last Capture Success": f"{age} ago" if age else "unavailable",
            "Expected Within": expected or "unavailable",
            "Meaning": COLLECTION_STATE_MEANINGS.get(entry.state, "unreported state"),
        })
    return rows


def unattested_collection_keys(health) -> tuple[str, ...]:
    """Name keys holding no capture success, which staleness cannot describe."""

    return tuple(
        entry.key
        for entry in health.entries
        if entry.state in {"never-observed", "unknown"}
    )


def render_collection_health() -> None:
    """Show whether each collection group recorded a capture success recently."""

    # core/collector.py owns the cycle intervals and the staleness threshold; a
    # reporter this build cannot import must read as unknown, never as collected.
    try:
        from core.collector import collection_health
    except ImportError:
        st.warning(
            "Collection-cycle reporting is unavailable in this build, so feed "
            "staleness is not shown here."
        )
        return

    health = collection_health()
    st.subheader("Collection Cycles")
    if health.status_file_state != "present":
        st.warning(
            f"The collector heartbeat is {health.status_file_state}; the cycle "
            "states below describe the heartbeat, not the collectors."
        )
    rows = collection_health_rows(health)
    if rows:
        st.dataframe(pd.DataFrame(rows), width="stretch")
    stale = health.stale_keys()
    if stale:
        st.warning(
            "No capture success within the expected interval for: "
            f"{', '.join(stale)}."
        )
    unattested = unattested_collection_keys(health)
    if unattested:
        st.warning(
            f"No capture success has ever been recorded for: {', '.join(unattested)}."
        )
    st.caption(COLLECTION_HEALTH_SCOPE)


def page_observations():
    from core.db import AnalysisRun
    from core.projections import project_observation

    st.title("Source Observations")
    st.caption(
        "Immutable collected records and deterministic source projections. "
        "They are not corroboration, risk scores, or truth findings."
    )
    render_collection_health()
    st.subheader("Captured Records")
    observations, total = observation_page()
    if not observations:
        st.info("No observations captured yet. Run `python ww3.py ingest SOURCE`.")
        return
    latest_runs = {}
    observation_ids = [observation.id for observation in observations]
    for run in (
        AnalysisRun.select()
        .where(AnalysisRun.observation.in_(observation_ids))
        .order_by(AnalysisRun.created_at.desc(), AnalysisRun.id.desc())
    ):
        latest_runs.setdefault(run.observation_id, run)
    rows = []
    for observation in observations:
        latest_run = latest_runs.get(observation.id)
        rows.append(observation_row(
            observation,
            project_observation(observation),
            latest_run.status if latest_run else "not-requested",
        ))
    frame = pd.DataFrame(rows)
    if (frame["Evidence Status"] == "SYNTHETIC").any():
        st.warning("Synthetic fixtures are present and are labeled before source fields.")
    st.info(bounded_view_notice(len(observations), total))
    st.dataframe(frame, width="stretch")
    st.caption(LEGACY_CAPTURE_HINT_SCOPE)


def page_timeline():
    from core.db import Event
    st.title("Model / Manual Event Timeline")
    st.caption(WRITER_ENTERED_SCOPE)
    events = list(Event.select().order_by(Event.date.desc()))
    if not events:
        st.info(
            "No derived Events yet. Collection-only observations remain available "
            "on the Source Observations page."
        )
        return
    rows = []
    for event in events:
        lineage, runs = _event_lineage(event)
        rows.append(event_timeline_row(event, lineage, event_field_origin(runs)))
    df = pd.DataFrame(rows)
    if (df["Evidence Status"] != "SOURCED").any():
        st.warning(
            "Legacy/manual or synthetic events are present. Evidence status precedes all scores."
        )
    fig = px.scatter(df, x="Date", y="Claimed Significance", color="Category",
                     hover_data=["Title", "Evidence Status", "Field Origin",
                                 "Claimed Classification"],
                     size="Claimed Significance", symbol="Evidence Status",
                     title="Events by claimed significance over time")
    st.plotly_chart(fig, width="stretch")
    st.dataframe(df, width="stretch")


def page_event(event_id: int):
    from core.db import Event
    try:
        event = Event.get_by_id(event_id)
    except Exception:
        st.error(f"Event {event_id} not found.")
        return
    st.title("Event Intelligence")
    st.text(f"[{event.category.upper()}] {event.title}")

    # Evidence provenance comes before model-derived analytical branches, and
    # covers only the runs that read an observation.  A forecast run carries no
    # observation lineage, so listing it here would repeat one source record as
    # though a second one had been captured.
    from core.db import AnalysisRun, observation_analysis_runs
    analysis_runs = list(
        observation_analysis_runs()
        .where(AnalysisRun.event == event)
        .order_by(AnalysisRun.created_at.asc())
    )
    if analysis_runs:
        st.subheader("Evidence & Provenance")
        provenance_rows = []
        for run in analysis_runs:
            observation = run.observation
            provenance_rows.append({
                "Source": observation.source,
                "Source Record": observation.source_record_id or "Unavailable",
                "Supersedes Observation": observation.supersedes_id,
                "Published": observation.published_at,
                "Fetched": observation.fetched_at,
                "Canonical URL": observation.canonical_url or "Unavailable",
                "Content SHA-256": observation.content_hash,
                "Synthetic": observation.synthetic,
                "Analysis Status": run.status,
                "Attempt": run.attempt,
                "Failure Stage": run.error_stage or "",
                "Failure Code": run.error or "",
                "Model": run.model_name or "Unknown",
                "Code Version": run.code_version or "Unknown",
                LEGACY_CAPTURE_HINT_COLUMN: observation.source_confidence,
            })
        if any(row["Synthetic"] for row in provenance_rows):
            st.warning("This event includes synthetic or test observations.")
        st.dataframe(pd.DataFrame(provenance_rows), width="stretch")
        st.caption(
            "The source record is immutable; event fields and analytical branches are derived views."
        )
        st.caption(LEGACY_CAPTURE_HINT_SCOPE)
    else:
        st.warning("This legacy event has no observation-level provenance record.")

    origin = event_field_origin(analysis_runs)
    col1, col2 = st.columns(2)
    col1.metric(origin.metric_label("Significance"), f"{event.significance}/10")
    col2.metric(origin.metric_label("Classification"), event.classification)
    st.caption(f"{origin.detail} {WRITER_ENTERED_SCOPE}")
    if event.summary:
        st.subheader("Summary")
        st.text(event.summary)

    # Actors
    actors = list(event.event_actors)
    if actors:
        st.subheader("Actors")
        st.caption(
            f"Actor rows and their capability tier are {origin.kind} entries. "
            "Appearing here is visibility in derived material, not power, "
            "responsibility, or intent."
        )
        st.table(pd.DataFrame([{
            "Name": ea.actor.name,
            "Type": ea.actor.type,
            "Country": ea.actor.country,
            "Role": ea.role,
            "Claimed Capability Tier": ea.actor.capability_tier,
        } for ea in actors]))

    # Narratives
    narratives = list(event.narratives)
    if narratives:
        st.subheader("Competing Narratives")
        st.caption(
            "Narrative text and its credibility number are claimed values "
            f"({origin.kind}). This framework does not assess a publisher, a "
            "narrative, or a claim."
        )
        for n in narratives:
            with st.expander(narrative_heading(n, origin)):
                st.text(n.content)

    # Escalation gauge
    from core.db import EscalationState
    latest = (
        EscalationState.select()
        .where(EscalationState.event == event)
        .order_by(EscalationState.assessed_at.desc())
        .first()
    )
    if latest:
        st.subheader("Escalation Ladder")
        st.caption(
            f"The escalation level is a {origin.kind} value; it is not a "
            "measured, observed, or projected risk level."
        )
        fig = go.Figure(go.Indicator(
            mode="gauge+number",
            value=latest.current_level,
            domain={"x": [0, 1], "y": [0, 1]},
            title={"text": f"{latest.region} — claimed escalation level"},
            gauge={
                "axis": {"range": [0, 10]},
                "bar": {"color": "darkred"},
                "steps": [
                    {"range": [1, 4], "color": "lightgreen"},
                    {"range": [4, 7], "color": "orange"},
                    {"range": [7, 10], "color": "red"},
                ],
            }
        ))
        st.plotly_chart(fig, width="stretch")

    # Scenario tree
    root_scenarios = [s for s in event.scenario_nodes if s.parent_id is None]
    if root_scenarios:
        st.subheader("Scenario Tree")
        st.caption(
            f"Scenario probabilities are {origin.kind} values. The colour marks "
            "the stated probability only."
        )
        _render_scenario_tree(root_scenarios)

    # Impact assessment
    from core.db import ImpactAssessment
    imp = (
        ImpactAssessment.select()
        .where(ImpactAssessment.event == event)
        .order_by(ImpactAssessment.assessed_at.desc())
        .first()
    )
    if imp:
        dims = ["military", "economic", "humanitarian", "diplomatic"]
        fig = go.Figure()
        fig.add_trace(go.Bar(
            name="Regional",
            x=dims,
            y=[getattr(imp, f"{d}_regional") for d in dims],
        ))
        fig.add_trace(go.Bar(
            name="Global",
            x=dims,
            y=[getattr(imp, f"{d}_global") for d in dims],
        ))
        fig.update_layout(barmode="group", yaxis={"range": [0, 10]},
                          title="Claimed impact by dimension")
        st.plotly_chart(fig, width="stretch")
        st.caption(f"Impact numbers are {origin.kind} values, not observed outcomes.")

    # Technical analysis
    tech_analyses = list(event.technical_analyses)
    if tech_analyses:
        st.subheader("Technical Analysis")
        for ta in tech_analyses:
            st.text(f"Type: {ta.category_type}")
            try:
                fields = json.loads(ta.fields_json)
                st.json(fields)
            except Exception:
                st.text(ta.fields_json)

    # Multi-horizon prediction timeline
    from core.db import HORIZON_CODES, HORIZON_LABELS, Prediction as PredModel  # alias avoids shadowing local Prediction uses

    # Custom (legacy) predictions — exclude horizon predictions shown in the timeline below
    predictions = list(
        PredModel.select()
        .where((PredModel.event == event) & (PredModel.horizon.is_null(True)))
    )
    if predictions:
        st.subheader("Custom Predictions")
        st.table(pd.DataFrame([{
            "Claimed Probability": f"{p.probability}%",
            "Claimed Confidence": p.confidence,
            "Timeframe": p.timeframe or "N/A",
            "Reasoning": (p.reasoning or "")[:80],
            "Outcome": p.outcome or "Pending",
            "Brier Score": p.brier_score,
        } for p in predictions]))
    horizon_preds = list(
        PredModel.select()
        .where((PredModel.event == event) & (PredModel.horizon.is_null(False)))
        .order_by(PredModel.created_at.desc())
    )
    # Keep most recent prediction per horizon
    seen = {}
    for p in horizon_preds:
        if p.horizon not in seen:
            seen[p.horizon] = p

    if seen:
        st.subheader("Prediction Timeline")
        CONF_COLOR = {"high": "#2ecc71", "medium": "#f39c12", "low": "#e74c3c"}
        rows = []
        for code in HORIZON_CODES:
            if code in seen:
                p = seen[code]
                rows.append({
                    "Horizon": HORIZON_LABELS[code],
                    "Claimed Probability": p.probability,
                    "Claimed Confidence": p.confidence,
                    "Color": CONF_COLOR.get(p.confidence, "#95a5a6"),
                    "Outcome": (p.reasoning or "")[:120],
                })
        df_h = pd.DataFrame(rows)
        fig_h = go.Figure()
        for conf_level, color in CONF_COLOR.items():
            subset = df_h[df_h["Claimed Confidence"] == conf_level]
            if subset.empty:
                continue
            fig_h.add_trace(go.Bar(
                name=conf_level.capitalize(),
                orientation="h",
                x=subset["Claimed Probability"],
                y=subset["Horizon"],
                marker_color=color,
                customdata=subset[["Outcome"]].values,
                hovertemplate="<b>%{y}</b><br>Claimed probability: %{x}%<br>%{customdata[0]}<extra></extra>",
            ))
        ordered_labels = [HORIZON_LABELS[c] for c in HORIZON_CODES if c in seen]
        fig_h.update_layout(
            barmode="group",
            xaxis={"range": [0, 100], "title": "Claimed probability (%)"},
            yaxis={"categoryorder": "array", "categoryarray": ordered_labels, "autorange": "reversed"},
            title="Multi-horizon claimed prediction timeline",
            legend_title="Claimed confidence",
            height=max(300, len(rows) * 50),
        )
        st.plotly_chart(fig_h, width="stretch")
        st.dataframe(
            df_h[["Horizon", "Claimed Probability", "Claimed Confidence", "Outcome"]],
            width="stretch",
        )


def _render_scenario_tree(nodes, depth=0):
    for node in nodes:
        color = "🟢" if node.probability >= 50 else "🟡" if node.probability >= 25 else "🔴"
        indent = "    " * depth
        st.text(f"{indent}{color} {node.probability}% — {node.scenario_text}")
        children = list(node.children)
        if children:
            _render_scenario_tree(children, depth + 1)


def page_predictions():
    from core.db import Prediction, Event
    st.title("Prediction Tracker")
    st.caption(WRITER_ENTERED_SCOPE)
    preds = list(
        Prediction.select(Prediction, Event)
        .join(Event)
        .order_by(Prediction.created_at.desc())
    )
    if not preds:
        st.info("No predictions yet. Use `python ww3.py predict <event_id>` to add one.")
        return
    # A forecast an operator typed and one a model wrote read identically once
    # they are rows in a table, so each states which it was.  "unrecorded" is
    # its own answer: it predates authorship capture and is not a claim that a
    # person wrote it.
    from analysis.predictions import prediction_authorship

    df = pd.DataFrame([{
        "ID": p.id,
        "Event": p.event.title,
        "Evidence Status": _event_lineage(p.event)[0],
        "Written By": prediction_authorship(p),
        "Claimed Probability": p.probability,
        "Claimed Confidence": p.confidence,
        "Resolved": p.resolved_at is not None,
        "Brier Score": p.brier_score,
    } for p in preds])
    if (df["Evidence Status"] != "SOURCED").any():
        st.warning("Some forecasts are attached to events without sourced provenance.")
    st.dataframe(df, width="stretch")
    resolved = df[df["Resolved"]]
    if not resolved.empty and resolved["Brier Score"].notna().any():
        avg_brier = resolved["Brier Score"].mean()
        st.metric("Mean Brier Score (resolved)", f"{avg_brier:.4f}",
                  help="Lower is better. 0=perfect, 1=worst")


def page_runs():
    from core.db import AnalysisRun

    st.title("Pipeline Runs")
    runs = list(AnalysisRun.select().order_by(AnalysisRun.created_at.desc()))
    if not runs:
        st.info("No observation analysis runs yet.")
        return
    rows = []
    for run in runs:
        observation = run.observation
        rows.append({
            "Run": run.id,
            "Status": run.status,
            "Attempt": run.attempt,
            "Event": run.event_id,
            "Observation": observation.id,
            "Source": observation.source,
            "Source Record": observation.source_record_id,
            "Synthetic": observation.synthetic,
            "Failure Stage": run.error_stage,
            "Failure Code": run.error,
            "Model": run.model_name,
            "Code Version": run.code_version,
        })
    frame = pd.DataFrame(rows)
    if (frame["Status"] == "failed").any():
        st.warning("Failed runs retain their Observation and can be retried safely.")
    st.dataframe(frame, width="stretch")


def main() -> None:
    """Render one dashboard page for the current Streamlit script run."""

    st.set_page_config(
        page_title="WW3 Observation Framework", layout="wide", page_icon="🌍"
    )
    _initialize_database()
    page = st.sidebar.selectbox(
        "Navigation",
        ["Source Observations", "Derived Event Timeline", "Event Drilldown",
         "Pipeline Runs", "Predictions"],
    )

    if page == "Source Observations":
        page_observations()
    elif page == "Derived Event Timeline":
        page_timeline()
    elif page == "Event Drilldown":
        from core.db import Event
        events = list(Event.select().order_by(Event.date.desc()))
        if events:
            options = {f"[{e.id}] {e.title}": e.id for e in events}
            selected = st.sidebar.selectbox("Select Event", list(options.keys()))
            page_event(options[selected])
        else:
            st.info(
                "No derived Events yet. Explicitly analyze an Observation or use "
                "`python ww3.py add-event`."
            )
    elif page == "Predictions":
        page_predictions()
    elif page == "Pipeline Runs":
        page_runs()


# Streamlit executes this file as "__main__"; importing it as dashboard.app must
# neither render a page nor open the ledger.
if __name__ == "__main__":
    main()
