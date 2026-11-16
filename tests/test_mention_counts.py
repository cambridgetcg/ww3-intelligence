"""Counts-only actor-mention report over one ledger file, read-only."""

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli.commands import cli
from core.db import Observation, SCHEMA_VERSION, database
from core.observations import get_or_create_observation
from core.registry import (
    ALIAS_FORMS,
    actor_profiles,
    registry_artifact_sha256,
    source_profiles,
)
from interpretation import mention_counts
from interpretation.actors import ACTOR_MENTION_VERSION, MATCH_MODES
from interpretation.casefile import build_casefile
from interpretation.mention_counts import (
    COUNTED_IDENTITY_STATUS,
    CSV_COLUMNS,
    DAY_BASIS_FETCHED,
    DAY_BASIS_PUBLISHED,
    MENTION_COUNTS_VERSION,
    REQUIRED_COLUMNS,
    REVISIONS_ALL,
    REVISIONS_NEWEST,
    SCOPE,
    SOURCE_IDENTITY_STATUSES,
    LedgerReadError,
    count_actor_mentions,
    resolve_ledger_path,
    write_rows_csv,
)


_SOURCE_URLS = {
    "un_press": "https://press.un.org/en/example",
    "bbc_world": "https://www.bbc.co.uk/news/example",
    "aljazeera": "https://www.aljazeera.com/news/example",
    "white_house": "https://www.whitehouse.gov/briefings/example",
    "dw_world": "https://www.dw.com/en/example",
}
_FETCHED = "2026-11-14T08:00:00Z"
_NOW = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc)
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _observation(source, record_id, title, summary, **leaf):
    payload = {
        "source": source,
        "source_record_id": record_id,
        "type": "news",
        "canonical_url": _SOURCE_URLS.get(source, "https://example.test/report"),
        "fetched_at": _FETCHED,
        "raw_text": json.dumps({"title": title, "summary": summary}),
    }
    payload.update(leaf)
    return get_or_create_observation(payload)[0]


def _ledger_file(tmp_path, name="ledger.db"):
    # The test database runs in WAL mode, so its main file alone may lack the
    # latest writes. VACUUM INTO writes one complete rollback-mode file.
    folder = tmp_path / "ledger"
    folder.mkdir(exist_ok=True)
    target = folder / name
    database.execute_sql("VACUUM INTO ?", (str(target),))
    return target


def _wal_marked_file(tmp_path, folder_name="walmarked"):
    # A page-for-page copy keeps the source header, so the file says it is
    # write-ahead-logged while no log sits beside it: the shape an archived
    # copy of the live ledger has.
    folder = tmp_path / folder_name
    folder.mkdir()
    target = folder / "copy.db"
    destination = sqlite3.connect(target)
    try:
        database.connection().backup(destination)
    finally:
        destination.close()
    header = target.read_bytes()[:100]
    assert (header[18], header[19]) == (2, 2)
    assert sorted(os.listdir(folder)) == ["copy.db"]
    return target


def _state(path):
    path = Path(path)
    info = path.stat()
    folder = path.parent
    return {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "mode": info.st_mode,
        "listing": sorted(os.listdir(folder)),
        "dir_mtime_ns": folder.stat().st_mtime_ns,
    }


def _rows(payload):
    return [
        (row["day"], row["source_id"], row["actor_id"],
         row["observations_mentioning"], row["spans"], row["publisher_self_mention"],
         row["observations_counted"])
        for row in payload["rows"]
    ]


def _by_actor(payload):
    return {item["actor_id"]: item for item in payload["totals"]["by_actor"]}


def _by_source(payload):
    return {item["source_id"]: item for item in payload["totals"]["by_source"]}


def _strings(value, keys=None):
    """Yield every string value in a JSON-safe structure, and collect keys."""
    if isinstance(value, dict):
        for key, item in value.items():
            if keys is not None:
                keys.add(key)
            yield from _strings(item, keys)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item, keys)
    elif isinstance(value, str):
        yield value


class _Sql(str):
    """A literal SQL expression for `_build_hand_ledger`."""


def _build_hand_ledger(path, rows, *, columns=REQUIRED_COLUMNS, user_version=None):
    """Write a minimal observation table by hand, for shapes the ORM cannot store."""
    connection = sqlite3.connect(path)
    try:
        connection.execute(f"CREATE TABLE observation ({', '.join(sorted(columns))})")
        for row in rows:
            names = sorted(row)
            values = ", ".join(
                row[name] if isinstance(row[name], _Sql) else "?" for name in names
            )
            connection.execute(
                f"INSERT INTO observation ({', '.join(names)}) VALUES ({values})",
                [row[name] for name in names if not isinstance(row[name], _Sql)],
            )
        if user_version is not None:
            connection.execute(f"PRAGMA user_version = {int(user_version)}")
        connection.commit()
    finally:
        connection.close()
    return path


