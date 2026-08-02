"""Headless, collection-only source watcher.

This module deliberately has no terminal UI and never imports the analysis
pipeline.  It is suitable for a long-running service colocated with the
SQLite ledger.  It also owns the pure reader that turns its own heartbeat
file into a per-group and per-source view of last observed capture success
and of how many records those cycles parsed, so the supervisor and the
dashboard share one definition of staleness.
"""

from __future__ import annotations

import json
import math
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
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
COLLECTION_HEALTH_VERSION = "collection-health-v2"
GROUP_INTERVALS = {
    "usgs": USGS_INTERVAL,
    "news": NEWS_INTERVAL,
    "gdacs": GDACS_INTERVAL,
}
# One cycle can lose a full bounded fetch timeout on every registered feed, and
# a redeploy adds another interval on top of that.  Every expectation below is
# an interval multiplied by this tolerance, never a second copy of a number.
STALENESS_TOLERANCE = 4
# A single bounded fetch that parses nothing is an ordinary quiet interval, so
# only a run of consecutive record-free cycles distinguishes a key that has
# stopped delivering from one whose publisher had nothing to serve this cycle.
NO_RECORD_CYCLE_TOLERANCE = 3
# The heartbeat is a running state, not a time series: the consecutive count
# saturates, so a key's entry cannot grow as cycles accumulate.
MAX_NO_RECORD_CYCLES = 9_999
# The states a cycle transition is worth one log line for.  ``fresh`` is the
# expected condition and the two unattested states describe the heartbeat file
# rather than a collector, so neither is a transition to announce.
REPORTED_CYCLE_STATES = ("stale", "no-records")
_COLLECTION_WRITE_LOCK = threading.Lock()
_STATUS_WRITE_LOCK = threading.Lock()
_CYCLE_LOG_LOCK = threading.Lock()
_LOGGED_CYCLE_STATES: dict[str, str] = {}


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


@dataclass(frozen=True, slots=True)
class RecordDelivery:
    """What one key's completed cycles parsed, apart from whether they ran."""

    last_records_at: float | None = None
    no_record_cycles: int = 0


_NO_DELIVERY = RecordDelivery()


def _read_delivery(value: Any) -> RecordDelivery:
    """Read one stored delivery entry, dropping any field it cannot use."""

    if not isinstance(value, dict):
        return _NO_DELIVERY
    last = value.get("last_records_at")
    if isinstance(last, bool) or not isinstance(last, (int, float)):
        last = None
    elif not math.isfinite(last):
        last = None
    else:
        last = float(last)
    cycles = value.get("no_record_cycles")
    if isinstance(cycles, bool) or not isinstance(cycles, int) or cycles < 0:
        cycles = 0
    return RecordDelivery(
        last_records_at=last,
        no_record_cycles=min(cycles, MAX_NO_RECORD_CYCLES),
    )


def _fold_delivery(previous: Any, received: int, at: float) -> dict[str, Any]:
    """Fold one cycle's parsed-record count into a bounded delivery entry."""

    delivery = _read_delivery(previous)
    if received > 0:
        return {"last_records_at": at, "no_record_cycles": 0}
    return {
        "last_records_at": delivery.last_records_at,
        "no_record_cycles": min(
            delivery.no_record_cycles + 1,
            MAX_NO_RECORD_CYCLES,
        ),
    }


