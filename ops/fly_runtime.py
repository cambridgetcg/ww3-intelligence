"""Supervise the Streamlit dashboard and colocated collection-only watcher."""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from core.collector import GROUP_INTERVALS, STALENESS_TOLERANCE
from core.db import database, init_db


SHUTDOWN_TIMEOUT = 30
DATA_MOUNT = Path("/data")
COLLECTOR_STATUS = DATA_MOUNT / ".collector-status.json"
MIN_FREE_BYTES = 64 * 1024 * 1024
HEALTH_INTERVAL = 30
POLL_INTERVAL = 0.5
CHILD_RESTART_BACKOFF = 5
CHILD_RESTART_BACKOFF_CAP = 60
CHILD_RESTART_LIMIT = 5
CHILD_STABLE_UPTIME = 10 * 60

# The watcher owns both its cycle lengths and how many cycles may be missed
# before a group reads as stale.  A second copy of either would drift from the
# loops this supervisor is judging, and the two would disagree about the same
# heartbeat: the dashboard reporting every group fresh while this handed the
# Machine back to Fly for a collector that was doing its job.
COLLECTOR_INTERVALS = GROUP_INTERVALS
COLLECTOR_MAX_AGES = {
    group: float(interval * STALENESS_TOLERANCE)
    for group, interval in COLLECTOR_INTERVALS.items()
}
# The heartbeat lives on the persistent volume, so a restarted Machine reads
# the timestamps its previous life left behind and every group is stale by
# construction until its own interval has run again.  A grace shorter than the
# staleness bounds it judges reports a live collector as dead, so the grace is
# the slowest bound plus room for one further slow cycle.
INITIAL_COLLECTOR_GRACE = max(COLLECTOR_MAX_AGES.values()) + max(
    COLLECTOR_INTERVALS.values()
)


def _prepare_runtime_data() -> None:
    """Require the Fly volume, then drop root after repairing its ownership."""
    if not DATA_MOUNT.is_mount():
        raise RuntimeError("/data is not a mounted volume")
    if shutil.disk_usage(DATA_MOUNT).free < MIN_FREE_BYTES:
        raise RuntimeError("/data has less than 64 MiB free")

    # An unprivileged local run has no ownership to repair and nothing to drop.
    if os.geteuid() != 0:
        return

    runtime_uid = int(os.environ.get("WW3_RUNTIME_UID", "0"))
    runtime_gid = int(os.environ.get("WW3_RUNTIME_GID", str(runtime_uid)))
    # Without a runtime user the public, unauthenticated dashboard would keep
    # root's read-write mount on the evidence volume, so a missing UID is a
    # configuration failure rather than a silent fallback.
    if runtime_uid <= 0:
        raise RuntimeError("WW3_RUNTIME_UID must name a non-root runtime user")

    for root, directories, files in os.walk(DATA_MOUNT, followlinks=False):
        os.chown(root, runtime_uid, runtime_gid, follow_symlinks=False)
        for name in [*directories, *files]:
            os.chown(
                Path(root) / name,
                runtime_uid,
                runtime_gid,
                follow_symlinks=False,
            )
    os.setgroups([])
    os.setgid(runtime_gid)
    os.setuid(runtime_uid)


def _fresh_collection_groups(groups: dict, now: float) -> list[str]:
    """Return the groups whose heartbeat sits inside its staleness bound."""
    fresh = []
    for group, max_age in COLLECTOR_MAX_AGES.items():
        timestamp = groups.get(group)
        if not isinstance(timestamp, (int, float)):
            continue
        # A heartbeat dated ahead of the reader means the clock stepped, not
        # that collection stopped.  Only a positive age evidences staleness.
        if now - timestamp <= max_age:
            fresh.append(group)
    return fresh


def _runtime_health_error(started_at: float) -> str | None:
    """Return a fail-closed runtime error when storage or all feeds are stale."""
    try:
        if shutil.disk_usage(DATA_MOUNT).free < MIN_FREE_BYTES:
            return "/data has less than 64 MiB free"
    except OSError:
        return "/data free-space check failed"

    # Every collection verdict shares the startup grace: a young process cannot
    # distinguish a dead collector from the stale heartbeat a restart inherited.
    young = time.monotonic() - started_at < INITIAL_COLLECTOR_GRACE
    try:
        payload = json.loads(COLLECTOR_STATUS.read_text(encoding="utf-8"))
        groups = payload.get("groups") if isinstance(payload, dict) else None
        if not isinstance(groups, dict):
            raise ValueError("invalid groups")
    except FileNotFoundError:
        if young:
            return None
        return "collector heartbeat was not created during startup grace"
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        if young:
            return None
        return "collector heartbeat is unreadable"

    if not _fresh_collection_groups(groups, time.time()):
        if young:
            return None
        return "all collection groups are stale"
    return None