def _hand_row(row_id, **overrides):
    row = {
        "id": row_id,
        "dedupe_key": f"key-{row_id}",
        "source": "un_press",
        "source_record_id": f"r{row_id}",
        "raw_text": json.dumps({"title": "United Nations"}),
        "canonical_url": _SOURCE_URLS["un_press"],
        "published_at": "2026-11-01 10:00:00",
        "fetched_at": "2026-11-01 11:00:00",
        "synthetic": 0,
        "supersedes_id": None,
        "created_at": f"2026-11-01 11:00:{row_id:02d}",
    }
    row.update(overrides)
    return row


# 1. Rows by UTC day, source and actor.

def test_rows_are_counted_per_utc_day_source_and_actor(tmp_path):
    _observation(
        "un_press", "u1", "United Nations session",
        "China and the United States spoke. China again.",
        published_at="2026-11-11T01:30:00+03:00",
    )
    _observation(
        "bbc_world", "b1", "China responds", "No other actor here.",
        published_at="2026-11-12T09:00:00Z",
    )
    _observation(
        "bbc_world", "b2", "Quiet day", "Nothing registered is named.",
        published_at="2026-11-12T10:00:00Z",
    )

    payload = count_actor_mentions(_ledger_file(tmp_path), now=_NOW)

    assert payload["contract"] == MENTION_COUNTS_VERSION
    assert _rows(payload) == [
        ("2026-11-10", "un_press", "org:un", 1, 1, True, 1),
        ("2026-11-10", "un_press", "state:china", 1, 2, False, 1),
        ("2026-11-10", "un_press", "state:us", 1, 1, False, 1),
        ("2026-11-12", "bbc_world", "state:china", 1, 1, False, 2),
    ]
    keys = [(row["day"], row["source_id"], row["actor_id"]) for row in payload["rows"]]
    assert keys == sorted(keys)
    assert payload["days"] == {"first": "2026-11-10", "last": "2026-11-12", "distinct": 2}
    assert payload["totals"]["by_day_source"] == [
        {"day": "2026-11-10", "source_id": "un_press", "observations_counted": 1,
         "observations_with_mentions": 1, "spans": 4},
        {"day": "2026-11-12", "source_id": "bbc_world", "observations_counted": 2,
         "observations_with_mentions": 1, "spans": 1},
    ]


# 2. Day basis and the fetched_at fallback.

def test_day_falls_back_to_fetch_time_and_can_use_fetch_time_only(tmp_path):
    _observation(
        "un_press", "u1", "China", "Published record.",
        published_at="2026-11-11T01:30:00+03:00",
    )
    _observation(
        "bbc_world", "b1", "China", "Undated by its publisher.",
        fetched_at="2026-11-12T23:59:59Z",
    )
    ledger = _ledger_file(tmp_path)

    published = count_actor_mentions(ledger, now=_NOW)
    assert published["filters"]["day_basis"] == DAY_BASIS_PUBLISHED
    assert published["day_basis"] == {"published_at": 1, "fetched_at": 1, "undated": 0}
    assert [(row["day"], row["source_id"]) for row in published["rows"]] == [
        ("2026-11-10", "un_press"), ("2026-11-12", "bbc_world"),
    ]

    fetched = count_actor_mentions(ledger, day_basis=DAY_BASIS_FETCHED, now=_NOW)
    assert fetched["filters"]["day_basis"] == DAY_BASIS_FETCHED
    assert fetched["day_basis"] == {"published_at": 0, "fetched_at": 2, "undated": 0}
    assert [(row["day"], row["source_id"]) for row in fetched["rows"]] == [
        ("2026-11-12", "bbc_world"), ("2026-11-14", "un_press"),
    ]

    with pytest.raises(ValueError):
        count_actor_mentions(ledger, day_basis="published")


def test_undated_rows_and_rows_without_a_record_id_are_counted_in_the_selection(tmp_path):
    ledger = _build_hand_ledger(tmp_path / "hand.db", [
        _hand_row(1),
        _hand_row(2, published_at=None, fetched_at=None),
        _hand_row(3, published_at="not a time", fetched_at="2026-11-02 08:00:00.123456"),
        # Two rows with no source record id are two records, not revisions.
        _hand_row(4, source_record_id=None),
        _hand_row(5, source_record_id=None, dedupe_key="key-5b"),
    ])

    payload = count_actor_mentions(ledger, now=_NOW)

    selection = payload["selection"]
    assert selection["observations_stored"] == 5
    assert selection["source_records"] == 5
    assert selection["revisions_superseded"] == 0
    assert selection["observations_undated"] == 1
    assert selection["observations_counted"] == 4
    assert payload["day_basis"] == {"published_at": 3, "fetched_at": 1, "undated": 1}
    assert payload["days"] == {"first": "2026-11-01", "last": "2026-11-02", "distinct": 2}
    assert payload["ledger"]["schema_version"] == 0
    assert payload["ledger"]["first_fetched_at"] == "2026-11-01T11:00:00Z"
    assert payload["ledger"]["last_fetched_at"] == "2026-11-02T08:00:00Z"


# 3. Revisions.

