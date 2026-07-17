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
from pathlib import Path

from core.db import database, init_db


SHUTDOWN_TIMEOUT = 30
DATA_MOUNT = Path("/data")
COLLECTOR_STATUS = DATA_MOUNT / ".collector-status.json"
MIN_FREE_BYTES = 64 * 1024 * 1024
INITIAL_COLLECTOR_GRACE = 20 * 60
COLLECTOR_RECOVERY_GRACE = 2 * 60
HEALTH_INTERVAL = 30
COLLECTOR_MAX_AGES = {
    "usgs": 15 * 60,
    "news": 40 * 60,
    "gdacs": 40 * 60,
}


def _prepare_runtime_data() -> None:
    """Require the Fly volume, then drop root after repairing its ownership."""
    if not DATA_MOUNT.is_mount():
        raise RuntimeError("/data is not a mounted volume")
    if shutil.disk_usage(DATA_MOUNT).free < MIN_FREE_BYTES:
        raise RuntimeError("/data has less than 64 MiB free")

    runtime_uid = int(os.environ.get("WW3_RUNTIME_UID", "0"))
    runtime_gid = int(os.environ.get("WW3_RUNTIME_GID", str(runtime_uid)))
    if os.geteuid() != 0 or runtime_uid <= 0:
        return

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


def _runtime_health_error(started_at: float) -> str | None:
    """Return a fail-closed runtime error when storage or all feeds are stale."""
    try:
        if shutil.disk_usage(DATA_MOUNT).free < MIN_FREE_BYTES:
            return "/data has less than 64 MiB free"
    except OSError:
        return "/data free-space check failed"

    uptime = time.monotonic() - started_at
    try:
        payload = json.loads(COLLECTOR_STATUS.read_text(encoding="utf-8"))
        groups = payload.get("groups") if isinstance(payload, dict) else None
        if not isinstance(groups, dict):
            raise ValueError("invalid groups")
    except FileNotFoundError:
        if uptime < INITIAL_COLLECTOR_GRACE:
            return None
        return "collector heartbeat was not created during startup grace"
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        if uptime < COLLECTOR_RECOVERY_GRACE:
            return None
        return "collector heartbeat is unreadable"

    now = time.time()
    fresh_groups = []
    for group, max_age in COLLECTOR_MAX_AGES.items():
        timestamp = groups.get(group)
        if isinstance(timestamp, (int, float)) and 0 <= now - timestamp <= max_age:
            fresh_groups.append(group)
    if not fresh_groups:
        if uptime < COLLECTOR_RECOVERY_GRACE:
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


def _stop_children(children: dict[str, subprocess.Popen]) -> None:
    for child in children.values():
        if child.poll() is None:
            child.terminate()

    deadline = time.monotonic() + SHUTDOWN_TIMEOUT
    for child in children.values():
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

    children: dict[str, subprocess.Popen] = {}
    started_at = time.monotonic()
    next_health_check = started_at
    try:
        for name, command in _commands().items():
            children[name] = subprocess.Popen(command)
        while not stop.wait(0.5):
            for name, child in children.items():
                return_code = child.poll()
                if return_code is not None:
                    print(
                        f"[runtime] {name} exited unexpectedly ({return_code}).",
                        file=sys.stderr,
                        flush=True,
                    )
                    return return_code if return_code != 0 else 1
            now = time.monotonic()
            if now >= next_health_check:
                error = _runtime_health_error(started_at)
                if error is not None:
                    print(f"[runtime] {error}.", file=sys.stderr, flush=True)
                    return 1
                next_health_check = now + HEALTH_INTERVAL
        return 0
    finally:
        stop.set()
        _stop_children(children)


if __name__ == "__main__":
    raise SystemExit(run())
