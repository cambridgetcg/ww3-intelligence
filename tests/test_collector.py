import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core import collector
from core.registry import rss_source_profiles


REGISTERED_SOURCES = tuple(profile.source_id for profile in rss_source_profiles())
FRESH_AT = 100_000.0
STALE_AT = FRESH_AT - collector.NEWS_INTERVAL * collector.STALENESS_TOLERANCE - 1


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


@pytest.fixture(autouse=True)
def _forget_logged_staleness():
    """Transition logging is process-wide, so no test may inherit its memory."""

    collector._LOGGED_STALE_KEYS.clear()
    yield
    collector._LOGGED_STALE_KEYS.clear()


def _write_status(path, groups) -> None:
    path.write_text(json.dumps({"version": 1, "groups": groups}), encoding="utf-8")


def _health(path, *, now=FRESH_AT):
    with patch("core.collector.collector_status_path", return_value=path):
        return collector.collection_health(now=now)


def _entries(health) -> dict:
    return {entry.key: entry for entry in health.entries}


def test_collector_heartbeat_is_private_and_atomic(tmp_path):
    status = tmp_path / ".collector-status.json"

    with patch("core.collector.collector_status_path", return_value=status), \
         patch("core.collector.time.time", return_value=1234.5):
        collector._record_success("usgs")
        collector._record_success("news")

    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload == {
        "version": 1,
        "groups": {"news": 1234.5, "usgs": 1234.5},
    }
    assert status.stat().st_mode & 0o777 == 0o600
    assert not status.with_name(f"{status.name}.tmp").exists()


def test_collection_writes_are_serialized():
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def collect_batch(_leaves):
        nonlocal calls
        with calls_lock:
            calls += 1
            call_number = calls
        if call_number == 1:
            first_entered.set()
            assert release_first.wait(1)
        else:
            second_entered.set()
        return call_number

    with patch("core.collection.collect_batch", side_effect=collect_batch):
        first = threading.Thread(target=collector.process_collected_leaves, args=([],))
        second = threading.Thread(target=collector.process_collected_leaves, args=([],))
        first.start()
        assert first_entered.wait(1)
        second.start()
        assert not second_entered.wait(0.05)
        release_first.set()
        first.join(timeout=1)
        second.join(timeout=1)

    assert second_entered.is_set()
    assert not first.is_alive()
    assert not second.is_alive()


def test_recent_status_reads_fresh_for_every_group_and_registered_source(tmp_path):
    status = tmp_path / ".collector-status.json"
    keys = ("usgs", "news", "gdacs", *REGISTERED_SOURCES)
    _write_status(status, {key: FRESH_AT - 60 for key in keys})

    health = _health(status)

    assert health.health_version == "collection-health-v1"
    assert health.status_file_state == "present"
    assert health.stale_keys() == ()
    assert {entry.key for entry in health.entries} == set(keys)
    assert {entry.state for entry in health.entries} == {"fresh"}


def test_stale_source_is_reported_while_the_aggregate_group_is_fresh(tmp_path):
    status = tmp_path / ".collector-status.json"
    # One yielding publisher is all the aggregate ``news`` heartbeat needs.
    groups = {key: STALE_AT for key in REGISTERED_SOURCES}
    groups.update({"un_press": FRESH_AT, "usgs": FRESH_AT, "gdacs": FRESH_AT})
    groups["news"] = FRESH_AT
    _write_status(status, groups)

    health = _health(status)
    entries = _entries(health)
    overdue = entries["bbc_world"]

    assert entries["news"].state == "fresh"
    assert entries["news"].kind == "group"
    assert entries["un_press"].state == "fresh"
    assert overdue.state == "stale"
    assert overdue.kind == "source"
    assert overdue.age_seconds > overdue.expected_max_age_seconds
    assert set(health.stale_keys()) == set(REGISTERED_SOURCES) - {"un_press"}


def test_never_observed_source_is_distinct_from_a_stale_one(tmp_path):
    status = tmp_path / ".collector-status.json"
    _write_status(status, {"usgs": FRESH_AT, "news": STALE_AT})

    health = _health(status)
    entries = _entries(health)

    assert entries["news"].state == "stale"
    assert entries["bbc_world"].state == "never-observed"
    assert entries["bbc_world"].last_capture_success_at is None
    assert entries["bbc_world"].age_seconds is None
    assert health.stale_keys() == ("news",)


def test_future_dated_capture_success_does_not_read_as_stale(tmp_path):
    status = tmp_path / ".collector-status.json"
    _write_status(status, {"usgs": FRESH_AT + 1_000})

    entries = _entries(_health(status))

    assert entries["usgs"].state == "fresh"
    assert entries["usgs"].age_seconds == -1_000.0
    assert entries["usgs"].last_capture_success_at == FRESH_AT + 1_000