def test_newest_revision_is_the_default_and_every_revision_is_a_flag(tmp_path):
    _observation("bbc_world", "b1", "China", "China said so.", published_at="2026-11-12T09:00:00Z")
    newer = _observation(
        "bbc_world", "b1", "Russia", "Revised text.", published_at="2026-11-12T09:00:00Z",
    )
    assert newer.supersedes_id is not None
    ledger = _ledger_file(tmp_path)

    newest = count_actor_mentions(ledger, now=_NOW)
    assert newest["filters"]["revisions"] == REVISIONS_NEWEST
    assert newest["selection"]["revisions_superseded"] == 1
    assert newest["selection"]["observations_selected"] == 1
    assert newest["selection"]["revision_link_disagreements"] == 0
    assert [(row["actor_id"], row["spans"]) for row in newest["rows"]] == [("state:russia", 1)]

    every = count_actor_mentions(ledger, all_revisions=True, now=_NOW)
    assert every["filters"]["revisions"] == REVISIONS_ALL
    assert every["selection"]["observations_selected"] == 2
    assert every["selection"]["revision_link_disagreements"] == 0
    assert [(row["actor_id"], row["spans"]) for row in every["rows"]] == [
        ("state:china", 2), ("state:russia", 1),
    ]


def test_revision_links_that_disagree_with_the_chosen_order_are_counted(tmp_path):
    ledger = _build_hand_ledger(tmp_path / "hand.db", [
        # The older row claims to supersede the newer one. The newest row is
        # then superseded and the older one is not, so both rows disagree.
        _hand_row(1, source_record_id="same", supersedes_id=2),
        _hand_row(2, source_record_id="same"),
    ])

    payload = count_actor_mentions(ledger, now=_NOW)

    assert payload["selection"]["revision_link_disagreements"] == 2
    assert payload["selection"]["observations_selected"] == 1


# 4. Source identity.

def test_only_host_consistent_records_are_counted(tmp_path):
    _observation("un_press", "u1", "China", "Consistent.")
    _observation(
        "white_house", "w1", "China", "Wrong host.",
        canonical_url="https://example.org/briefings/w1",
    )
    _observation(
        "bbc_world", "b1", "China", "Plain HTTP.",
        canonical_url="http://www.bbc.co.uk/news/b1",
    )
    _observation("dw_world", "d1", "China", "No URL.", canonical_url=None)
    _observation(
        "brave_search", "s1", "China", "Search result.",
        canonical_url="https://example.net/s1",
    )
    _observation("un_press", "u2", "China", "Fixture.", synthetic=True)
    _observation(
        "zz_private_feed", "z1", "China", "Unregistered.",
        canonical_url="https://private.example/z1",
    )

    payload = count_actor_mentions(_ledger_file(tmp_path), now=_NOW)

    identity = payload["source_identity"]
    assert identity["counted_status"] == COUNTED_IDENTITY_STATUS
    assert set(identity["observations_by_status"]) == set(SOURCE_IDENTITY_STATUSES)
    assert identity["observations_by_status"] == {
        "canonical-host-consistent": 1,
        "canonical-host-match-insecure-url": 1,
        "canonical-host-mismatch": 1,
        "canonical-host-unverifiable": 1,
        "canonical-url-missing-or-invalid": 1,
        "synthetic-fixture": 1,
        "unregistered-source-id": 1,
    }
    assert identity["excluded"] == [
        {"source_id": "bbc_world", "status": "canonical-host-match-insecure-url",
         "observations": 1},
        {"source_id": "brave_search", "status": "canonical-host-unverifiable",
         "observations": 1},
        {"source_id": "dw_world", "status": "canonical-url-missing-or-invalid",
         "observations": 1},
        {"source_id": "un_press", "status": "synthetic-fixture", "observations": 1},
        {"source_id": "white_house", "status": "canonical-host-mismatch",
         "observations": 1},
        {"source_id": None, "status": "unregistered-source-id", "observations": 1},
    ]
    assert identity["unregistered_source_ids"] == 1
    assert payload["selection"]["observations_excluded_by_source_identity"] == 6
    assert payload["selection"]["observations_counted"] == 1
    assert {row["source_id"] for row in payload["rows"]} == {"un_press"}
    assert [item["source_id"] for item in payload["totals"]["by_source"]] == ["un_press"]
    text = json.dumps(payload)
    assert "zz_private_feed" not in text
    assert "private.example" not in text


# 5. Publisher self-mentions.

def test_publisher_self_mentions_are_flagged_narrowly(tmp_path):
    _observation("un_press", "u1", "United Nations", "The United Nations met.")
    _observation("bbc_world", "b1", "United Nations", "Reported by the BBC.")
    _observation("white_house", "w1", "United States", "The United States said.")

    payload = count_actor_mentions(_ledger_file(tmp_path), now=_NOW)

    flags = {
        (row["source_id"], row["actor_id"]): row["publisher_self_mention"]
        for row in payload["rows"]
    }
    assert flags == {
        ("bbc_world", "org:un"): False,
        ("un_press", "org:un"): True,
        ("white_house", "state:us"): False,
    }
    un = _by_actor(payload)["org:un"]
    assert (un["observations_mentioning"], un["spans"]) == (2, 3)
    assert (un["publisher_self_observations"], un["publisher_self_spans"]) == (1, 2)
    us = _by_actor(payload)["state:us"]
    assert (us["publisher_self_observations"], us["publisher_self_spans"]) == (0, 0)
    sources = _by_source(payload)
    assert sources["un_press"]["publisher_actor_id"] == "org:un"
    assert sources["un_press"]["publisher_self_observations"] == 1
    assert sources["un_press"]["publisher_self_spans"] == 2
    assert sources["white_house"]["publisher_actor_id"] == "org:white-house"
    assert sources["white_house"]["publisher_self_spans"] == 0
    assert payload["filters"]["publisher_self_mentions"] == "counted-and-flagged"


