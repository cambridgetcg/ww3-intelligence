import json
import threading
from unittest.mock import patch

from core import collector


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
