import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from analysis.predictions import resolve_prediction
from core.db import (
    Event, Actor, RawLeaf, database, init_db,
    install_observation_guards, remove_observation_guards,
)
from core.observations import get_or_create_observation
from core.stream import StreamWriter, StreamReader


# The observation table in the shape a prototype from before revision links
# would hold (no such code survives; this is v0.1.0's table without
# supersedes_id), and the prediction table as v0.1.0 (2026-07-17) wrote it,
# before forecast provenance.
LEGACY_OBSERVATION_TABLE = """
CREATE TABLE "observation" (
    "id" INTEGER NOT NULL PRIMARY KEY,
    "dedupe_key" VARCHAR(255) NOT NULL,
    "source" VARCHAR(255) NOT NULL,
    "source_record_id" TEXT,
    "type" VARCHAR(255) NOT NULL,
    "raw_text" TEXT NOT NULL,
    "content_hash" VARCHAR(255) NOT NULL,
    "canonical_url" TEXT,
    "published_at" DATETIME,
    "fetched_at" DATETIME NOT NULL,
    "geo_lat" REAL,
    "geo_lon" REAL,
    "location_name" VARCHAR(255),
    "source_confidence" VARCHAR(255) NOT NULL,
    "synthetic" INTEGER NOT NULL,
    "created_at" DATETIME NOT NULL
)
"""
LEGACY_PREDICTION_TABLE = """
CREATE TABLE "prediction" (
    "id" INTEGER NOT NULL PRIMARY KEY,
    "event_id" INTEGER NOT NULL,
    "scenario_node_id" INTEGER,
    "probability" INTEGER NOT NULL,
    "confidence" VARCHAR(255) NOT NULL,
    "timeframe" VARCHAR(255),
    "horizon" VARCHAR(255),
    "reasoning" TEXT NOT NULL,
    "created_at" DATETIME NOT NULL,
    "resolved_at" DATETIME,
    "outcome" TEXT,
    "brier_score" REAL,
    FOREIGN KEY ("event_id") REFERENCES "event" ("id"),
    FOREIGN KEY ("scenario_node_id") REFERENCES "scenarionode" ("id")
)
"""
OBSERVATION_GUARDS = {
    "observation_no_update", "observation_no_delete", "observation_no_replace",
}


def _observation_triggers():
    return {
        row[0] for row in database.execute_sql(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'trigger' AND tbl_name = 'observation'"
        )
    }

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


def test_init_db_indexes_revision_links_added_to_a_legacy_observation_table():
    # SQLite cannot drop a column named in a foreign key, so the table is
    # rebuilt in the shape a prototype ledger would hold, with rows already
    # in it.
    database.execute_sql("DROP TABLE observation")
    database.execute_sql(LEGACY_OBSERVATION_TABLE)
    database.execute_sql(
        'CREATE UNIQUE INDEX "observation_dedupe_key" ON "observation" ("dedupe_key")'
    )
    for number in (1, 2, 3):
        database.execute_sql(
            "INSERT INTO observation (dedupe_key, source, type, raw_text, "
            "content_hash, fetched_at, source_confidence, synthetic, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"legacy-{number}", "example-feed", "news", f"legacy body {number}",
             f"legacy-hash-{number}", "2026-06-01 00:00:00", "not-assessed", 0,
             "2026-06-01 00:00:00"),
        )
    database.execute_sql("PRAGMA user_version = 0")

    init_db()

    assert database.execute_sql("PRAGMA integrity_check").fetchall() == [("ok",)]
    indexed_unlinked = database.execute_sql(
        "SELECT COUNT(*) FROM observation INDEXED BY observation_supersedes_id "
        "WHERE supersedes_id IS NULL"
    ).fetchone()[0]
    assert indexed_unlinked == 3


def test_init_db_indexes_forecast_provenance_added_to_a_legacy_prediction_table():
    event = Event.create(
        title="Legacy", date="2026-07-20", category="political",
        classification="reported", significance=3,
    )
    database.execute_sql("DROP TABLE prediction")
    database.execute_sql(LEGACY_PREDICTION_TABLE)
    database.execute_sql(
        'CREATE INDEX "prediction_event_id" ON "prediction" ("event_id")'
    )
    database.execute_sql(
        'CREATE INDEX "prediction_scenario_node_id" ON "prediction" ("scenario_node_id")'
    )
    for probability in (40, 70):
        database.execute_sql(
            "INSERT INTO prediction "
            "(event_id, probability, confidence, reasoning, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (event.id, probability, "medium", "Legacy forecast",
             "2026-07-20 00:00:00"),
        )
    legacy_id = database.execute_sql("SELECT MIN(id) FROM prediction").fetchone()[0]

    init_db()

    assert database.execute_sql("PRAGMA integrity_check").fetchall() == [("ok",)]
    indexed_unlinked = database.execute_sql(
        "SELECT COUNT(*) FROM prediction INDEXED BY prediction_analysis_run_id "
        "WHERE analysis_run_id IS NULL"
    ).fetchone()[0]
    assert indexed_unlinked == 2
    # Saving rewrites every column, so a stale index entry fails the update
    # as a malformed database image.
    resolved = resolve_prediction(legacy_id, outcome="Did not occur", occurred=False)
    assert resolved.brier_score is not None


def test_init_db_adds_the_replace_guard_to_a_ledger_that_lacks_it():
    stored, _ = get_or_create_observation({
        "source": "example-feed", "type": "news", "raw_text": "stored body",
    })
    # A ledger written before the guard existed holds only the update and
    # delete triggers.
    database.execute_sql("DROP TRIGGER IF EXISTS observation_no_replace")

    init_db()

    assert _observation_triggers() == OBSERVATION_GUARDS
    assert database.execute_sql("PRAGMA integrity_check").fetchall() == [("ok",)]
    added, created = get_or_create_observation({
        "source": "example-feed", "type": "news", "raw_text": "a later body",
    })
    assert created is True
    assert added.id == stored.id + 1


def test_remove_observation_guards_lifts_the_replace_guard_too():
    # Controlled teardown, conftest's included, lifts every append-only guard
    # before it drops the schema; installing them again restores all three.
    remove_observation_guards()
    left_behind = _observation_triggers()
    install_observation_guards()

    assert left_behind == set()
    assert _observation_triggers() == OBSERVATION_GUARDS

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