# 6. Truncation.

def test_truncated_scans_and_passages_are_reported(tmp_path, monkeypatch):
    flood = _observation("bbc_world", "b1", "China " * 5000, "")
    long_text = _observation("bbc_world", "b2", "lorem ipsum " * 3000, "")
    plain = _observation("bbc_world", "b3", "China", "Short.")

    payload = count_actor_mentions(_ledger_file(tmp_path), now=_NOW)

    truncation = payload["scan"]["truncation"]
    assert truncation["complete"] is False
    assert truncation["scans_truncated"] == 1
    assert truncation["scans_truncated_observation_ids"] == [flood.id]
    assert truncation["passages_truncated"] == 2
    assert truncation["passages_truncated_observation_ids"] == sorted([flood.id, long_text.id])
    assert plain.id not in truncation["passages_truncated_observation_ids"]
    assert truncation["ids_omitted"] == 0

    monkeypatch.setattr(mention_counts, "MAX_LISTED_TRUNCATED_IDS", 1)
    bounded = count_actor_mentions(_ledger_file(tmp_path, "again.db"), now=_NOW)
    truncation = bounded["scan"]["truncation"]
    assert truncation["ids_listed_at_most"] == 1
    assert truncation["scans_truncated_observation_ids"] == [flood.id]
    assert truncation["passages_truncated_observation_ids"] == [min(flood.id, long_text.id)]
    assert truncation["passages_truncated"] == 2
    assert truncation["ids_omitted"] == 1


def test_a_short_ledger_is_complete(tmp_path):
    _observation("bbc_world", "b1", "China", "Short.")
    truncation = count_actor_mentions(_ledger_file(tmp_path), now=_NOW)["scan"]["truncation"]
    assert truncation["complete"] is True
    assert truncation["scans_truncated_observation_ids"] == []


# 7. Agreement with a casefile.

def test_counts_agree_with_a_casefile_over_the_same_records(tmp_path):
    observations = [
        _observation(
            "un_press", "u1", "United Nations meeting",
            "The United States and China addressed the Council.",
        ),
        _observation("bbc_world", "b1", "China responds", "China responded to the US."),
        _observation("bbc_world", "b1", "Russia responds", "Russia and Iran."),
        _observation("aljazeera", "a1", "Moscow and Beijing", "Россия и 中国."),
        _observation("white_house", "w1", "White House", "The White House said."),
        _observation("dw_world", "d1", "China " * 5000, ""),
    ]
    casefile = build_casefile(observations)
    payload = count_actor_mentions(_ledger_file(tmp_path), all_revisions=True, now=_NOW)

    by_actor = _by_actor(payload)
    visibility = {item["actor"]["actor_id"]: item for item in casefile["actor_visibility"]}
    assert visibility
    for actor_id, item in visibility.items():
        assert by_actor[actor_id]["spans"] == item["mention_count"], actor_id
        assert by_actor[actor_id]["observations_mentioning"] == len(item["observation_ids"])
    for actor_id, item in by_actor.items():
        if actor_id not in visibility:
            assert item["spans"] == 0, actor_id
    total_spans = sum(len(record["actor_mentions"]) for record in casefile["observations"])
    assert payload["scan"]["mentions_emitted"] == total_spans
    assert sum(row["spans"] for row in payload["rows"]) == total_spans
    assert (
        payload["scan"]["truncation"]["scans_truncated_observation_ids"]
        == casefile["actor_visibility_status"]["truncated_observation_ids"]
    )
    assert casefile["actor_visibility_status"]["truncated_observation_ids"]
    for name, value in payload["contracts"].items():
        assert casefile["contracts"][name] == value, name
    assert payload["registry_artifacts_sha256"] == casefile["registry_artifacts_sha256"]


# 8. No publisher text.

def _sentinel_ledger(tmp_path):
    observations = [
        _observation(
            "bbc_world", "recsentinel-1", "Zanzibarquux headline about cHiNa",
            "Plumbusflorp summary naming the United Nations and Russia.",
            published_at="2026-11-12T09:00:00Z",
        ),
        _observation(
            "un_press", "recsentinel-2", "Quorblax title",
            "The United Nations and cHiNa again.",
            published_at="2026-11-11T09:00:00Z",
        ),
        _observation(
            "zz_private_feed", "recsentinel-3", "Snorkelwump", "cHiNa",
            canonical_url="https://private.example/z1",
        ),
    ]
    return _ledger_file(tmp_path), [item.dedupe_key for item in observations]


