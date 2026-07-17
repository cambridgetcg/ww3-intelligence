"""Headless, collection-only source watcher.

This module deliberately has no terminal UI and never imports the analysis
pipeline.  It is suitable for a long-running service colocated with the
SQLite ledger.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from core.db import database, init_db


USGS_INTERVAL = 300
NEWS_INTERVAL = 600
GDACS_INTERVAL = 900
WATCH_NEWS_LIMIT = 200
WATCH_GDACS_LIMIT = 50
_COLLECTION_WRITE_LOCK = threading.Lock()
_STATUS_WRITE_LOCK = threading.Lock()


def process_collected_leaves(leaves: list):
    """Capture one source batch without entering the model layer."""
    from core.collection import collect_batch

    # Fetches remain parallel, but SQLite writes are ordered through one lock.
    # This avoids losing the remainder of a batch to concurrent writer locks.
    with _COLLECTION_WRITE_LOCK:
        return collect_batch(leaves)


def collector_status_path() -> Path | None:
    """Return the operational heartbeat path beside the configured ledger."""
    if database.database == ":memory:":
        return None
    return Path(database.database).parent / ".collector-status.json"


def _record_success(group: str) -> None:
    """Atomically persist a non-evidentiary collector success heartbeat."""
    path = collector_status_path()
    if path is None:
        return
    with _STATUS_WRITE_LOCK:
        payload = {"version": 1, "groups": {}}
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and isinstance(existing.get("groups"), dict):
                payload = existing
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            pass
        payload["version"] = 1
        payload["groups"][group] = time.time()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(f"{path.name}.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        temporary.replace(path)
        os.chmod(path, 0o600)


def _log_batch(source: str, batch) -> None:
    print(
        f"[collector:{source}] received={batch.received} "
        f"new={batch.new} known={batch.known} "
        f"revision={batch.revision} invalid={batch.invalid}",
        flush=True,
    )


def _capture_batch(source: str, leaves: list, processor, *, group: str) -> None:
    batch = processor(leaves)
    _record_success(group)
    _log_batch(source, batch)


def ingest_thread_usgs(
    stop: threading.Event,
    interval: int = USGS_INTERVAL,
    *,
    processor=process_collected_leaves,
) -> None:
    """Collect one bounded USGS batch per interval."""
    from ingestion.seismic_feed import fetch_seismic_events

    database.connect(reuse_if_open=True)
    try:
        while not stop.is_set():
            try:
                _capture_batch(
                    "usgs",
                    fetch_seismic_events(),
                    processor,
                    group="usgs",
                )
            except Exception as exc:
                print(
                    f"[collector:usgs] cycle failed ({type(exc).__name__}).",
                    file=sys.stderr,
                    flush=True,
                )
            stop.wait(interval)
    finally:
        database.close()


def ingest_thread_news(
    stop: threading.Event,
    interval: int = NEWS_INTERVAL,
    *,
    processor=process_collected_leaves,
) -> None:
    """Collect bounded registered RSS batches sequentially per interval."""
    from ingestion.news_feed import iter_feed_batches

    database.connect(reuse_if_open=True)
    try:
        while not stop.is_set():
            try:
                cycle_succeeded = False
                for source, leaves in iter_feed_batches(total_limit=WATCH_NEWS_LIMIT):
                    if stop.is_set():
                        break
                    _capture_batch(source, leaves, processor, group=source)
                    cycle_succeeded = True
                if cycle_succeeded:
                    _record_success("news")
            except Exception as exc:
                print(
                    f"[collector:news] cycle failed ({type(exc).__name__}).",
                    file=sys.stderr,
                    flush=True,
                )
            stop.wait(interval)
    finally:
        database.close()


def ingest_thread_gdacs(
    stop: threading.Event,
    interval: int = GDACS_INTERVAL,
    *,
    processor=process_collected_leaves,
) -> None:
    """Collect one bounded GDACS batch per interval."""
    from ingestion.gdacs_feed import fetch_gdacs_events

    database.connect(reuse_if_open=True)
    try:
        while not stop.is_set():
            try:
                _capture_batch(
                    "gdacs",
                    fetch_gdacs_events(limit=WATCH_GDACS_LIMIT),
                    processor,
                    group="gdacs",
                )
            except Exception as exc:
                print(
                    f"[collector:gdacs] cycle failed ({type(exc).__name__}).",
                    file=sys.stderr,
                    flush=True,
                )
            stop.wait(interval)
    finally:
        database.close()


def start_collector_threads(stop: threading.Event) -> list[threading.Thread]:
    """Start the three bounded collection loops and return their handles."""
    threads = [
        threading.Thread(
            target=ingest_thread_usgs,
            args=(stop,),
            daemon=True,
            name="collect-usgs",
        ),
        threading.Thread(
            target=ingest_thread_news,
            args=(stop,),
            daemon=True,
            name="collect-news",
        ),
        threading.Thread(
            target=ingest_thread_gdacs,
            args=(stop,),
            daemon=True,
            name="collect-gdacs",
        ),
    ]
    for thread in threads:
        thread.start()
    return threads


def run_collector() -> int:
    """Initialize SQLite, then run collection loops until stopped."""
    init_db()
    database.close()
    stop = threading.Event()

    def request_stop(_signum, _frame) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    threads = start_collector_threads(stop)
    try:
        while not stop.wait(1):
            dead = [thread.name for thread in threads if not thread.is_alive()]
            if dead:
                print(
                    f"[collector] worker stopped unexpectedly: {', '.join(dead)}",
                    file=sys.stderr,
                    flush=True,
                )
                return 1
        return 0
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=35)


if __name__ == "__main__":
    raise SystemExit(run_collector())
