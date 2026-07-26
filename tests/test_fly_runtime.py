import json
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core import collector
from ops import fly_runtime


class _FakeEvent:
    def __init__(self, *, stop_after=None):
        self.stop_after = stop_after
        self.waits = 0
        self.stopped = False

    def wait(self, _timeout):
        self.waits += 1
        if self.stop_after is not None and self.waits > self.stop_after:
            self.stopped = True
        return self.stopped

    def set(self):
        self.stopped = True


class _FakeProcess:
    def __init__(self, *, exited=False, return_code=0):
        self.exited = exited
        self.return_code = return_code
        self.terminated = False

    def poll(self):
        if self.exited:
            return self.return_code
        return -15 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        del timeout
        return self.poll()

    def kill(self):
        self.terminated = True


class _StubbornProcess:
    def __init__(self):
        self.terminated = False
        self.killed = False
        self.waits = []

    def poll(self):
        return 0 if self.killed else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if not self.killed:
            raise subprocess.TimeoutExpired(cmd="child", timeout=timeout)
        return 0

    def kill(self):
        self.killed = True


class _FakeClock:
    def __init__(self, step=30.0):
        self.now = 0.0
        self.step = step

    def __call__(self):
        value = self.now
        self.now += self.step
        return value


_GRACE = fly_runtime.INITIAL_COLLECTOR_GRACE


def _heartbeat(directory, groups):
    """Write a collector heartbeat with the given per-group capture times."""
    status = directory / ".collector-status.json"
    status.write_text(
        json.dumps({"version": 1, "groups": groups}),
        encoding="utf-8",
    )
    return status


def _health_error(status, *, uptime, now=1_000.0):
    """Judge one heartbeat at a fixed process uptime and wall-clock reading."""
    with patch("ops.fly_runtime.COLLECTOR_STATUS", status), \
         patch("ops.fly_runtime.shutil.disk_usage", return_value=SimpleNamespace(free=10**9)), \
         patch("ops.fly_runtime.time.time", return_value=now), \
         patch("ops.fly_runtime.time.monotonic", return_value=uptime):
        return fly_runtime._runtime_health_error(started_at=0.0)


def _run_supervisor(popen, *, stop_after):
    """Run the supervisor over fake children, a fake clock, and no health I/O."""
    with patch("ops.fly_runtime._prepare_runtime_data"), \
         patch("ops.fly_runtime.init_db"), \
         patch("ops.fly_runtime.database.close"), \
         patch("ops.fly_runtime._runtime_health_error", return_value=None), \
         patch("ops.fly_runtime.subprocess.Popen", side_effect=popen), \
         patch("ops.fly_runtime.time.monotonic", side_effect=_FakeClock()), \
         patch("ops.fly_runtime.threading.Event", return_value=_FakeEvent(stop_after=stop_after)), \
         patch("ops.fly_runtime.signal.signal"):
        return fly_runtime.run()


def test_runtime_commands_keep_collection_and_web_colocated():
    commands = fly_runtime._commands()

    assert commands["collector"][-2:] == ["-m", "core.collector"]
    assert commands["web"][1:4] == ["-m", "streamlit", "run"]
    assert "--server.address=0.0.0.0" in commands["web"]
    assert "--server.port=8080" in commands["web"]


def test_runtime_preflights_database_before_starting_children():
    events = []
    collector_process = _FakeProcess()
    web = _FakeProcess()

    def popen(command):
        events.append(("start", command[-1]))
        return collector_process if "core.collector" in command else web

    with patch("ops.fly_runtime._prepare_runtime_data", side_effect=lambda: events.append("prepare-data")), \
         patch("ops.fly_runtime.init_db", side_effect=lambda: events.append("init-db")), \
         patch("ops.fly_runtime.database.close", side_effect=lambda: events.append("close-db")), \
         patch("ops.fly_runtime._runtime_health_error", return_value=None), \
         patch("ops.fly_runtime.subprocess.Popen", side_effect=popen), \
         patch("ops.fly_runtime.threading.Event", return_value=_FakeEvent(stop_after=2)), \
         patch("ops.fly_runtime.signal.signal"):
        result = fly_runtime.run()

    assert result == 0
    assert events == [
        "prepare-data",
        "init-db",
        "close-db",
        ("start", "core.collector"),
        ("start", "--browser.gatherUsageStats=false"),
    ]
    assert collector_process.terminated is True
    assert web.terminated is True


def test_runtime_respawns_a_dead_child_instead_of_ending_the_run():
    started = []
    collector_process = _FakeProcess()
    replacement = _FakeProcess()

    def popen(command):
        if "core.collector" in command:
            started.append("collector")
            return collector_process
        started.append("web")
        return _FakeProcess(exited=True, return_code=1) if len(started) == 2 else replacement

    result = _run_supervisor(popen, stop_after=6)

    assert result == 0
    assert started == ["collector", "web", "web"]
    assert collector_process.terminated is True
    assert replacement.terminated is True


