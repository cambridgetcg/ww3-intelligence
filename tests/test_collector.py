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

    collector._LOGGED_CYCLE_STATES.clear()
    yield
    collector._LOGGED_CYCLE_STATES.clear()


def _write_status(path, groups, records=None) -> None:
    # The deployed heartbeat holds ``version`` and ``groups`` only, so a test
    # that names no record counts writes exactly the file on the live volume.
    payload = {"version": 1, "groups": groups}
    if records is not None:
        payload["records"] = records
    path.write_text(json.dumps(payload), encoding="utf-8")


def _delivered(at=FRESH_AT, cycles=0) -> dict:
    return {"last_records_at": at, "no_record_cycles": cycles}


def _cycles(path, key, received, *, count=1, at=FRESH_AT) -> None:
    """Record completed cycles for one key against a fixed clock."""

    with patch("core.collector.collector_status_path", return_value=path), \
         patch("core.collector.time.time", return_value=at):
        for _ in range(count):
            collector._record_success(key, received)


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
        "records": {},
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

    assert health.health_version == "collection-health-v2"
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

    assert payload["health_version"] == "collection-health-v2"
    assert payload["observed_at"] == FRESH_AT
    assert payload["status_file_state"] == "present"
    assert payload["stale_keys"] == ["news", "usgs"]
    assert payload["no_record_keys"] == []
    assert entries["usgs"]["last_capture_success_utc"] == "1970-01-01T00:16:40Z"
    assert entries["bbc_world"]["last_capture_success_utc"] is None
    assert entries["bbc_world"]["last_records_utc"] is None
    assert entries["bbc_world"]["no_record_cycles"] == 0


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
    assert collector._LOGGED_CYCLE_STATES == {}


def test_unreadable_heartbeat_is_not_reported_as_a_recovery(tmp_path):
    status = tmp_path / ".collector-status.json"
    _write_status(status, {"usgs": STALE_AT})
    collector.log_stale_collection_transitions(_health(status))

    status.write_text("truncated", encoding="utf-8")
    lines = collector.log_stale_collection_transitions(_health(status))

    assert lines == ()
    assert collector._LOGGED_CYCLE_STATES == {"usgs": "stale"}


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


def test_a_source_still_delivering_records_reads_fresh(tmp_path):
    status = tmp_path / ".collector-status.json"

    _cycles(
        status,
        "un_press",
        0,
        count=collector.NO_RECORD_CYCLE_TOLERANCE + 2,
        at=FRESH_AT - 600,
    )
    _cycles(status, "un_press", 25)
    entry = _entries(_health(status))["un_press"]

    assert entry.state == "fresh"
    assert entry.no_record_cycles == 0
    assert entry.last_records_at == FRESH_AT
    assert _health(status).no_record_keys() == ()


def test_record_free_cycles_reach_no_records_only_after_the_threshold(tmp_path):
    status = tmp_path / ".collector-status.json"

    _cycles(status, "usgs", 12, at=FRESH_AT - 900)
    _cycles(status, "usgs", 0, count=collector.NO_RECORD_CYCLE_TOLERANCE - 1)
    before = _entries(_health(status))["usgs"]

    _cycles(status, "usgs", 0)
    after = _entries(_health(status))["usgs"]

    assert before.state == "fresh"
    assert before.no_record_cycles == collector.NO_RECORD_CYCLE_TOLERANCE - 1
    assert after.state == "no-records"
    assert after.no_record_cycles == collector.NO_RECORD_CYCLE_TOLERANCE
    # The cycle completed just now, and the last records this collector parsed
    # are 900 seconds older: that gap is the whole point of the two timestamps.
    assert after.last_capture_success_at == FRESH_AT
    assert after.last_records_at == FRESH_AT - 900


