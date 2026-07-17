import json
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
import pandas as pd

st.set_page_config(page_title="WW3 Observation Framework", layout="wide", page_icon="🌍")

from core.db import database, init_db


@st.cache_resource(show_spinner=False)
def _initialize_database():
    """Run schema/integrity startup once per dashboard process."""
    init_db()
    database.close()
    return True


_initialize_database()


def _event_lineage(event):
    runs = list(event.analysis_runs)
    if not runs:
        return "NO PROVENANCE", runs
    if any(run.observation.synthetic for run in runs):
        return "SYNTHETIC", runs
    return "SOURCED", runs


def page_observations():
    from core.db import AnalysisRun, Observation
    from core.projections import project_observation

    st.title("Source Observations")
    st.caption(
        "Immutable collected records and deterministic source projections. "
        "They are not corroboration, risk scores, or truth findings."
    )
    observations = list(
        Observation.select()
        .order_by(Observation.fetched_at.desc(), Observation.id.desc())
        .limit(500)
    )
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
        projection = project_observation(observation)
        latest_run = latest_runs.get(observation.id)
        rows.append({
            "ID": observation.id,
            "Evidence Status": "SYNTHETIC" if observation.synthetic else "SOURCE-RECORD",
            "Source": observation.source,
            "Type": observation.type,
            "Title": projection.title,
            "Published": projection.published_at,
            "Fetched": projection.fetched_at,
            "Revision Of": observation.supersedes_id,
            "Analysis": latest_run.status if latest_run else "not-requested",
            "Canonical URL": observation.canonical_url,
            "Content SHA-256": observation.content_hash,
        })
    frame = pd.DataFrame(rows)
    if (frame["Evidence Status"] == "SYNTHETIC").any():
        st.warning("Synthetic fixtures are present and are labeled before source fields.")
    st.dataframe(frame, use_container_width=True)


def page_timeline():
    from core.db import Event
    st.title("Model / Manual Event Timeline")
    events = list(Event.select().order_by(Event.date.desc()))
    if not events:
        st.info(
            "No derived Events yet. Collection-only observations remain available "
            "on the Source Observations page."
        )
        return
    rows = []
    for event in events:
        lineage, _ = _event_lineage(event)
        rows.append({
            "ID": event.id, "Title": event.title, "Date": str(event.date),
            "Evidence Status": lineage, "Category": event.category,
            "Significance": event.significance,
            "Classification": event.classification,
        })
    df = pd.DataFrame(rows)
    if (df["Evidence Status"] != "SOURCED").any():
        st.warning(
            "Legacy/manual or synthetic events are present. Evidence status precedes all scores."
        )
    fig = px.scatter(df, x="Date", y="Significance", color="Category",
                     hover_data=["Title", "Evidence Status", "Classification"],
                     size="Significance", symbol="Evidence Status",
                     title="Events by Significance Over Time")
    st.plotly_chart(fig, use_container_width=True)
    st.dataframe(df, use_container_width=True)