_SENTINELS = (
    "Zanzibarquux", "Plumbusflorp", "Quorblax", "Snorkelwump", "cHiNa", "headline",
    "summary naming", "zz_private_feed", "private.example", "press.un.org", "bbc.co.uk",
    "recsentinel", "United Nations", "Russia.",
)


def test_output_holds_no_publisher_text(tmp_path):
    ledger, dedupe_keys = _sentinel_ledger(tmp_path)
    payload = count_actor_mentions(ledger, now=_NOW)
    assert payload["scan"]["mentions_emitted"] > 0

    outputs = [json.dumps(payload, ensure_ascii=False)]
    handle = io.StringIO()
    write_rows_csv(payload, handle)
    outputs.append(handle.getvalue())
    human = CliRunner().invoke(cli, ["mentions", str(ledger)])
    assert human.exit_code == 0, human.output
    outputs.append(human.output)
    as_json = CliRunner().invoke(cli, ["mentions", str(ledger), "--json"])
    assert as_json.exit_code == 0, as_json.output
    outputs.append(as_json.output)
    for output in outputs:
        for sentinel in (*_SENTINELS, *dedupe_keys):
            assert sentinel not in output, sentinel
        assert "China" not in output.replace("state:china", "")

    actor_ids = {profile.actor_id for profile in actor_profiles()}
    source_ids = {profile.source_id for profile in source_profiles()}
    constants = {
        MENTION_COUNTS_VERSION, ACTOR_MENTION_VERSION, SCOPE, COUNTED_IDENTITY_STATUS,
        REVISIONS_NEWEST, REVISIONS_ALL, DAY_BASIS_PUBLISHED, DAY_BASIS_FETCHED,
        "canonical-host-consistent-only", "counted-and-flagged", ledger.name,
        *SOURCE_IDENTITY_STATUSES, *payload["contracts"].values(),
    }
    keys = set()
    for value in _strings(payload, keys):
        assert (
            value in constants or value in actor_ids or value in source_ids
            or _DAY.match(value) or _SHA256.match(value) or _ISO_Z.match(value)
        ), value
    field = re.compile(r"^[a-z][a-z0-9_]*$")
    for key in keys:
        assert (
            field.match(key) or key in MATCH_MODES or key in ALIAS_FORMS
            or key in SOURCE_IDENTITY_STATUSES
        ), key


# 9. The file and its directory are unchanged.

def test_reading_leaves_the_file_and_its_directory_unchanged(tmp_path):
    _observation("un_press", "u1", "China", "Text.", published_at="2026-11-12T09:00:00Z")
    ledger = _ledger_file(tmp_path)
    outside = tmp_path / "out"
    outside.mkdir()
    before = _state(ledger)

    count_actor_mentions(ledger, now=_NOW)
    count_actor_mentions(ledger, all_revisions=True, day_basis=DAY_BASIS_FETCHED, now=_NOW)
    runner = CliRunner()
    for arguments in (
        ["mentions", str(ledger), "--json"],
        ["mentions", str(ledger), "--csv", str(outside / "rows.csv")],
        ["mentions", str(ledger)],
    ):
        result = runner.invoke(cli, arguments)
        assert result.exit_code == 0, result.output

    assert _state(ledger) == before
    assert (outside / "rows.csv").exists()


# 10. A read-only file in a read-only directory.

@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores file permissions",
)
@pytest.mark.parametrize("make", [_ledger_file, _wal_marked_file], ids=["rollback", "wal"])
def test_a_read_only_file_in_a_read_only_directory_is_read(tmp_path, make):
    _observation("un_press", "u1", "China", "Text.")
    ledger = make(tmp_path)
    folder = ledger.parent
    os.chmod(ledger, 0o400)
    os.chmod(folder, 0o500)
    try:
        before = _state(ledger)
        payload = count_actor_mentions(ledger, now=_NOW)
        assert payload["selection"]["observations_counted"] == 1
        assert _state(ledger) == before
    finally:
        os.chmod(folder, 0o700)
        os.chmod(ledger, 0o600)


# 11. A missing path is refused and nothing is created.

def test_a_missing_path_is_refused_and_nothing_is_created(tmp_path):
    missing = tmp_path / "absent.db"
    listing = sorted(os.listdir(tmp_path))

    for call in (resolve_ledger_path, count_actor_mentions):
        with pytest.raises(LedgerReadError) as caught:
            call(missing)
        assert caught.value.code == "ledger_not_found"

    assert not missing.exists()
    assert sorted(os.listdir(tmp_path)) == listing


# 12. Other refusals.

def test_a_directory_is_refused(tmp_path):
    with pytest.raises(LedgerReadError) as caught:
        count_actor_mentions(tmp_path)
    assert caught.value.code == "ledger_not_a_file"


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"", "ledger_schema_unsupported"),
        (b"this is not a database " * 200, "ledger_unreadable"),
    ],
)
def test_empty_and_non_database_files_are_refused(tmp_path, content, code):
    path = tmp_path / "file.db"
    path.write_bytes(content)
    before = _state(path)
    with pytest.raises(LedgerReadError) as caught:
        count_actor_mentions(path)
    assert caught.value.code == code
    assert _state(path) == before


