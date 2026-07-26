import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("streamlit")
pytest.importorskip("plotly")

from core.collector import CollectionEntry, CollectionHealth  # noqa: E402
from core.observations import get_or_create_observation  # noqa: E402
from core.projections import project_observation  # noqa: E402
from dashboard import app  # noqa: E402


_SOURCE = Path(app.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)

# Words that would claim the framework judged a source or an outcome.  Every
# rendered string carrying one must also say who claimed it, or deny it.
LOADED_TERMS = (
    "credibility", "confidence", "reliab", "corroborat", "risk", "verified",
    "power", "truth",
)
QUALIFIERS = ("claimed", "model-derived", "hand-entered", "legacy", "not ", "never ")


def _docstring_ids(tree) -> set[int]:
    """Identify docstrings, which describe the module rather than render."""
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, holders) or not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))
    return found


def _rendered_strings() -> list[str]:
    skip = _docstring_ids(_TREE)
    return [
        node.value
        for node in ast.walk(_TREE)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skip
    ]


def _caption_arguments() -> set[str]:
    names = set()
    for node in ast.walk(_TREE):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "caption":
            continue
        for argument in node.args:
            names.update(
                child.id for child in ast.walk(argument) if isinstance(child, ast.Name)
            )
    return names


def _leaf(*, source_record_id="record-1", **overrides):
    leaf = {
        "source": "example-feed",
        "source_record_id": source_record_id,
        "type": "news",
        "raw_text": json.dumps({"title": f"Source title {source_record_id}"}),
        "canonical_url": f"https://example.test/{source_record_id}",
        "published_at": "2026-07-17T09:15:00Z",
        "fetched_at": "2026-07-17T09:20:00Z",
    }
    leaf.update(overrides)
    return leaf


def _observation(**overrides):
    return get_or_create_observation(_leaf(**overrides))[0]


def _entry(key, *, kind="source", state="fresh", age=60.0, expected=1_200.0, last=940.0):
    return CollectionEntry(
        key=key,
        kind=kind,
        state=state,
        last_capture_success_at=last,
        age_seconds=age,
        expected_max_age_seconds=expected,
    )


def _health(*entries, file_state="present"):
    return CollectionHealth(
        observed_at=1_000.0,
        status_file_state=file_state,
        entries=tuple(entries),
    )


def test_bounded_view_notice_names_the_records_it_cannot_show():
    notice = app.bounded_view_notice(500, 2_443)

    assert "500" in notice
    assert "2,443" in notice
    assert "bounded" in notice
    assert "oldest row below is not the oldest record held" in notice


def test_bounded_view_notice_says_when_nothing_is_hidden():
    assert app.bounded_view_notice(12, 12) == "Showing all 12 captured observations."


def test_observation_page_total_counts_records_beyond_the_bound():
    for index in range(3):
        _observation(source_record_id=f"record-{index}")

    observations, total = app.observation_page(limit=2)

    assert len(observations) == 2
    assert total == 3
    assert "3 captured observations" in app.bounded_view_notice(len(observations), total)


def test_observation_row_never_shows_source_confidence_bare():
    observation = _observation()

    row = app.observation_row(
        observation, project_observation(observation), "not-requested"
    )

    assert row[app.LEGACY_CAPTURE_HINT_COLUMN] == "not-assessed"
    assert not [column for column in row if "confidence" in column.lower()]
    assert "legacy" in app.LEGACY_CAPTURE_HINT_COLUMN.lower()
    assert "not source credibility" in app.LEGACY_CAPTURE_HINT_SCOPE


def test_no_rendered_string_claims_credibility_or_risk():
    unqualified = [
        text
        for text in _rendered_strings()
        if any(term in text.lower() for term in LOADED_TERMS)
        and not any(qualifier in text.lower() for qualifier in QUALIFIERS)
    ]

    assert unqualified == []


def test_dashboard_never_reads_the_unwritten_confidence_score():
    attributes = {
        node.attr for node in ast.walk(_TREE) if isinstance(node, ast.Attribute)
    }

    assert "confidence_score" not in attributes
    assert "confidence_score" not in _SOURCE


def test_scope_captions_are_rendered_beside_the_values_they_bound():
    captions = _caption_arguments()

    assert "LEGACY_CAPTURE_HINT_SCOPE" in captions
    assert "COLLECTION_HEALTH_SCOPE" in captions
    assert "WRITER_ENTERED_SCOPE" in captions


def test_importing_the_dashboard_neither_renders_nor_opens_the_ledger():
    module_calls = [
        node for node in _TREE.body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
    ]
    guards = [
        node for node in _TREE.body
        if isinstance(node, ast.If) and "__main__" in ast.unparse(node.test)
    ]

    assert module_calls == []
    assert len(guards) == 1
    assert [ast.unparse(statement) for statement in guards[0].body] == ["main()"]


