import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ops import fly_runtime


class _FakeEvent:
    def __init__(self):
        self.stopped = False

    def wait(self, _timeout):
        return self.stopped

    def set(self):
        self.stopped = True


class _FakeProcess:
    def __init__(self, *, exited=False):
        self.exited = exited
        self.terminated = False

    def poll(self):
        if self.exited:
            return 0
        return -15 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        del timeout
        return self.poll()

    def kill(self):
        self.terminated = True


def test_runtime_commands_keep_collection_and_web_colocated():
    commands = fly_runtime._commands()

    assert commands["collector"][-2:] == ["-m", "core.collector"]
    assert commands["web"][1:4] == ["-m", "streamlit", "run"]
    assert "--server.address=0.0.0.0" in commands["web"]
    assert "--server.port=8080" in commands["web"]


def test_runtime_preflights_database_and_stops_peer_on_child_exit():
    events = []
    collector = _FakeProcess()
    web = _FakeProcess(exited=True)

    def init_db():
        events.append("init-db")

    def close_db():
        events.append("close-db")

    def popen(command):
        events.append(("start", command[-1]))
        return collector if "core.collector" in command else web

    with patch("ops.fly_runtime._prepare_runtime_data", side_effect=lambda: events.append("prepare-data")), \
         patch("ops.fly_runtime.init_db", side_effect=init_db), \
         patch("ops.fly_runtime.database.close", side_effect=close_db), \
         patch("ops.fly_runtime.subprocess.Popen", side_effect=popen), \
         patch("ops.fly_runtime.threading.Event", return_value=_FakeEvent()), \
         patch("ops.fly_runtime.signal.signal"):
        result = fly_runtime.run()

    assert result == 1
    assert events[:3] == ["prepare-data", "init-db", "close-db"]
    assert events[3:] == [
        ("start", "core.collector"),
        ("start", "--browser.gatherUsageStats=false"),
    ]
    assert collector.terminated is True


def test_runtime_health_allows_one_fresh_collection_group(tmp_path):
    status = tmp_path / ".collector-status.json"
    status.write_text(
        json.dumps({"version": 1, "groups": {"usgs": 995.0, "news": 1.0}}),
        encoding="utf-8",
    )

    with patch("ops.fly_runtime.COLLECTOR_STATUS", status), \
         patch("ops.fly_runtime.shutil.disk_usage", return_value=SimpleNamespace(free=10**9)), \
         patch("ops.fly_runtime.time.time", return_value=1_000.0):
        assert fly_runtime._runtime_health_error(started_at=0.0) is None


def test_runtime_health_fails_when_all_collection_groups_are_stale(tmp_path):
    status = tmp_path / ".collector-status.json"
    status.write_text(
        json.dumps({
            "version": 1,
            "groups": {"usgs": 1.0, "news": 1.0, "gdacs": 1.0},
        }),
        encoding="utf-8",
    )

    with patch("ops.fly_runtime.COLLECTOR_STATUS", status), \
         patch("ops.fly_runtime.shutil.disk_usage", return_value=SimpleNamespace(free=10**9)), \
         patch("ops.fly_runtime.time.time", return_value=10_000.0):
        assert fly_runtime._runtime_health_error(0.0) == "all collection groups are stale"


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