def test_a_table_without_the_required_columns_is_refused(tmp_path):
    path = _build_hand_ledger(
        tmp_path / "narrow.db", [], columns=REQUIRED_COLUMNS - {"supersedes_id"},
    )
    with pytest.raises(LedgerReadError) as caught:
        count_actor_mentions(path)
    assert caught.value.code == "ledger_schema_unsupported"

    other = tmp_path / "other.db"
    connection = sqlite3.connect(other)
    connection.execute("CREATE TABLE event (id INTEGER)")
    connection.commit()
    connection.close()
    with pytest.raises(LedgerReadError) as caught:
        count_actor_mentions(other)
    assert caught.value.code == "ledger_schema_unsupported"


def test_a_newer_schema_version_is_refused(tmp_path):
    path = _build_hand_ledger(tmp_path / "newer.db", [], user_version=99)
    assert 99 > SCHEMA_VERSION
    with pytest.raises(LedgerReadError) as caught:
        count_actor_mentions(path)
    assert caught.value.code == "ledger_schema_unsupported"


def test_text_that_is_not_text_is_refused(tmp_path):
    blob = _build_hand_ledger(
        tmp_path / "blob.db", [_hand_row(1, raw_text=b"\x00\x01")],
    )
    with pytest.raises(LedgerReadError) as caught:
        count_actor_mentions(blob)
    assert caught.value.code == "ledger_schema_unsupported"

    undecodable = _build_hand_ledger(
        tmp_path / "undecodable.db", [_hand_row(1, raw_text=_Sql("CAST(X'FFFE' AS TEXT)"))],
    )
    with pytest.raises(LedgerReadError) as caught:
        count_actor_mentions(undecodable)
    assert caught.value.code == "ledger_unreadable"


# 13. A journal beside the file.

def test_a_live_write_ahead_log_is_refused():
    _observation("un_press", "u1", "China", "Text.")
    live = Path(database.database)
    wal = Path(f"{live}-wal")
    assert wal.exists() and wal.stat().st_size > 0

    with pytest.raises(LedgerReadError) as caught:
        count_actor_mentions(live)
    assert caught.value.code == "ledger_has_journal"


def test_a_non_empty_rollback_journal_is_refused_and_an_empty_log_is_not(tmp_path):
    _observation("un_press", "u1", "China", "Text.")
    ledger = _ledger_file(tmp_path)
    journal = Path(f"{ledger}-journal")
    journal.write_bytes(b"\x00" * 512)
    with pytest.raises(LedgerReadError) as caught:
        count_actor_mentions(ledger)
    assert caught.value.code == "ledger_has_journal"
    assert journal.read_bytes() == b"\x00" * 512

    journal.unlink()
    empty_wal = Path(f"{ledger}-wal")
    empty_wal.write_bytes(b"")
    before = _state(ledger)
    payload = count_actor_mentions(ledger, now=_NOW)
    assert payload["selection"]["observations_counted"] == 1
    assert _state(ledger) == before
    assert empty_wal.stat().st_size == 0


# 14. A file whose header marks it as write-ahead-logged, with no log.

def test_a_wal_marked_file_without_a_log_is_read_whole_and_gains_no_sidecar(tmp_path):
    first = _observation("un_press", "u1", "China", "Text.")
    _observation("bbc_world", "b1", "United Nations", "Text.")
    target = _wal_marked_file(tmp_path)
    folder = target.parent
    before = _state(target)

    payload = count_actor_mentions(target, now=_NOW)

    assert payload["ledger"]["observations"] == 2
    assert payload["ledger"]["max_id"] >= first.id
    assert payload["selection"]["observations_counted"] == 2
    assert _state(target) == before
    assert sorted(os.listdir(folder)) == ["copy.db"]


# 15. A file that changes while it is read.

def test_a_file_that_changes_during_the_read_is_refused(tmp_path, monkeypatch):
    _observation("un_press", "u1", "China", "Text.")
    ledger = _ledger_file(tmp_path)
    real = mention_counts._file_state
    calls = []

    def changing(path):
        calls.append(path)
        size, mtime_ns, inode, device = real(path)
        return (size, mtime_ns + len(calls) - 1, inode, device)

    monkeypatch.setattr(mention_counts, "_file_state", changing)
    with pytest.raises(LedgerReadError) as caught:
        count_actor_mentions(ledger)
    assert caught.value.code == "ledger_changed_during_read"
    assert len(calls) == 2


# 16. An empty ledger.

def test_an_empty_ledger_gives_zeros(tmp_path):
    payload = count_actor_mentions(_ledger_file(tmp_path), now=_NOW)

    assert payload["rows"] == []
    assert payload["ledger"]["observations"] == 0
    assert payload["ledger"]["max_id"] is None
    assert payload["ledger"]["first_fetched_at"] is None
    assert payload["ledger"]["schema_version"] == SCHEMA_VERSION
    assert set(payload["selection"].values()) == {0}
    assert payload["days"] == {"first": None, "last": None, "distinct": 0}
    assert payload["totals"]["by_source"] == []
    assert payload["totals"]["by_day_source"] == []
    by_actor = _by_actor(payload)
    assert set(by_actor) == {profile.actor_id for profile in actor_profiles()}
    assert all(item["spans"] == 0 for item in by_actor.values())
    assert payload["registry"] == {
        "actors": len(actor_profiles()), "sources": len(source_profiles()),
    }
    assert set(payload["scan"]["match_modes"]) == set(MATCH_MODES)
    assert set(payload["scan"]["alias_forms"]) == set(ALIAS_FORMS)
    assert payload["scan"]["truncation"]["complete"] is True