def test_event_metrics_name_the_analysis_run_that_wrote_them():
    runs = [
        SimpleNamespace(id=7, model_name="claude-local"),
        SimpleNamespace(id=9, model_name="claude-local"),
    ]

    origin = app.event_field_origin(runs)

    assert origin.kind == "model-derived"
    assert "AnalysisRun 7, 9" in origin.detail
    assert "claude-local" in origin.detail
    assert origin.metric_label("Significance") == "Significance (model-derived)"


def test_event_metrics_name_an_operator_when_no_run_wrote_them():
    origin = app.event_field_origin([])

    assert origin.kind == "hand-entered"
    assert "No AnalysisRun wrote these fields" in origin.detail
    assert origin.metric_label("Classification") == "Classification (hand-entered)"


def test_event_timeline_row_states_what_wrote_each_score():
    event = SimpleNamespace(
        id=3, title="Recorded event", date="2026-07-20", category="political",
        significance=8, classification="reported",
    )

    row = app.event_timeline_row(event, "SOURCED", app.event_field_origin([]))

    assert row["Field Origin"] == "hand-entered"
    assert row["Claimed Significance"] == 8
    assert row["Claimed Classification"] == "reported"
    assert "Significance" not in row


def test_narrative_heading_keeps_its_credibility_number_claimed():
    narrative = SimpleNamespace(narrative_type="official_state", credibility_score=9)

    heading = app.narrative_heading(narrative, app.event_field_origin([]))

    assert "claimed credibility 9/10" in heading
    assert "hand-entered" in heading


def test_collection_health_rows_report_a_stale_source_as_stale():
    health = _health(
        _entry("usgs", kind="group", state="fresh", age=120.0, expected=900.0),
        _entry("reuters-world", state="stale", age=7_260.0),
    )

    rows = app.collection_health_rows(health)
    stale = [row for row in rows if row["Collection Key"] == "reuters-world"][0]

    assert stale["Cycle State"] == "stale"
    assert stale["Last Capture Success"] == "2h ago"
    assert stale["Expected Within"] == "20m"
    assert "no capture success was recorded" in stale["Meaning"]
    assert health.stale_keys() == ("reuters-world",)
    assert app.unattested_collection_keys(health) == ()


def test_collection_health_rows_report_a_key_that_never_captured():
    health = _health(
        _entry("usgs", kind="group", age=30.0, expected=900.0),
        _entry("xinhua", state="never-observed", age=None, last=None),
        _entry("gdacs", kind="group", state="unknown", age=None, last=None),
    )

    rows = app.collection_health_rows(health)
    never = [row for row in rows if row["Collection Key"] == "xinhua"][0]

    assert never["Last Capture Success"] == "unavailable"
    assert never["Cycle State"] == "never-observed"
    assert app.unattested_collection_keys(health) == ("xinhua", "gdacs")


def test_collection_health_rows_describe_collection_not_publishers():
    rows = app.collection_health_rows(_health(_entry("usgs", kind="group")))

    assert not [column for column in rows[0] if "confidence" in column.lower()]
    assert "publisher" in app.COLLECTION_HEALTH_SCOPE
    assert "not the health, reliability, or credibility" in app.COLLECTION_HEALTH_SCOPE


def test_event_lineage_ignores_a_forecast_run(tmp_path):
    # A forecast run hangs off the same Event but never reads an observation,
    # so crediting it would report one captured source record as two and name
    # a model that never wrote the Event's own fields.
    from core.db import AnalysisRun, Event, HORIZON_FORECAST_CONTRACT

    observation = get_or_create_observation({
        "source": "usgs",
        "source_record_id": "lineage-1",
        "type": "seismic",
        "raw_text": "M 5.0 recorded offshore.",
        "canonical_url": "https://earthquake.usgs.gov/lineage-1",
    })[0]
    event = Event.create(
        title="Recorded event",
        date="2026-07-20",
        category="natural",
        classification="reported",
        significance=4,
    )
    derivation = AnalysisRun.create(
        observation=observation,
        event=event,
        status="completed",
        model_name="derive-model",
    )
    AnalysisRun.create(
        observation=observation,
        event=event,
        status="completed",
        model_name="forecast-model",
        contract=HORIZON_FORECAST_CONTRACT,
    )

    lineage, runs = app._event_lineage(event)

    assert lineage == "SOURCED"
    assert [run.id for run in runs] == [derivation.id]
    assert "forecast-model" not in app.event_field_origin(runs).detail
