"""Headless, collection-only source watcher.

This module deliberately has no terminal UI and never imports the analysis
pipeline.  It is suitable for a long-running service colocated with the
SQLite ledger.  It also owns the pure reader that turns its own heartbeat
file into a per-group and per-source view of last observed capture success,
so the supervisor and the dashboard share one definition of staleness.
"""

from __future__ import annotations

import json
import math
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.db import database, init_db
from core.registry import rss_source_profiles


USGS_INTERVAL = 300
NEWS_INTERVAL = 600
GDACS_INTERVAL = 900
WATCH_NEWS_LIMIT = 200
WATCH_GDACS_LIMIT = 50
COLLECTION_HEALTH_VERSION = "collection-health-v1"
GROUP_INTERVALS = {
    "usgs": USGS_INTERVAL,
    "news": NEWS_INTERVAL,
    "gdacs": GDACS_INTERVAL,
}
# One cycle can lose a full bounded fetch timeout on every registered feed, and
# a redeploy adds another interval on top of that.  Every expectation below is
# an interval multiplied by this tolerance, never a second copy of a number.
STALENESS_TOLERANCE = 4
_COLLECTION_WRITE_LOCK = threading.Lock()
_STATUS_WRITE_LOCK = threading.Lock()
_STALE_LOG_LOCK = threading.Lock()
_LOGGED_STALE_KEYS: set[str] = set()


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


@dataclass(frozen=True, slots=True)
class CollectorStatus:
    """The heartbeat file as read, with a defined state when it is unusable."""

    file_state: str
    last_success: dict[str, float]
    unreadable_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CollectionEntry:
    """Last observed capture success for one collector group or source key."""

    key: str
    kind: str
    state: str
    last_capture_success_at: float | None
    age_seconds: float | None
    expected_max_age_seconds: float

    def as_dict(self) -> dict[str, Any]:
        """Return a fresh dictionary with a stable, strict-JSON-safe shape."""

        return {
            "key": self.key,
            "kind": self.kind,
            "state": self.state,
            "last_capture_success_at": self.last_capture_success_at,
            "last_capture_success_utc": _utc_isoformat(self.last_capture_success_at),
            "age_seconds": self.age_seconds,
            "expected_max_age_seconds": self.expected_max_age_seconds,
        }


@dataclass(frozen=True, slots=True)
class CollectionHealth:
    """A versioned, JSON-safe view of when each collector last succeeded."""

    observed_at: float
    status_file_state: str
    entries: tuple[CollectionEntry, ...]
    health_version: str = COLLECTION_HEALTH_VERSION

    def stale_keys(self) -> tuple[str, ...]:
        """Name the collector keys whose last recorded success is overdue."""

        return tuple(entry.key for entry in self.entries if entry.state == "stale")

    def as_dict(self) -> dict[str, Any]:
        """Return a fresh dictionary with a stable, strict-JSON-safe shape."""

        return {
            "health_version": self.health_version,
            "observed_at": self.observed_at,
            "status_file_state": self.status_file_state,
            "entries": [entry.as_dict() for entry in self.entries],
            "stale_keys": list(self.stale_keys()),
        }