# 17. Determinism.

def test_two_runs_agree_apart_from_the_generation_time(tmp_path):
    _observation("un_press", "u1", "China", "United Nations.")
    _observation("bbc_world", "b1", "Russia", "China.")
    ledger = _ledger_file(tmp_path)

    first = count_actor_mentions(ledger, now=_NOW)
    second = count_actor_mentions(ledger)

    assert first["generated_at"] == "2026-11-16T12:00:00Z"
    assert _ISO_Z.match(second["generated_at"])
    first.pop("generated_at")
    second.pop("generated_at")
    assert first == second
    assert first["ledger"]["db_sha256"] == hashlib.sha256(ledger.read_bytes()).hexdigest()
    assert first["ledger"]["db_bytes"] == ledger.stat().st_size
    assert first["ledger"]["file_name"] == "ledger.db"
    assert first["registry_artifacts_sha256"] == registry_artifact_sha256()


def test_a_link_reports_its_target(tmp_path):
    _observation("un_press", "u1", "China", "Text.")
    ledger = _ledger_file(tmp_path)
    link = tmp_path / "latest.db"
    link.symlink_to(ledger)
    assert count_actor_mentions(link, now=_NOW)["ledger"]["file_name"] == "ledger.db"


# 18. Totals.

def test_totals_agree_with_the_rows(tmp_path):
    _observation(
        "un_press", "u1", "United Nations", "China, Russia and the United States.",
        published_at="2026-11-10T09:00:00Z",
    )
    _observation(
        "bbc_world", "b1", "China", "China and Russia.", published_at="2026-11-10T10:00:00Z",
    )
    _observation("bbc_world", "b2", "Nothing named", "Quiet.", published_at="2026-11-11T10:00:00Z")
    _observation("white_house", "w1", "White House", "Iran.", published_at="2026-11-11T11:00:00Z")
    _observation("dw_world", "d1", "China", "No URL.", canonical_url=None)

    payload = count_actor_mentions(_ledger_file(tmp_path), now=_NOW)

    spans = payload["scan"]["mentions_emitted"]
    assert sum(row["spans"] for row in payload["rows"]) == spans
    for table in ("by_actor", "by_source", "by_day_source"):
        assert sum(item["spans"] for item in payload["totals"][table]) == spans, table
    cells = payload["totals"]["by_day_source"]
    selection = payload["selection"]
    assert sum(cell["observations_counted"] for cell in cells) == selection["observations_counted"]
    assert (
        sum(cell["observations_with_mentions"] for cell in cells)
        == payload["scan"]["observations_with_mentions"]
    )
    assert selection["observations_counted"] == payload["scan"]["observations_scanned"]
    assert selection["observations_selected"] == (
        selection["observations_excluded_by_source_identity"]
        + selection["observations_undated"]
        + selection["observations_counted"]
    )
    denominators = {(cell["day"], cell["source_id"]): cell["observations_counted"] for cell in cells}
    for row in payload["rows"]:
        assert row["observations_counted"] == denominators[(row["day"], row["source_id"])]
        assert row["observations_mentioning"] <= row["observations_counted"]
    by_actor = payload["totals"]["by_actor"]
    order = [(-item["observations_mentioning"], -item["spans"], item["actor_id"]) for item in by_actor]
    assert order == sorted(order)
    assert sum(payload["scan"]["match_modes"].values()) == spans
    assert sum(payload["scan"]["alias_forms"].values()) == spans
    sources = _by_source(payload)
    assert sources["bbc_world"]["days"] == 2
    assert sources["bbc_world"]["observations_counted"] == 2
    assert sources["bbc_world"]["observations_with_mentions"] == 1


# 19-22. The command.