def page_event(event_id: int):
    from core.db import Event
    try:
        event = Event.get_by_id(event_id)
    except Exception:
        st.error(f"Event {event_id} not found.")
        return
    st.title("Event Intelligence")
    st.text(f"[{event.category.upper()}] {event.title}")

    # Evidence provenance comes before model-derived analytical branches.
    from core.db import AnalysisRun
    analysis_runs = list(
        AnalysisRun.select()
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
            })
        if any(row["Synthetic"] for row in provenance_rows):
            st.warning("This event includes synthetic or test observations.")
        st.dataframe(pd.DataFrame(provenance_rows), use_container_width=True)
        st.caption(
            "The source record is immutable; event fields and analytical branches are derived views."
        )
    else:
        st.warning("This legacy event has no observation-level provenance record.")

    col1, col2, col3 = st.columns(3)
    col1.metric("Significance", f"{event.significance}/10")
    col2.metric("Classification", event.classification)
    col3.metric("Confidence", f"{event.confidence_score or 'N/A'}")
    if event.summary:
        st.subheader("Summary")
        st.text(event.summary)

    # Actors
    actors = list(event.event_actors)
    if actors:
        st.subheader("Actors")
        st.table(pd.DataFrame([{
            "Name": ea.actor.name,
            "Type": ea.actor.type,
            "Country": ea.actor.country,
            "Role": ea.role,
            "Capability": ea.actor.capability_tier,
        } for ea in actors]))

    # Narratives
    narratives = list(event.narratives)
    if narratives:
        st.subheader("Competing Narratives")
        for n in narratives:
            with st.expander(f"{n.narrative_type} — Credibility {n.credibility_score}/10"):
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
        fig = go.Figure(go.Indicator(
            mode="gauge+number",
            value=latest.current_level,
            domain={"x": [0, 1], "y": [0, 1]},
            title={"text": f"{latest.region} — Escalation Level"},
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
        st.plotly_chart(fig, use_container_width=True)

    # Scenario tree
    root_scenarios = [s for s in event.scenario_nodes if s.parent_id is None]
    if root_scenarios:
        st.subheader("Scenario Tree")
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
                          title="Impact by Dimension")
        st.plotly_chart(fig, use_container_width=True)

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
            "Probability": f"{p.probability}%",
            "Confidence": p.confidence,
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
                    "Probability": p.probability,
                    "Confidence": p.confidence,
                    "Color": CONF_COLOR.get(p.confidence, "#95a5a6"),
                    "Outcome": (p.reasoning or "")[:120],
                })
        df_h = pd.DataFrame(rows)
        fig_h = go.Figure()
        for conf_level, color in CONF_COLOR.items():
            subset = df_h[df_h["Confidence"] == conf_level]
            if subset.empty:
                continue
            fig_h.add_trace(go.Bar(
                name=conf_level.capitalize(),
                orientation="h",
                x=subset["Probability"],
                y=subset["Horizon"],
                marker_color=color,
                customdata=subset[["Outcome"]].values,
                hovertemplate="<b>%{y}</b><br>Probability: %{x}%<br>%{customdata[0]}<extra></extra>",
            ))
        ordered_labels = [HORIZON_LABELS[c] for c in HORIZON_CODES if c in seen]
        fig_h.update_layout(
            barmode="group",
            xaxis={"range": [0, 100], "title": "Probability (%)"},
            yaxis={"categoryorder": "array", "categoryarray": ordered_labels, "autorange": "reversed"},
            title="Multi-Horizon Prediction Timeline",
            legend_title="Confidence",
            height=max(300, len(rows) * 50),
        )
        st.plotly_chart(fig_h, use_container_width=True)
        st.dataframe(
            df_h[["Horizon", "Probability", "Confidence", "Outcome"]],
            use_container_width=True,
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
    preds = list(
        Prediction.select(Prediction, Event)
        .join(Event)
        .order_by(Prediction.created_at.desc())
    )
    if not preds:
        st.info("No predictions yet. Use `python ww3.py predict <event_id>` to add one.")
        return
    df = pd.DataFrame([{
        "ID": p.id,
        "Event": p.event.title,
        "Evidence Status": _event_lineage(p.event)[0],
        "Probability": p.probability,
        "Confidence": p.confidence,
        "Resolved": p.resolved_at is not None,
        "Brier Score": p.brier_score,
    } for p in preds])
    if (df["Evidence Status"] != "SOURCED").any():
        st.warning("Some forecasts are attached to events without sourced provenance.")
    st.dataframe(df, use_container_width=True)
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
    st.dataframe(frame, use_container_width=True)


# Navigation
page = st.sidebar.selectbox(
    "Navigation",
    ["Source Observations", "Derived Event Timeline", "Event Drilldown", "Pipeline Runs", "Predictions"],
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