def test_runtime_exits_once_a_child_cannot_be_kept_alive():
    started = []
    collector_process = _FakeProcess()

    def popen(command):
        if "core.collector" in command:
            started.append("collector")
            return collector_process
        started.append("web")
        return _FakeProcess(exited=True, return_code=3)

    result = _run_supervisor(popen, stop_after=200)

    assert result == 3
    assert started.count("web") == fly_runtime.CHILD_RESTART_LIMIT + 1
    assert collector_process.terminated is True


def test_runtime_termination_signal_stops_children_without_an_error_code():
    handlers = {}
    processes = []

    def popen(_command):
        process = _FakeProcess()
        processes.append(process)
        if len(processes) == 2:
            handlers[signal.SIGTERM](signal.SIGTERM, None)
        return process

    with patch("ops.fly_runtime._prepare_runtime_data"), \
         patch("ops.fly_runtime.init_db"), \
         patch("ops.fly_runtime.database.close"), \
         patch("ops.fly_runtime._runtime_health_error", return_value=None), \
         patch("ops.fly_runtime.subprocess.Popen", side_effect=popen), \
         patch("ops.fly_runtime.threading.Event", return_value=_FakeEvent()), \
         patch("ops.fly_runtime.signal.signal", side_effect=handlers.__setitem__):
        result = fly_runtime.run()

    assert result == 0
    assert set(handlers) == {signal.SIGINT, signal.SIGTERM}
    assert [process.terminated for process in processes] == [True, True]


def test_restart_delay_backs_off_and_stays_capped():
    delays = [fly_runtime._restart_delay(failures) for failures in range(1, 9)]

    assert delays[0] == fly_runtime.CHILD_RESTART_BACKOFF
    assert delays == sorted(delays)
    assert max(delays) == fly_runtime.CHILD_RESTART_BACKOFF_CAP
    assert fly_runtime._restart_delay(0) == fly_runtime.CHILD_RESTART_BACKOFF


def test_supervised_child_forgives_failures_after_a_stable_uptime():
    child = fly_runtime._SupervisedChild("web", ["true"])

    with patch("ops.fly_runtime.subprocess.Popen", return_value=_FakeProcess()):
        child.spawn(0.0)
        child.note_exit(1.0)
        child.spawn(1.0)
        child.note_exit(1.0 + fly_runtime.CHILD_STABLE_UPTIME)

    assert child.failures == 1
    assert child.process is None


def test_stop_children_kills_a_child_that_outlives_the_shutdown_timeout():
    stubborn = _StubbornProcess()

    fly_runtime._stop_children([stubborn])

    assert stubborn.terminated is True
    assert stubborn.killed is True
    assert 0 < stubborn.waits[0] <= fly_runtime.SHUTDOWN_TIMEOUT


def test_collection_staleness_bounds_derive_from_the_collector_intervals():
    assert fly_runtime.COLLECTOR_INTERVALS == {
        "usgs": collector.USGS_INTERVAL,
        "news": collector.NEWS_INTERVAL,
        "gdacs": collector.GDACS_INTERVAL,
    }
    assert set(fly_runtime.COLLECTOR_MAX_AGES) == set(fly_runtime.COLLECTOR_INTERVALS)
    for group, interval in fly_runtime.COLLECTOR_INTERVALS.items():
        assert fly_runtime.COLLECTOR_MAX_AGES[group] >= interval
        # A grace shorter than the bound it judges reads a collector that has
        # only just restarted as a dead one.
        assert fly_runtime.INITIAL_COLLECTOR_GRACE > fly_runtime.COLLECTOR_MAX_AGES[group]


def test_supervisor_and_watcher_agree_on_what_stale_means():
    # Two tolerances for one heartbeat is the worst of both: the dashboard
    # reports every group fresh while this hands the Machine back to Fly.
    health = collector.collection_health(now=1_000.0)
    expectations = {
        entry["key"]: entry["expected_max_age_seconds"]
        for entry in health.as_dict()["entries"]
        if entry["kind"] == "group"
    }
    assert expectations == fly_runtime.COLLECTOR_MAX_AGES


def test_runtime_health_allows_one_fresh_collection_group(tmp_path):
    status = _heartbeat(tmp_path, {"usgs": 995.0, "news": 1.0})

    assert _health_error(status, uptime=0.0, now=1_000.0) is None


def test_runtime_health_tolerates_a_restart_stale_heartbeat_within_the_grace(tmp_path):
    status = _heartbeat(tmp_path, {"usgs": 1.0, "news": 1.0, "gdacs": 1.0})

    assert _health_error(status, uptime=_GRACE - 1, now=100_000.0) is None


def test_runtime_health_fails_when_all_collection_groups_are_stale(tmp_path):
    status = _heartbeat(tmp_path, {"usgs": 1.0, "news": 1.0, "gdacs": 1.0})

    assert _health_error(status, uptime=_GRACE, now=100_000.0) == (
        "all collection groups are stale"
    )


def test_runtime_health_waits_for_a_missing_heartbeat_then_reports_it(tmp_path):
    status = tmp_path / ".collector-status.json"

    assert _health_error(status, uptime=_GRACE - 1) is None
    assert _health_error(status, uptime=_GRACE) == (
        "collector heartbeat was not created during startup grace"
    )