def _commands() -> dict[str, list[str]]:
    return {
        "collector": [sys.executable, "-m", "core.collector"],
        "web": [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            "dashboard/app.py",
            "--server.address=0.0.0.0",
            "--server.port=8080",
            "--server.headless=true",
            "--browser.gatherUsageStats=false",
        ],
    }


def _restart_delay(failures: int) -> float:
    """Return the capped exponential backoff owed before the next respawn."""
    exponent = max(failures - 1, 0)
    return float(
        min(CHILD_RESTART_BACKOFF * 2**exponent, CHILD_RESTART_BACKOFF_CAP)
    )


@dataclass(slots=True)
class _SupervisedChild:
    """One supervised process and the restart budget it has left."""

    name: str
    command: list[str]
    process: subprocess.Popen | None = None
    spawned_at: float = 0.0
    failures: int = 0
    retry_at: float = 0.0

    def spawn(self, now: float) -> None:
        """Start the command and record when this attempt began."""
        self.process = subprocess.Popen(self.command)
        self.spawned_at = now
        self.retry_at = now

    def note_exit(self, now: float) -> None:
        """Charge one failure, forgiving a child that stayed up long enough."""
        self.process = None
        if now - self.spawned_at >= CHILD_STABLE_UPTIME:
            self.failures = 0
        self.failures += 1
        self.retry_at = now + _restart_delay(self.failures)


def _supervise_child(child: _SupervisedChild, now: float) -> int | None:
    """Respawn a dead child in place, or return the code that ends the run."""
    if child.process is None:
        if now >= child.retry_at:
            child.spawn(now)
        return None

    return_code = child.process.poll()
    if return_code is None:
        return None

    child.note_exit(now)
    # Escalating to Fly costs the public dashboard and one of the Machine's
    # finite restart attempts, so only a child that cannot be kept alive here
    # is worth a Machine restart.
    if child.failures > CHILD_RESTART_LIMIT:
        print(
            f"[runtime] {child.name} exited ({return_code}) after "
            f"{CHILD_RESTART_LIMIT} restarts.",
            file=sys.stderr,
            flush=True,
        )
        return return_code if return_code != 0 else 1

    print(
        f"[runtime] {child.name} exited ({return_code}); restarting in "
        f"{child.retry_at - now:.0f}s.",
        file=sys.stderr,
        flush=True,
    )
    return None


def _stop_children(children: list[subprocess.Popen]) -> None:
    """Terminate every live child, then kill whatever outlives the timeout."""
    for child in children:
        if child.poll() is None:
            child.terminate()

    deadline = time.monotonic() + SHUTDOWN_TIMEOUT
    for child in children:
        if child.poll() is not None:
            continue
        remaining = max(0, deadline - time.monotonic())
        try:
            child.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def run() -> int:
    # Streamlit's own health endpoint can become ready before it executes the
    # app.  Perform the SQLite mount/schema/integrity preflight first instead.
    _prepare_runtime_data()
    init_db()
    database.close()

    stop = threading.Event()

    def request_stop(_signum, _frame) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    children = [
        _SupervisedChild(name, command) for name, command in _commands().items()
    ]
    started_at = time.monotonic()
    next_health_check = started_at
    try:
        for child in children:
            child.spawn(started_at)
        while not stop.wait(POLL_INTERVAL):
            now = time.monotonic()
            for child in children:
                return_code = _supervise_child(child, now)
                if return_code is not None:
                    return return_code
            if now >= next_health_check:
                error = _runtime_health_error(started_at)
                if error is not None:
                    print(f"[runtime] {error}.", file=sys.stderr, flush=True)
                    return 1
                next_health_check = now + HEALTH_INTERVAL
        return 0
    finally:
        stop.set()
        _stop_children(
            [child.process for child in children if child.process is not None]
        )


if __name__ == "__main__":
    raise SystemExit(run())