def test_expected_ages_derive_from_the_collector_intervals(tmp_path):
    status = tmp_path / ".collector-status.json"
    _write_status(status, {"usgs": FRESH_AT})

    entries = _entries(_health(status))
    expected = {key: entry.expected_max_age_seconds for key, entry in entries.items()}
    tolerance = collector.STALENESS_TOLERANCE

    assert expected["usgs"] == collector.USGS_INTERVAL * tolerance
    assert expected["news"] == collector.NEWS_INTERVAL * tolerance
    assert expected["gdacs"] == collector.GDACS_INTERVAL * tolerance
    assert expected["bbc_world"] == collector.NEWS_INTERVAL * tolerance


def test_absent_heartbeat_reports_never_observed_without_raising(tmp_path):
    missing = tmp_path / "never-written.json"

    health = _health(missing)

    assert health.status_file_state == "absent"
    assert {entry.state for entry in health.entries} == {"never-observed"}
    assert health.stale_keys() == ()

    with patch("core.collector.collector_status_path", return_value=None):
        unconfigured = collector.collection_health(now=FRESH_AT)

    assert unconfigured.status_file_state == "absent"
    assert unconfigured.stale_keys() == ()


def test_corrupt_heartbeat_degrades_to_unknown_without_raising(tmp_path):
    status = tmp_path / ".collector-status.json"
    status.write_text('{"version": 1, "groups"', encoding="utf-8")

    health = _health(status)

    assert health.status_file_state == "unreadable"
    assert {entry.state for entry in health.entries} == {"unknown"}
    assert health.stale_keys() == ()


def test_unusable_heartbeat_value_is_unknown_not_never_observed(tmp_path):
    status = tmp_path / ".collector-status.json"
    _write_status(status, {"usgs": "just now", "news": FRESH_AT})

    entries = _entries(_health(status))

    assert entries["usgs"].state == "unknown"
    assert entries["usgs"].last_capture_success_at is None
    assert entries["news"].state == "fresh"
    assert entries["gdacs"].state == "never-observed"


def test_collection_health_is_json_safe(tmp_path):
    status = tmp_path / ".collector-status.json"
    _write_status(status, {"usgs": 1_000.0, "news": STALE_AT})

    payload = json.loads(json.dumps(_health(status).as_dict()))
    entries = {entry["key"]: entry for entry in payload["entries"]}

    assert payload["health_version"] == "collection-health-v1"
    assert payload["observed_at"] == FRESH_AT
    assert payload["status_file_state"] == "present"
    assert payload["stale_keys"] == ["news", "usgs"]
    assert entries["usgs"]["last_capture_success_utc"] == "1970-01-01T00:16:40Z"
    assert entries["bbc_world"]["last_capture_success_utc"] is None


def test_staleness_is_logged_once_per_transition(tmp_path, capsys):
    status = tmp_path / ".collector-status.json"
    _write_status(status, {"usgs": STALE_AT})

    first = collector.log_stale_collection_transitions(_health(status))
    repeated = collector.log_stale_collection_transitions(_health(status))
    error = capsys.readouterr().err
    expected_age = collector.USGS_INTERVAL * collector.STALENESS_TOLERANCE

    assert len(first) == 1
    assert repeated == ()
    assert error.count("[collector:health]") == 1
    assert "no capture success for group usgs" in error
    assert f"expected within {expected_age}s" in error

    _write_status(status, {"usgs": FRESH_AT})
    recovery = collector.log_stale_collection_transitions(_health(status))

    assert recovery == ("[collector:health] capture success recorded again for usgs.",)
    assert collector._LOGGED_STALE_KEYS == set()


def test_unreadable_heartbeat_is_not_reported_as_a_recovery(tmp_path):
    status = tmp_path / ".collector-status.json"
    _write_status(status, {"usgs": STALE_AT})
    collector.log_stale_collection_transitions(_health(status))

    status.write_text("truncated", encoding="utf-8")
    lines = collector.log_stale_collection_transitions(_health(status))

    assert lines == ()
    assert collector._LOGGED_STALE_KEYS == {"usgs"}


def test_news_cycle_reports_the_sources_its_aggregate_heartbeat_hides(tmp_path, capsys):
    status = tmp_path / ".collector-status.json"
    _write_status(status, {key: STALE_AT for key in REGISTERED_SOURCES})
    batch = SimpleNamespace(received=0, new=0, known=0, revision=0, invalid=0)

    with patch("core.collector.collector_status_path", return_value=status), \
         patch("core.collector.time.time", return_value=FRESH_AT), \
         patch(
             "ingestion.news_feed.iter_feed_batches",
             return_value=iter([("un_press", [])]),
         ):
        collector.ingest_thread_news(
            _OneCycleStop(),
            interval=0,
            processor=lambda leaves: batch,
        )

    error = capsys.readouterr().err
    recorded = json.loads(status.read_text(encoding="utf-8"))["groups"]

    assert recorded["news"] == FRESH_AT
    assert recorded["un_press"] == FRESH_AT
    assert error.count("[collector:health]") == len(REGISTERED_SOURCES) - 1
    assert "no capture success for source bbc_world" in error
    assert "un_press" not in error
