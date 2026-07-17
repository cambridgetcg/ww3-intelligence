"""Live terminal view over immutable source observations.

The watcher collects bounded records only. Model-derived Events are handled by
separate, explicit CLI actions; collection never imports the analysis pipeline.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from datetime import datetime, timezone

from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from cli.rendering import terminal_line, terminal_text
from core.db import AnalysisRun, Observation, database, init_db
from core.projections import project_observation


MAX_CARDS = 20
POLL_INTERVAL = 2.0
USGS_INTERVAL = 300
NEWS_INTERVAL = 600
GDACS_INTERVAL = 900
WATCH_NEWS_LIMIT = 200
WATCH_GDACS_LIMIT = 50


def _latest_analysis_status(observation: Observation) -> str:
    run = (
        AnalysisRun.select()
        .where(AnalysisRun.observation == observation)
        .order_by(AnalysisRun.created_at.desc(), AnalysisRun.id.desc())
        .first()
    )
    return run.status if run is not None else "not-requested"


def render_observation_card(observation: Observation) -> Panel:
    """Build an evidence-first card from a deterministic source projection."""
    projection = project_observation(observation)
    item = projection.as_dict()
    evidence = "SYNTHETIC FIXTURE" if observation.synthetic else "SOURCE RECORD"
    revision = (
        f" · revision of {observation.supersedes_id}"
        if observation.supersedes_id else ""
    )
    border = "magenta" if observation.synthetic else "yellow" if revision else "cyan"
    title_suffix = "…" if projection.title_truncated else ""
    title = Text(
        f"[{terminal_line(observation.source).upper()}/"
        f"{terminal_line(observation.type).upper()}] "
        f"{terminal_line(projection.title)}{title_suffix}"
    )

    table = Table.grid(padding=(0, 1))
    table.add_column(style="bold cyan", width=17)
    table.add_column()
    table.add_row("Observation", str(observation.id))
    table.add_row("Evidence", f"{evidence}{revision}")
    table.add_row("Analysis snapshot", _latest_analysis_status(observation))
    table.add_row("Published", item["observation"]["published_at"] or "unavailable")
    table.add_row("Fetched", item["observation"]["fetched_at"])
    if observation.location_name:
        table.add_row("Location", escape(terminal_line(observation.location_name)))
    if projection.facts:
        facts = ", ".join(f"{key}={value}" for key, value in projection.facts)
        if len(facts) > 240:
            facts = facts[:240] + "…"
        table.add_row("Source fields", escape(terminal_line(facts)))
    if projection.summary:
        summary = projection.summary[:240]
        if len(projection.summary) > 240 or projection.summary_truncated:
            summary += "…"
        table.add_row("Source text", escape(terminal_text(summary)))
    table.add_row("Caveat", "Source-provided record; not an independent truth finding.")
    return Panel(table, title=title, border_style=border)


class FeedDisplay:
    """Hold recent observation cards and build the terminal layout."""

    def __init__(self, watch: bool = False):
        self.watch = watch
        self.cards: list[Panel] = []
        self.observation_count = 0
        self.last_poll_time = time.monotonic()

    def add_observation(self, observation: Observation) -> None:
        self.cards.insert(0, render_observation_card(observation))
        self.cards = self.cards[:MAX_CARDS]
        self.observation_count += 1

    def render(self) -> Layout:
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        if self.watch:
            mode = "LIVE+COLLECT"
            schedule = "USGS 5m · RSS 10m · GDACS 15m cycle delays"
        else:
            mode = "DB-POLL"
            schedule = "External collection"

        header_text = Text()
        header_text.append("WW3 OBSERVATION FEED", style="bold white")
        header_text.append(f"  ·  {mode}", style="bold cyan")
        header_text.append(f"  ·  {now}", style="dim")
        header = Panel(header_text, subtitle=schedule, style="bold")

        content = (
            Group(*self.cards)
            if self.cards
            else Text("Waiting for observations…", style="dim italic", justify="center")
        )
        feed_panel = Panel(content, title="Source Observations", border_style="dim")

        elapsed = time.monotonic() - self.last_poll_time
        status = Text(
            f"Last poll: {elapsed:.1f}s ago  ·  "
            f"Observations seen: {self.observation_count}  ·  Ctrl+C to quit",
            style="dim",
        )
        layout = Layout()
        layout.split_column(
            Layout(header, name="header", size=4),
            Layout(feed_panel, name="feed"),
            Layout(Panel(status, style="dim"), name="status", size=3),
        )
        return layout


def observation_watcher_thread(
    observation_queue: queue.Queue,
    stop: threading.Event,
    last_id: int,
) -> None:
    """Poll for newly captured observations and queue them in ledger order."""
    database.connect(reuse_if_open=True)
    current_id = last_id
    try:
        while not stop.is_set():
            try:
                records = list(
                    Observation.select()
                    .where(Observation.id > current_id)
                    .order_by(Observation.id.asc())
                )
                for observation in records:
                    observation_queue.put(observation)
                    current_id = observation.id
                observation_queue.put(None)
            except Exception as exc:
                print(
                    f"[observation_watcher] poll failed ({type(exc).__name__}).",
                    file=sys.stderr,
                )
            stop.wait(POLL_INTERVAL)
    finally:
        database.close()


def _process_collected_leaves(leaves: list) -> None:
    """Capture one batch without importing or entering model analysis."""
    from core.collection import collect_batch

    collect_batch(leaves)


def ingest_thread_usgs(
    stop: threading.Event,
    interval: int = USGS_INTERVAL,
) -> None:
    """Collect one bounded USGS batch per interval."""
    from ingestion.seismic_feed import fetch_seismic_events

    database.connect(reuse_if_open=True)
    try:
        while not stop.is_set():
            try:
                _process_collected_leaves(fetch_seismic_events())
            except Exception as exc:
                print(f"[collect_thread_usgs] failed ({type(exc).__name__}).", file=sys.stderr)
            stop.wait(interval)
    finally:
        database.close()


def ingest_thread_news(
    stop: threading.Event,
    interval: int = NEWS_INTERVAL,
) -> None:
    """Collect bounded RSS batches sequentially per interval."""
    from ingestion.news_feed import iter_feed_batches

    database.connect(reuse_if_open=True)
    try:
        while not stop.is_set():
            try:
                for _, leaves in iter_feed_batches(total_limit=WATCH_NEWS_LIMIT):
                    if stop.is_set():
                        break
                    _process_collected_leaves(leaves)
            except Exception as exc:
                print(f"[collect_thread_news] failed ({type(exc).__name__}).", file=sys.stderr)
            stop.wait(interval)
    finally:
        database.close()


def ingest_thread_gdacs(
    stop: threading.Event,
    interval: int = GDACS_INTERVAL,
) -> None:
    """Collect one bounded GDACS alert batch per interval."""
    from ingestion.gdacs_feed import fetch_gdacs_events

    database.connect(reuse_if_open=True)
    try:
        while not stop.is_set():
            try:
                _process_collected_leaves(fetch_gdacs_events(limit=WATCH_GDACS_LIMIT))
            except Exception as exc:
                print(
                    f"[collect_thread_gdacs] failed ({type(exc).__name__}).",
                    file=sys.stderr,
                )
            stop.wait(interval)
    finally:
        database.close()


def run_feed(watch: bool = False) -> None:
    """Start the observation watcher and optional source collection loops."""
    init_db()
    try:
        last_id = Observation.select(Observation.id).order_by(Observation.id.desc()).get().id
    except Observation.DoesNotExist:
        last_id = 0

    observation_queue: queue.Queue = queue.Queue()
    stop = threading.Event()
    threading.Thread(
        target=observation_watcher_thread,
        args=(observation_queue, stop, last_id),
        daemon=True,
        name="observation-watcher",
    ).start()

    if watch:
        threading.Thread(
            target=ingest_thread_usgs,
            args=(stop,),
            daemon=True,
            name="collect-usgs",
        ).start()
        threading.Thread(
            target=ingest_thread_news,
            args=(stop,),
            daemon=True,
            name="collect-news",
        ).start()
        threading.Thread(
            target=ingest_thread_gdacs,
            args=(stop,),
            daemon=True,
            name="collect-gdacs",
        ).start()

    display = FeedDisplay(watch=watch)
    try:
        with Live(display.render(), refresh_per_second=2, screen=True) as live:
            while not stop.is_set():
                while True:
                    try:
                        item = observation_queue.get_nowait()
                        if item is None:
                            display.last_poll_time = time.monotonic()
                        else:
                            display.add_observation(item)
                    except queue.Empty:
                        break
                live.update(display.render())
                time.sleep(0.5)
    except KeyboardInterrupt:
        stop.set()
    finally:
        stop.set()
        database.close()
