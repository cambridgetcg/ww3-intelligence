import builtins
import io
import json
import os
import queue
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel

from cli.feed import (
    MAX_CARDS,
    FeedDisplay,
    _process_collected_leaves,
    ingest_thread_gdacs,
    ingest_thread_news,
    ingest_thread_usgs,
    observation_watcher_thread,
    render_observation_card,
)
from core.db import AnalysisRun, Event, Observation
from core.observations import get_or_create_observation


def _leaf(
    *,
    source="example-feed",
    source_record_id="record-1",
    raw_text=None,
    synthetic=False,
    **overrides,
):
    if raw_text is None:
        raw_text = json.dumps({
            "title": f"Source title {source_record_id}",
            "summary": "Source-provided summary.",
        })
    leaf = {
        "source": source,
        "source_record_id": source_record_id,
        "type": "news",
        "raw_text": raw_text,
        "canonical_url": f"https://example.test/{source_record_id}",
        "published_at": "2026-07-17T09:15:00Z",
        "fetched_at": "2026-07-17T09:20:00Z",
        "synthetic": synthetic,
    }
    leaf.update(overrides)
    return leaf


def _observation(**overrides):
    return get_or_create_observation(_leaf(**overrides))[0]


def _render(value) -> str:
    output = io.StringIO()
    console = Console(
        file=output,
        width=120,
        height=50,
        color_system=None,
        force_terminal=False,
    )
    console.print(value)
    return output.getvalue()


class _OneCycleStop:
    def __init__(self):
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, _interval):
        self.stopped = True
        return True

    def set(self):
        self.stopped = True


def test_render_observation_card_is_evidence_first():
    raw = json.dumps({
        "title": "M 4.3 - Test Region",
        "mag": 4.3,
        "depth_km": 12.5,
        "event_type": "earthquake",
        "place": "Test Region",
    })
    observation = _observation(
        source="usgs",
        source_record_id="usgs-1",
        type="seismic",
        raw_text=raw,
        location_name="Test Region",
    )

    panel = render_observation_card(observation)
    rendered = _render(panel.renderable)

    assert isinstance(panel, Panel)
    assert "[USGS/SEISMIC] M 4.3 - Test Region" in str(panel.title)
    assert panel.border_style == "cyan"
    assert "Observation" in rendered
    assert str(observation.id) in rendered
    assert "Evidence" in rendered
    assert "SOURCE RECORD" in rendered
    assert "Analysis snapshot" in rendered
    assert "not-requested" in rendered
    assert "Source fields" in rendered
    assert "mag=4.3" in rendered
    assert rendered.index("Evidence") < rendered.index("Source fields")
    assert rendered.index("Analysis snapshot") < rendered.index("Source fields")
    assert "not an independent truth finding" in rendered


def test_render_observation_card_shows_latest_analysis_status():
    observation = _observation(source_record_id="analysed-1")
    AnalysisRun.create(observation=observation, status="failed")

    rendered = _render(render_observation_card(observation).renderable)

    assert "Analysis snapshot" in rendered
    assert "failed" in rendered


def test_render_observation_card_labels_synthetic_revision():
    first = _observation(
        source="fixture-feed",
        source_record_id="stable-id",
        raw_text=json.dumps({"title": "First version", "summary": "First"}),
    )
    revision = _observation(
        source="fixture-feed",
        source_record_id="stable-id",
        raw_text=json.dumps({"title": "Corrected fixture", "summary": "Correction"}),
        synthetic=True,
    )

    panel = render_observation_card(revision)
    rendered = _render(panel.renderable)

    assert revision.supersedes_id == first.id
    assert panel.border_style == "magenta"
    assert "SYNTHETIC FIXTURE" in rendered
    assert f"revision of {first.id}" in rendered


def test_render_observation_card_labels_non_synthetic_revision():
    first = _observation(
        source="revision-feed",
        source_record_id="stable-id",
        raw_text=json.dumps({"title": "First version", "summary": "First"}),
    )
    revision = _observation(
        source="revision-feed",
        source_record_id="stable-id",
        raw_text=json.dumps({"title": "Correction", "summary": "Corrected"}),
    )

    panel = render_observation_card(revision)
    rendered = _render(panel.renderable)

    assert panel.border_style == "yellow"
    assert "SOURCE RECORD" in rendered
    assert f"revision of {first.id}" in rendered


def test_render_observation_card_sanitizes_legacy_metadata_controls():
    observation = Observation.create(
        dedupe_key="legacy-control",
        source="feed\u202eexe",
        source_record_id="legacy-control",
        type="news",
        raw_text=json.dumps({"title": "Legacy title"}),
        content_hash="hash",
        fetched_at=datetime(2026, 7, 17, 12, 0),
    )

    panel = render_observation_card(observation)

    assert "\u202e" not in str(panel.title)
    assert "FEED�EXE" in str(panel.title)


def test_feed_display_tracks_and_prepends_observations():
    display = FeedDisplay(watch=False)
    first = _observation(source_record_id="first")
    second = _observation(source_record_id="second")

    assert display.observation_count == 0
    assert display.cards == []

    display.add_observation(first)
    display.add_observation(second)

    assert display.observation_count == 2
    assert len(display.cards) == 2
    assert "Source title second" in str(display.cards[0].title)
    assert "Source title first" in str(display.cards[1].title)