def test_cli_json_never_initialises_the_default_database(tmp_path):
    _observation("un_press", "u1", "China", "Text.")
    ledger = _ledger_file(tmp_path)

    with patch("cli.commands.init_db") as init:
        result = CliRunner().invoke(cli, ["mentions", str(ledger), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["contract"] == MENTION_COUNTS_VERSION
    assert payload["selection"]["observations_counted"] == 1
    init.assert_not_called()


def test_cli_creates_no_database_in_a_fresh_process(tmp_path):
    _observation("un_press", "u1", "China", "Text.")
    ledger = _ledger_file(tmp_path)
    db_path = tmp_path / "must-not-exist.db"
    stream_path = tmp_path / "must-not-exist.jsonl"
    project_root = Path(__file__).resolve().parent.parent
    script = "\n".join([
        "import json, os",
        "from pathlib import Path",
        f"os.environ['DB_PATH'] = {str(db_path)!r}",
        f"os.environ['WW3_STREAM_PATH'] = {str(stream_path)!r}",
        "os.environ['WW3_DISABLE_LLM'] = '1'",
        "from click.testing import CliRunner",
        "from cli.commands import cli",
        f"result = CliRunner().invoke(cli, ['mentions', {str(ledger)!r}, '--json'])",
        "assert result.exit_code == 0, result.output",
        f"assert json.loads(result.output)['contract'] == {MENTION_COUNTS_VERSION!r}",
        "assert not Path(os.environ['DB_PATH']).exists()",
        "assert not Path(os.environ['WW3_STREAM_PATH']).exists()",
    ])
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(project_root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": str(tmp_path),
        "LANG": "C.UTF-8",
    }
    before = _state(ledger)

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not db_path.exists()
    assert not stream_path.exists()
    assert _state(ledger) == before


def test_cli_writes_csv_rows_and_refuses_a_path_beside_the_ledger(tmp_path):
    _observation("un_press", "u1", "United Nations", "China.", published_at="2026-11-10T09:00:00Z")
    _observation("bbc_world", "b1", "China", "Russia.", published_at="2026-11-11T09:00:00Z")
    ledger = _ledger_file(tmp_path)
    outside = tmp_path / "out"
    outside.mkdir()
    target = outside / "rows.csv"
    runner = CliRunner()

    result = runner.invoke(cli, ["mentions", str(ledger), "--csv", str(target)])

    assert result.exit_code == 0, result.output
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with target.open(encoding="utf-8", newline="") as handle:
        table = list(csv.reader(handle))
    assert tuple(table[0]) == CSV_COLUMNS
    payload = count_actor_mentions(ledger, now=_NOW)
    expected = [
        [
            ("true" if row[column] else "false") if isinstance(row[column], bool)
            else str(row[column])
            for column in CSV_COLUMNS
        ]
        for row in payload["rows"]
    ]
    assert table[1:] == expected
    assert f"Wrote {len(expected)} row(s) to" in result.output

    target.write_text("old", encoding="utf-8")
    os.chmod(target, 0o644)
    again = runner.invoke(cli, ["mentions", str(ledger), "--csv", str(target)])
    assert again.exit_code == 0, again.output
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_text(encoding="utf-8").startswith(",".join(CSV_COLUMNS))

    listing = sorted(os.listdir(ledger.parent))
    beside = runner.invoke(cli, ["mentions", str(ledger), "--csv", str(ledger.parent / "x.csv")])
    assert beside.exit_code == 1
    assert "Refusing to write beside the ledger" in beside.output
    over = runner.invoke(cli, ["mentions", str(ledger), "--csv", str(ledger)])
    assert over.exit_code == 1
    missing_dir = runner.invoke(
        cli, ["mentions", str(ledger), "--csv", str(tmp_path / "nowhere" / "x.csv")],
    )
    assert missing_dir.exit_code == 1
    assert not (tmp_path / "nowhere").exists()
    assert sorted(os.listdir(ledger.parent)) == listing


def test_cli_refusals_exit_with_a_code(tmp_path):
    missing = tmp_path / "absent.db"
    runner = CliRunner()

    plain = runner.invoke(cli, ["mentions", str(missing)])
    assert plain.exit_code == 1
    assert "ledger_not_found" in plain.output

    as_json = runner.invoke(cli, ["mentions", str(missing), "--json"])
    assert as_json.exit_code == 1
    assert json.loads(as_json.output) == {"error": {"code": "ledger_not_found"}}

    folder = runner.invoke(cli, ["mentions", str(tmp_path), "--json"])
    assert folder.exit_code == 1
    assert json.loads(folder.output) == {"error": {"code": "ledger_not_a_file"}}

    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not a database " * 200)
    unreadable = runner.invoke(cli, ["mentions", str(bad), "--json"])
    assert unreadable.exit_code == 1
    assert json.loads(unreadable.output) == {"error": {"code": "ledger_unreadable"}}
    assert not missing.exists()


def test_cli_human_view_lists_actors_sources_and_scope(tmp_path):
    _observation("un_press", "u1", "United Nations", "China.", published_at="2026-11-10T09:00:00Z")
    _observation("dw_world", "d1", "China", "No URL.", canonical_url=None)
    ledger = _ledger_file(tmp_path)

    result = CliRunner().invoke(cli, ["mentions", str(ledger), "--all-revisions",
                                      "--day-basis", "fetched"])

    assert result.exit_code == 0, result.output
    output = result.output
    assert MENTION_COUNTS_VERSION in output and ACTOR_MENTION_VERSION in output
    assert "ledger.db" in output
    assert REVISIONS_ALL in output and DAY_BASIS_FETCHED in output
    assert "canonical-url-missing-or-invalid=1" in output
    assert "  org:un: 1 obs · 1 spans · 1 self obs" in output
    assert "  state:china: 1 obs · 1 spans · 0 self obs" in output
    assert "No alias matched for: " in output
    assert "  un_press: 1 obs" in output
    assert f"SCOPE: {SCOPE}" in output
    assert Observation.select().count() == 2