def test_no_records_is_distinct_from_stale_and_from_fresh(tmp_path):
    status = tmp_path / ".collector-status.json"
    empty = _delivered(STALE_AT, collector.NO_RECORD_CYCLE_TOLERANCE)
    _write_status(
        status,
        {"usgs": FRESH_AT, "news": FRESH_AT, "bbc_world": STALE_AT},
        {"usgs": _delivered(FRESH_AT - 60), "news": empty, "bbc_world": empty},
    )

    health = _health(status)
    entries = _entries(health)

    assert entries["usgs"].state == "fresh"
    assert entries["news"].state == "no-records"
    # A key whose cycles stopped is described by that; the counter it froze at
    # is not a second verdict on the same key.
    assert entries["bbc_world"].state == "stale"
    assert health.no_record_keys() == ("news",)
    assert health.stale_keys() == ("bbc_world",)
    assert set(health.no_record_keys()).isdisjoint(health.stale_keys())


def test_a_heartbeat_in_the_previous_format_reads_without_a_record_free_run(tmp_path):
    status = tmp_path / ".collector-status.json"
    # Exactly the file the running deployment's volume holds: cycle timestamps
    # and no record counts anywhere.
    _write_status(status, {"usgs": FRESH_AT, "news": FRESH_AT, "gdacs": FRESH_AT})

    health = _health(status)
    entries = _entries(health)

    assert health.status_file_state == "present"
    assert entries["usgs"].state == "fresh"
    assert entries["usgs"].no_record_cycles == 0
    assert entries["usgs"].last_records_at is None
    assert health.no_record_keys() == ()
    assert health.stale_keys() == ()


def test_a_recorded_cycle_keeps_the_shape_an_older_reader_expects(tmp_path):
    status = tmp_path / ".collector-status.json"
    _write_status(status, {"news": FRESH_AT - 30})

    _cycles(status, "usgs", 0, count=collector.NO_RECORD_CYCLE_TOLERANCE + 1)
    payload = json.loads(status.read_text(encoding="utf-8"))

    # The supervisor reads this file seconds after boot and knows only
    # ``version`` and ``groups``.  Record counts are a field it never asks for,
    # and the timestamps it does ask for keep their bare epoch shape.
    assert payload["version"] == 1
    assert payload["groups"] == {"news": FRESH_AT - 30, "usgs": FRESH_AT}
    assert all(isinstance(value, float) for value in payload["groups"].values())


def test_a_heartbeat_carrying_unknown_fields_still_reads(tmp_path):
    status = tmp_path / ".collector-status.json"
    status.write_text(
        json.dumps({
            "version": 9,
            "groups": {"usgs": FRESH_AT},
            "records": {"usgs": {**_delivered(), "cycles_per_hour": 12}},
            "later_field": [1, 2, 3],
        }),
        encoding="utf-8",
    )

    entry = _entries(_health(status))["usgs"]

    assert entry.state == "fresh"
    assert entry.last_records_at == FRESH_AT
    assert entry.no_record_cycles == 0


def test_an_unusable_record_count_is_not_a_record_free_cycle(tmp_path):
    status = tmp_path / ".collector-status.json"
    _write_status(
        status,
        {"usgs": FRESH_AT, "news": FRESH_AT, "gdacs": FRESH_AT},
        {
            "usgs": "many",
            "news": {"no_record_cycles": "9", "last_records_at": "recently"},
            "gdacs": {"no_record_cycles": -4},
        },
    )

    entries = _entries(_health(status))

    assert {entries[key].state for key in ("usgs", "news", "gdacs")} == {"fresh"}
    assert entries["news"].no_record_cycles == 0
    assert entries["news"].last_records_at is None
    assert entries["gdacs"].no_record_cycles == 0


def test_a_cycle_that_cannot_count_its_records_is_not_a_record_free_one(tmp_path):
    status = tmp_path / ".collector-status.json"

    _cycles(status, "usgs", None, count=collector.NO_RECORD_CYCLE_TOLERANCE + 3)
    payload = json.loads(status.read_text(encoding="utf-8"))
    entry = _entries(_health(status))["usgs"]

    assert payload["records"] == {}
    assert entry.state == "fresh"
    assert entry.no_record_cycles == 0


def test_a_batch_that_states_no_count_reports_none():
    assert collector._received_count(SimpleNamespace(received=4)) == 4
    assert collector._received_count(SimpleNamespace(received=0)) == 0
    assert collector._received_count(SimpleNamespace()) is None
    assert collector._received_count(SimpleNamespace(received=True)) is None
    assert collector._received_count(SimpleNamespace(received=-1)) is None