def test_feed_display_caps_recent_observation_cards():
    display = FeedDisplay(watch=False)
    for index in range(MAX_CARDS + 3):
        display.add_observation(_observation(source_record_id=f"record-{index}"))

    assert display.observation_count == MAX_CARDS + 3
    assert len(display.cards) == MAX_CARDS
    assert f"Source title record-{MAX_CARDS + 2}" in str(display.cards[0].title)
    assert all("Source title record-0" not in str(card.title) for card in display.cards)


def test_feed_display_layout_names_passive_and_collection_modes():
    passive = FeedDisplay(watch=False)
    collecting = FeedDisplay(watch=True)

    passive_layout = passive.render()
    collecting_layout = collecting.render()

    assert isinstance(passive_layout, Layout)
    assert passive_layout["header"].name == "header"
    assert passive_layout["feed"].name == "feed"
    assert passive_layout["status"].name == "status"
    assert "DB-POLL" in _render(passive_layout)
    collecting_text = _render(collecting_layout)
    assert "LIVE+COLLECT" in collecting_text
    assert "ANALYZE" not in collecting_text


def test_process_collected_leaves_without_analysis_stays_collection_only():
    stream_path = Path(os.environ["WW3_STREAM_PATH"])
    imported = []
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        imported.append(name)
        if name in {"core.pipeline", "analysis.llm_processor", "analysis.claude_cli"}:
            raise AssertionError(f"collection attempted model import: {name}")
        return real_import(name, *args, **kwargs)

    assert not stream_path.exists()
    with patch("core.stream.StreamWriter.write") as stream_write, \
         patch("subprocess.run") as subprocess_run, \
         patch.object(builtins, "__import__", side_effect=guarded_import):
        _process_collected_leaves(
            [_leaf(source_record_id="collect-only")],
        )

    assert Observation.select().count() == 1
    assert AnalysisRun.select().count() == 0
    assert Event.select().count() == 0
    assert not stream_path.exists()
    assert not ({"core.pipeline", "analysis.llm_processor", "analysis.claude_cli"} & set(imported))
    stream_write.assert_not_called()
    subprocess_run.assert_not_called()


def test_process_collected_leaves_deduplicates_and_links_revisions_without_analysis():
    first = _leaf(
        source="batch-feed",
        source_record_id="stable-id",
        raw_text="version A",
    )
    duplicate = dict(first)
    revision = {**first, "raw_text": "version B"}
    invalid = {"type": "news", "raw_text": "invalid without source"}

    _process_collected_leaves([first, duplicate, revision, invalid])

    observations = list(Observation.select().order_by(Observation.id.asc()))
    assert len(observations) == 2
    assert observations[1].supersedes_id == observations[0].id
    assert AnalysisRun.select().count() == 0
    assert Event.select().count() == 0
    assert not Path(os.environ["WW3_STREAM_PATH"]).exists()


def test_observation_watcher_queues_new_rows_in_ledger_order():
    first = _observation(source_record_id="watch-1")
    second = _observation(source_record_id="watch-2")
    output = queue.Queue()

    observation_watcher_thread(output, _OneCycleStop(), last_id=0)

    assert output.get_nowait().id == first.id
    assert output.get_nowait().id == second.id
    assert output.get_nowait() is None
    assert output.empty()


def test_observation_watcher_surfaces_sanitized_poll_failure(capsys):
    with patch.object(Observation, "select", side_effect=RuntimeError("private detail")):
        observation_watcher_thread(queue.Queue(), _OneCycleStop(), last_id=0)

    error = capsys.readouterr().err
    assert "poll failed (RuntimeError)" in error
    assert "private detail" not in error


def test_usgs_watcher_defaults_to_collection_only_and_propagates_flag():
    leaves = [_leaf(source="usgs", source_record_id="watch-usgs", type="seismic")]

    with patch("ingestion.seismic_feed.fetch_seismic_events", return_value=leaves), \
         patch("cli.feed._process_collected_leaves") as process:
        ingest_thread_usgs(_OneCycleStop(), interval=0)

    process.assert_called_once_with(leaves)


def test_news_watcher_caps_total_collection_across_registered_feeds():
    leaves = [_leaf(source="un_press", source_record_id="watch-news")]

    with patch(
        "ingestion.news_feed.iter_feed_batches",
        return_value=iter([("un_press", leaves)]),
    ) as batches, patch("cli.feed._process_collected_leaves") as process:
        ingest_thread_news(_OneCycleStop(), interval=0)

    batches.assert_called_once_with(total_limit=200)
    process.assert_called_once_with(leaves)


def test_gdacs_watcher_is_bounded_and_collection_only():
    leaves = [_leaf(source="gdacs", source_record_id="watch-gdacs", type="hazard")]

    with patch("ingestion.gdacs_feed.fetch_gdacs_events", return_value=leaves) as fetch, \
         patch("cli.feed._process_collected_leaves") as process:
        ingest_thread_gdacs(_OneCycleStop(), interval=0)

    fetch.assert_called_once_with(limit=50)
    process.assert_called_once_with(leaves)


def test_cli_feed_watch_delegates_collection_only_without_cli_db_init():
    from click.testing import CliRunner
    from cli.commands import cli

    with patch("cli.commands.init_db") as init, \
         patch("cli.feed.run_feed") as run_feed:
        result = CliRunner().invoke(cli, ["feed", "--watch"])

    assert result.exit_code == 0, result.output
    init.assert_not_called()
    run_feed.assert_called_once_with(watch=True)