def _record_success(group: str, received: int | None = None) -> None:
    """Atomically persist a non-evidentiary collector cycle heartbeat.

    ``received`` is how many records the bounded fetch parsed, which is what
    separates a key still delivering from one whose cycles return nothing.  A
    cycle that cannot report a count passes ``None``, because an uncounted
    cycle is not a cycle that parsed zero.
    """
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
        # ``version`` names the shape of ``groups``: a key mapped to one bare
        # epoch timestamp.  Every other field is additive and optional, so a
        # reader that predates one still finds the timestamps it came for, and
        # claiming a break here would only invite a future reader to refuse a
        # file it can read.
        payload["version"] = 1
        at = time.time()
        payload["groups"][group] = at
        deliveries = payload.get("records")
        if not isinstance(deliveries, dict):
            deliveries = {}
        if received is not None:
            deliveries[group] = _fold_delivery(deliveries.get(group), received, at)
        # A delivery entry belongs to a key that recorded a cycle, so keeping
        # no other holds both maps to the one bound.
        payload["records"] = {
            key: value for key, value in deliveries.items() if key in payload["groups"]
        }
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
    deliveries: dict[str, RecordDelivery] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CollectionEntry:
    """Last observed capture success for one collector group or source key."""

    key: str
    kind: str
    state: str
    last_capture_success_at: float | None
    age_seconds: float | None
    expected_max_age_seconds: float
    last_records_at: float | None = None
    no_record_cycles: int = 0

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
            "last_records_at": self.last_records_at,
            "last_records_utc": _utc_isoformat(self.last_records_at),
            "no_record_cycles": self.no_record_cycles,
            "no_record_cycle_tolerance": NO_RECORD_CYCLE_TOLERANCE,
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

    def no_record_keys(self) -> tuple[str, ...]:
        """Name the keys whose recent completed cycles parsed no records."""

        return tuple(entry.key for entry in self.entries if entry.state == "no-records")

    def as_dict(self) -> dict[str, Any]:
        """Return a fresh dictionary with a stable, strict-JSON-safe shape."""

        return {
            "health_version": self.health_version,
            "observed_at": self.observed_at,
            "status_file_state": self.status_file_state,
            "entries": [entry.as_dict() for entry in self.entries],
            "stale_keys": list(self.stale_keys()),
            "no_record_keys": list(self.no_record_keys()),
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

    # A heartbeat written before per-key record counts existed carries none, and
    # an entry no reader can use carries none either.  Neither absence may read
    # as cycles that parsed nothing, so both leave the key at zero consecutive
    # record-free cycles.
    deliveries: dict[str, RecordDelivery] = {}
    stored = payload.get("records")
    if isinstance(stored, dict):
        for key, value in stored.items():
            if isinstance(key, str):
                deliveries[key] = _read_delivery(value)
    return CollectorStatus(
        file_state="present",
        last_success=last_success,
        unreadable_keys=tuple(sorted(unreadable)),
        deliveries=deliveries,
    )


def collection_health(now: float | None = None) -> CollectionHealth:
    """Summarize when each collector group and source key last captured.

    The summary reports this collector's own observation of its cycles.  A
    ``stale`` entry means this collector group has not completed a cycle
    recently; it claims nothing about the publisher, its availability, or its
    content.  A ``no-records`` entry means cycles are still completing but the
    last ``NO_RECORD_CYCLE_TOLERANCE`` of them parsed no records at all, which
    is what an HTTP 200 carrying nothing looks like from here; it is equally
    what a publisher with nothing to serve looks like, and it is not a finding
    about either.  A ``fresh`` entry means cycles completed and records were
    parsed recently.  A ``never-observed`` entry means no success was ever
    written under that key, which can simply mean the source was never enabled,
    and ``unknown`` means the heartbeat itself could not be read.
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
        *status.deliveries,
    )
    for key in observed_keys:
        expected.setdefault(key, source_max_age)
        kinds.setdefault(key, "source")

    entries = []
    for key in sorted(expected):
        timestamp = status.last_success.get(key)
        delivery = status.deliveries.get(key, _NO_DELIVERY)
        if status.file_state == "unreadable" or key in status.unreadable_keys:
            state, age = "unknown", None
        elif timestamp is None:
            state, age = "never-observed", None
        else:
            age = round(observed_at - timestamp, 3)
            # A future-dated heartbeat means the clock moved, not that a cycle
            # was missed, so only a positive overdue age reads as stale.
            if age > expected[key]:
                # A key whose cycles stopped is described by that, not by what
                # its last completed cycles happened to parse.
                state = "stale"
            elif delivery.no_record_cycles >= NO_RECORD_CYCLE_TOLERANCE:
                state = "no-records"
            else:
                state = "fresh"
        entries.append(
            CollectionEntry(
                key=key,
                kind=kinds[key],
                state=state,
                last_capture_success_at=timestamp,
                age_seconds=age,
                expected_max_age_seconds=expected[key],
                last_records_at=delivery.last_records_at,
                no_record_cycles=delivery.no_record_cycles,
            )
        )
    return CollectionHealth(
        observed_at=observed_at,
        status_file_state=status.file_state,
        entries=tuple(entries),
    )


def _cycle_state_line(entry: CollectionEntry) -> str:
    """Describe one reported cycle state as this collector's own observation."""

    if entry.state == "no-records":
        return (
            f"[collector:health] no records parsed for {entry.kind} {entry.key} "
            f"in {entry.no_record_cycles} consecutive completed cycles."
        )
    return (
        f"[collector:health] no capture success for {entry.kind} {entry.key} "
        f"in {entry.age_seconds:.0f}s "
        f"(expected within {entry.expected_max_age_seconds:.0f}s)."
    )


def _cycle_recovery_line(key: str, reported: str) -> str:
    """Describe what a key left behind, so a recovery names what returned."""

    if reported == "no-records":
        return f"[collector:health] records parsed again for {key}."
    return f"[collector:health] capture success recorded again for {key}."


def log_stale_collection_transitions(health: CollectionHealth) -> tuple[str, ...]:
    """Report cycle-state changes once per transition and return the lines."""
    entries = {entry.key: entry for entry in health.entries}
    lines = []
    with _CYCLE_LOG_LOCK:
        for key, entry in sorted(entries.items()):
            reported = _LOGGED_CYCLE_STATES.get(key)
            if entry.state == reported:
                continue
            if entry.state in REPORTED_CYCLE_STATES:
                lines.append(_cycle_state_line(entry))
                _LOGGED_CYCLE_STATES[key] = entry.state
            elif reported is not None and entry.state == "fresh":
                # An unreadable heartbeat is not a recovery, so a remembered key
                # is only cleared once a later cycle both completes and delivers
                # under it.
                lines.append(_cycle_recovery_line(key, reported))
                del _LOGGED_CYCLE_STATES[key]
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


def _received_count(batch: Any) -> int | None:
    """Read how many records a batch parsed, or None when it does not say."""

    received = getattr(batch, "received", None)
    if isinstance(received, bool) or not isinstance(received, int) or received < 0:
        return None
    return received


def _capture_batch(source: str, leaves: list, processor, *, group: str) -> int | None:
    batch = processor(leaves)
    received = _received_count(batch)
    _record_success(group, received)
    _log_batch(source, batch)
    return received


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
                cycle_records: int | None = None
                for source, leaves in iter_feed_batches(total_limit=WATCH_NEWS_LIMIT):
                    if stop.is_set():
                        break
                    received = _capture_batch(source, leaves, processor, group=source)
                    cycle_succeeded = True
                    # The aggregate key carries what the whole cycle parsed, so
                    # a cycle no feed could count stays uncounted rather than
                    # summing to a zero nothing observed.
                    if received is not None:
                        cycle_records = (cycle_records or 0) + received
                if cycle_succeeded:
                    _record_success("news", cycle_records)
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
