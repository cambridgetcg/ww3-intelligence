import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from core.db import Event, Actor, RawLeaf, database, init_db
from core.stream import StreamWriter, StreamReader

def test_event_create():
    e = Event.create(
        title="Synthetic Seismic M4.3",
        date="2026-03-03",
        location_name="Test Region",
        category="seismic",
        classification="speculation",
        significance=8,
        summary="Synthetic M4.3 seismic event",
    )
    assert Event.get_by_id(e.id).title == "Synthetic Seismic M4.3"

def test_actor_create():
    a = Actor.create(name="Testland", type="state", country="TL", capability_tier=3)
    assert Actor.get_by_id(a.id).name == "Testland"

def test_raw_leaf_unprocessed_by_default():
    leaf = RawLeaf.create(
        source="usgs", type="seismic", raw_text='{"mag": 4.3}',
    )
    assert leaf.processed is False


def test_init_db_hardens_sqlite_permissions():
    path = Path(database.database)
    path.chmod(0o644)

    init_db()

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_init_db_rejects_newer_schema_version():
    database.execute_sql("PRAGMA user_version = 999")
    try:
        with pytest.raises(RuntimeError, match="newer than supported"):
            init_db()
    finally:
        database.execute_sql("PRAGMA user_version = 2")


def test_init_db_does_not_repeat_completed_schema_backfill():
    database.execute_sql("PRAGMA user_version = 2")

    with (
        patch("core.db._backfill_analysis_keys") as backfill,
        patch("core.db._migrate_prediction_horizon") as horizon_check,
    ):
        init_db()

    backfill.assert_not_called()
    horizon_check.assert_called_once_with()

def test_stream_write_and_read(tmp_path):
    path = tmp_path / "test.jsonl"
    writer = StreamWriter(path)
    writer.write({"type": "seismic", "mag": 4.3})
    writer.write({"type": "news", "headline": "Synthetic quake"})
    reader = StreamReader(path)
    records = list(reader.read_unprocessed())
    assert len(records) == 2
    assert records[0]["type"] == "seismic"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

def test_stream_mark_processed(tmp_path):
    path = tmp_path / "test.jsonl"
    writer = StreamWriter(path)
    writer.write({"id": "abc", "type": "news"})
    reader = StreamReader(path)
    reader.mark_processed("abc")
    records = list(reader.read_unprocessed())
    assert len(records) == 0

def test_stream_write_returns_id(tmp_path):
    path = tmp_path / "test.jsonl"
    writer = StreamWriter(path)
    record_id = writer.write({"type": "seismic"})
    assert isinstance(record_id, str)
    assert len(record_id) > 0

def test_stream_auto_assigns_id_and_ts(tmp_path):
    path = tmp_path / "test.jsonl"
    writer = StreamWriter(path)
    writer.write({"type": "news"})
    reader = StreamReader(path)
    records = list(reader.read_all())
    assert "id" in records[0]
    assert "ts" in records[0]
    assert records[0]["processed"] is False