def test_runtime_health_waits_for_an_unreadable_heartbeat_then_reports_it(tmp_path):
    status = tmp_path / ".collector-status.json"
    status.write_text('{"version": 1}', encoding="utf-8")

    assert _health_error(status, uptime=_GRACE - 1) is None
    assert _health_error(status, uptime=_GRACE) == "collector heartbeat is unreadable"


def test_runtime_health_reads_a_backwards_clock_step_as_a_live_collector(tmp_path):
    status = _heartbeat(tmp_path, {"usgs": 100_000.0})

    assert _health_error(status, uptime=10 * _GRACE, now=1_000.0) is None


def test_fresh_collection_groups_bounds_only_positive_age():
    ahead = fly_runtime._fresh_collection_groups({"gdacs": 2_000.0}, 1_000.0)
    inside = fly_runtime._fresh_collection_groups({"usgs": 900.0}, 1_000.0)
    outside = fly_runtime._fresh_collection_groups(
        {group: 1.0 for group in fly_runtime.COLLECTOR_MAX_AGES},
        100_000.0,
    )

    assert ahead == ["gdacs"]
    assert inside == ["usgs"]
    assert outside == []


def test_fresh_collection_groups_ignores_non_numeric_heartbeats():
    groups = {"usgs": "1000", "news": None, "gdacs": {"at": 1000}}

    assert fly_runtime._fresh_collection_groups(groups, 1_000.0) == []


def test_runtime_health_fails_before_disk_is_full():
    with patch(
        "ops.fly_runtime.shutil.disk_usage",
        return_value=SimpleNamespace(free=fly_runtime.MIN_FREE_BYTES - 1),
    ):
        assert fly_runtime._runtime_health_error(0.0) == "/data has less than 64 MiB free"


def test_runtime_requires_data_to_be_a_mount(tmp_path):
    with patch("ops.fly_runtime.DATA_MOUNT", tmp_path), \
         pytest.raises(RuntimeError, match="not a mounted volume"):
        fly_runtime._prepare_runtime_data()


def test_runtime_refuses_root_without_a_runtime_uid(tmp_path, monkeypatch):
    monkeypatch.setenv("WW3_RUNTIME_UID", "0")

    with patch("ops.fly_runtime.DATA_MOUNT", tmp_path), \
         patch.object(Path, "is_mount", return_value=True), \
         patch("ops.fly_runtime.shutil.disk_usage", return_value=SimpleNamespace(free=10**9)), \
         patch("ops.fly_runtime.os.geteuid", return_value=0), \
         patch("ops.fly_runtime.os.setuid") as setuid, \
         pytest.raises(RuntimeError, match="WW3_RUNTIME_UID"):
        fly_runtime._prepare_runtime_data()

    assert setuid.call_count == 0


def test_runtime_drops_root_to_the_configured_runtime_user(tmp_path, monkeypatch):
    (tmp_path / "stream").mkdir()
    (tmp_path / "stream" / "events.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setenv("WW3_RUNTIME_UID", "10001")
    monkeypatch.delenv("WW3_RUNTIME_GID", raising=False)
    chowned = []

    def chown(path, uid, gid, follow_symlinks=True):
        chowned.append((Path(path).name, uid, gid, follow_symlinks))

    with patch("ops.fly_runtime.DATA_MOUNT", tmp_path), \
         patch.object(Path, "is_mount", return_value=True), \
         patch("ops.fly_runtime.shutil.disk_usage", return_value=SimpleNamespace(free=10**9)), \
         patch("ops.fly_runtime.os.geteuid", return_value=0), \
         patch("ops.fly_runtime.os.chown", side_effect=chown), \
         patch("ops.fly_runtime.os.setgroups") as setgroups, \
         patch("ops.fly_runtime.os.setgid") as setgid, \
         patch("ops.fly_runtime.os.setuid") as setuid:
        fly_runtime._prepare_runtime_data()

    assert {entry[1:] for entry in chowned} == {(10001, 10001, False)}
    assert {entry[0] for entry in chowned} == {tmp_path.name, "stream", "events.jsonl"}
    assert setgroups.call_args.args == ([],)
    assert setgid.call_args.args == (10001,)
    assert setuid.call_args.args == (10001,)


def test_runtime_data_preparation_is_silent_for_a_non_root_run(tmp_path, monkeypatch):
    monkeypatch.setenv("WW3_RUNTIME_UID", "0")

    with patch("ops.fly_runtime.DATA_MOUNT", tmp_path), \
         patch.object(Path, "is_mount", return_value=True), \
         patch("ops.fly_runtime.shutil.disk_usage", return_value=SimpleNamespace(free=10**9)), \
         patch("ops.fly_runtime.os.geteuid", return_value=1_000), \
         patch("ops.fly_runtime.os.chown") as chown, \
         patch("ops.fly_runtime.os.setuid") as setuid:
        assert fly_runtime._prepare_runtime_data() is None

    assert chown.call_count == 0
    assert setuid.call_count == 0