def _utc_isoformat(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    try:
        moment = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None
    return moment.isoformat().replace("+00:00", "Z")


def _registered_rss_sources() -> tuple[str, ...]:
    """Name the RSS sources whose per-source heartbeat the news loop writes."""
    try:
        return tuple(profile.source_id for profile in rss_source_profiles())
    except (OSError, RuntimeError, UnicodeError, ValueError):
        # A registry a reader cannot load must not turn a heartbeat read into an
        # exception; the keys already in the file still describe themselves.
        return ()


def read_collector_status(path: Path | None = None) -> CollectorStatus:
    """Read the heartbeat file, degrading to a defined state when unusable."""
    if path is None:
        path = collector_status_path()
    if path is None:
        return CollectorStatus(file_state="absent", last_success={})
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return CollectorStatus(file_state="absent", last_success={})
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        return CollectorStatus(file_state="unreadable", last_success={})
    groups = payload.get("groups") if isinstance(payload, dict) else None
    if not isinstance(groups, dict):
        return CollectorStatus(file_state="unreadable", last_success={})

    last_success: dict[str, float] = {}
    unreadable: list[str] = []
    for key, value in groups.items():
        if not isinstance(key, str):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            unreadable.append(key)
        elif not math.isfinite(value):
            unreadable.append(key)
        else:
            last_success[key] = float(value)
    return CollectorStatus(
        file_state="present",
        last_success=last_success,
        unreadable_keys=tuple(sorted(unreadable)),
    )


def collection_health(now: float | None = None) -> CollectionHealth:
    """Summarize when each collector group and source key last captured.

    The summary reports last observed capture success only.  A ``stale`` entry
    means this collector group has not completed a cycle recently; it claims
    nothing about the publisher, its availability, or its content.  A
    ``never-observed`` entry means no success was ever written under that key,
    which can simply mean the source was never enabled, and ``unknown`` means
    the heartbeat itself could not be read.
    """
    observed_at = time.time() if now is None else float(now)
    status = read_collector_status()

    expected = {
        group: float(interval * STALENESS_TOLERANCE)
        for group, interval in GROUP_INTERVALS.items()
    }
    kinds = {group: "group" for group in GROUP_INTERVALS}
    # Per-source heartbeats are written only by the news loop, which walks every
    # registered feed once per news interval whatever the publisher is.
    source_max_age = float(NEWS_INTERVAL * STALENESS_TOLERANCE)
    observed_keys = (
        *_registered_rss_sources(),
        *status.last_success,
        *status.unreadable_keys,
    )
    for key in observed_keys:
        expected.setdefault(key, source_max_age)
        kinds.setdefault(key, "source")

    entries = []
    for key in sorted(expected):
        timestamp = status.last_success.get(key)
        if status.file_state == "unreadable" or key in status.unreadable_keys:
            state, age = "unknown", None
        elif timestamp is None:
            state, age = "never-observed", None
        else:
            age = round(observed_at - timestamp, 3)
            # A future-dated heartbeat means the clock moved, not that a cycle
            # was missed, so only a positive overdue age reads as stale.
            state = "stale" if age > expected[key] else "fresh"
        entries.append(
            CollectionEntry(
                key=key,
                kind=kinds[key],
                state=state,
                last_capture_success_at=timestamp,
                age_seconds=age,
                expected_max_age_seconds=expected[key],
            )
        )
    return CollectionHealth(
        observed_at=observed_at,
        status_file_state=status.file_state,
        entries=tuple(entries),
    )


def log_stale_collection_transitions(health: CollectionHealth) -> tuple[str, ...]:
    """Report staleness changes once per transition and return the lines."""
    entries = {entry.key: entry for entry in health.entries}
    stale = {key for key, entry in entries.items() if entry.state == "stale"}
    lines = []
    with _STALE_LOG_LOCK:
        # An unreadable heartbeat is not a recovery, so a remembered key is only
        # cleared once a later cycle records a success under it.
        recovered = {
            key
            for key in _LOGGED_STALE_KEYS
            if key in entries and entries[key].state == "fresh"
        }
        for key in sorted(stale - _LOGGED_STALE_KEYS):
            entry = entries[key]
            lines.append(
                f"[collector:health] no capture success for {entry.kind} {key} "
                f"in {entry.age_seconds:.0f}s "
                f"(expected within {entry.expected_max_age_seconds:.0f}s)."
            )
        for key in sorted(recovered):
            lines.append(
                f"[collector:health] capture success recorded again for {key}."
            )
        _LOGGED_STALE_KEYS.difference_update(recovered)
        _LOGGED_STALE_KEYS.update(stale)
    for line in lines:
        print(line, file=sys.stderr, flush=True)
    return tuple(lines)


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
                # One yielding feed keeps the aggregate ``news`` heartbeat fresh
                # while every other registered publisher fails silently inside
                # iter_feed_batches, so the per-source keys are read here.
                log_stale_collection_transitions(collection_health())
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