def test_the_heartbeat_stays_bounded_as_record_free_cycles_accumulate(tmp_path):
    status = tmp_path / ".collector-status.json"

    _cycles(status, "usgs", 0, count=4)
    early = status.stat().st_size
    _cycles(status, "usgs", 0, count=200)
    payload = json.loads(status.read_text(encoding="utf-8"))

    # A heartbeat is a running state, not a time series: one entry per key that
    # recorded a cycle, never one per cycle.
    assert set(payload) == {"version", "groups", "records"}
    assert set(payload["records"]) == {"usgs"}
    assert payload["records"]["usgs"]["no_record_cycles"] == 204
    assert status.stat().st_size <= early + 8
    assert collector._fold_delivery(
        {"no_record_cycles": collector.MAX_NO_RECORD_CYCLES}, 0, FRESH_AT
    ) == {
        "last_records_at": None,
        "no_record_cycles": collector.MAX_NO_RECORD_CYCLES,
    }


def test_a_record_free_run_is_logged_once_per_transition(tmp_path, capsys):
    status = tmp_path / ".collector-status.json"
    _cycles(status, "usgs", 0, count=collector.NO_RECORD_CYCLE_TOLERANCE)

    first = collector.log_stale_collection_transitions(_health(status))
    repeated = collector.log_stale_collection_transitions(_health(status))
    error = capsys.readouterr().err

    assert first == (
        "[collector:health] no records parsed for group usgs in "
        f"{collector.NO_RECORD_CYCLE_TOLERANCE} consecutive completed cycles.",
    )
    assert repeated == ()
    assert error.count("[collector:health]") == 1

    _cycles(status, "usgs", 6)
    recovery = collector.log_stale_collection_transitions(_health(status))

    assert recovery == ("[collector:health] records parsed again for usgs.",)
    assert collector._LOGGED_CYCLE_STATES == {}


def test_a_stale_key_whose_cycles_resume_empty_reports_the_state_it_moved_to(tmp_path):
    status = tmp_path / ".collector-status.json"
    _write_status(status, {"news": STALE_AT})
    collector.log_stale_collection_transitions(_health(status))

    _cycles(status, "news", 0, count=collector.NO_RECORD_CYCLE_TOLERANCE)
    lines = collector.log_stale_collection_transitions(_health(status))

    assert collector._LOGGED_CYCLE_STATES == {"news": "no-records"}
    assert len(lines) == 1
    assert "no records parsed for group news" in lines[0]
    # Cycles resumed while nothing was captured, so this is not a recovery.
    assert "again" not in lines[0]


def test_the_news_cycle_records_what_the_whole_cycle_parsed(tmp_path):
    status = tmp_path / ".collector-status.json"
    feeds = [("un_press", ["a", "b", "c"]), ("bbc_world", [])]

    def processor(leaves):
        return SimpleNamespace(
            received=len(leaves), new=0, known=len(leaves), revision=0, invalid=0,
        )

    with patch("core.collector.collector_status_path", return_value=status), \
         patch("core.collector.time.time", return_value=FRESH_AT), \
         patch("ingestion.news_feed.iter_feed_batches", return_value=iter(feeds)):
        collector.ingest_thread_news(_OneCycleStop(), interval=0, processor=processor)

    recorded = json.loads(status.read_text(encoding="utf-8"))["records"]

    assert recorded["un_press"] == {"last_records_at": FRESH_AT, "no_record_cycles": 0}
    assert recorded["bbc_world"] == {"last_records_at": None, "no_record_cycles": 1}
    # One feed delivering keeps the aggregate key's own count at zero, which is
    # why the per-source counts are read as well.
    assert recorded["news"] == {"last_records_at": FRESH_AT, "no_record_cycles": 0}


def test_an_uncounted_news_cycle_leaves_the_aggregate_count_alone(tmp_path):
    status = tmp_path / ".collector-status.json"
    batch = SimpleNamespace(received=None, new=0, known=0, revision=0, invalid=0)

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

    payload = json.loads(status.read_text(encoding="utf-8"))
    entries = _entries(_health(status))

    assert payload["records"] == {}
    assert payload["groups"]["news"] == FRESH_AT
    assert entries["news"].state == "fresh"
    assert entries["un_press"].state == "fresh"
