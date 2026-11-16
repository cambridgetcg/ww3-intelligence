"""Counts of registered-actor mentions across one ledger file, read-only.

A casefile shows the actor mentions in at most a hundred records, with the
passages that hold them. This module answers the wider question a casefile
cannot: across a whole ledger, which registered actors the collected records
name, on which UTC day and in which source. It runs the casefile's own passage
builder and mention scanner (`ww3.actor-mentions/v3`) on each counted record,
so for any record it counts, a number here is the number a casefile would show.

The output holds ids, counts, days, hashes and versions, and never publisher
text. A matched span, a passage, a title and the name of an unregistered
source are all text a publisher or a collector supplied, so none of them is
read into the result. An unregistered source id is counted but not printed.
Every string in the output is therefore a constant, a registry id, a day, a
digest, a timestamp or the ledger's file name, which a test can check.

The file is opened with SQLite's `mode=ro&immutable=1` and nothing else that
writes. `mode=ro` alone is not read-only at the file level: on a file whose
header marks it as write-ahead-logged, SQLite creates `-shm` and `-wal` beside
it and leaves them, and in a read-only directory it cannot open the file at
all. `immutable=1` reads the main file as it stands and creates nothing, which
is what makes an archived copy safe to point at. It also ignores a live
write-ahead log, so a file with a non-empty `-wal` or `-journal` beside it is
refused rather than read without its last writes. Nothing here imports
`init_db` or Peewee's connection, so no table, trigger, index, journal mode or
permission is touched.

Mention count does not measure power, responsibility, agreement, importance,
or intent. A zero is an alias that did not match in the counted text, not
evidence that an actor went unnamed.
"""

from __future__ import annotations

import csv
import hashlib
import os
import sqlite3
import stat
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.db import SCHEMA_VERSION
from core.registry import (
    ACTOR_CONTRACT,
    ALIAS_FORMS,
    SOURCE_CONTRACT,
    SOURCE_IDENTITY_CONTRACT,
    actor_profiles,
    registry_artifact_sha256,
    source_identity_context,
    source_profiles,
)
from interpretation.actors import (
    ACTOR_MENTION_VERSION,
    MATCH_MODES,
    MAX_MENTION_CANDIDATES_PER_PASSAGE,
    MAX_MENTIONS_PER_OBSERVATION,
    MAX_MENTIONS_PER_PASSAGE,
    scan_actor_mentions,
)
from interpretation.rhetorlint import (
    MAX_PASSAGE_BYTES,
    MAX_PASSAGES,
    MAX_TOTAL_INPUT_BYTES,
    PASSAGE_TRANSFORM_VERSION,
    extract_passages,
)


# Bump when the row arithmetic or a default changes: two outputs are
# comparable only under one contract and one registry_artifacts_sha256.
MENTION_COUNTS_VERSION = "ww3.actor-mention-counts/v1"
COUNTED_IDENTITY_STATUS = "canonical-host-consistent"
# Listed at zero in the output, because a status no record reached and a
# status this command never checked are different claims.
SOURCE_IDENTITY_STATUSES = (
    "canonical-host-consistent", "canonical-host-match-insecure-url",
    "canonical-host-mismatch", "canonical-host-unverifiable",
    "canonical-url-missing-or-invalid", "synthetic-fixture",
    "unregistered-source-id",
)
REVISIONS_NEWEST = "newest-per-source-record"
REVISIONS_ALL = "all-stored-revisions"
DAY_BASIS_PUBLISHED = "published_at-else-fetched_at"
DAY_BASIS_FETCHED = "fetched_at"
DAY_BASES = (DAY_BASIS_PUBLISHED, DAY_BASIS_FETCHED)
MAX_LISTED_TRUNCATED_IDS = 1_000
CSV_COLUMNS = (
    "day", "source_id", "actor_id", "observations_mentioning", "spans",
    "publisher_self_mention", "observations_counted",
)
REQUIRED_COLUMNS = frozenset({
    "id", "dedupe_key", "source", "source_record_id", "raw_text",
    "canonical_url", "published_at", "fetched_at", "synthetic", "supersedes_id",
    "created_at",
})
SCOPE = (
    "Counts of visible registered-actor alias spans in stored records. Mention count does not "
    "measure power, responsibility, agreement, importance, or intent. These are counts of what "
    "the collector stored, not of what happened or of what a publisher published. Matching is "
    "bounded and may be incomplete: an actor at zero matched no alias in the counted text, which "
    "is not evidence that it was not named, and an actor outside the registry is never counted. "
    "A publisher self-mention is a mention of the actor registered as publisher of the record's "
    "own source; a ministry naming its state is not one."
)

_SOURCE_IDENTITY_FILTER = "canonical-host-consistent-only"
_SELF_MENTION_FILTER = "counted-and-flagged"
_HASH_CHUNK_BYTES = 1024 * 1024
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class LedgerReadError(RuntimeError):
    """Stable refusal to read a ledger file; never carries file content."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{message} ({code})")
        self.code = code


@dataclass(frozen=True, slots=True)
class _StoredRecord:
    # The two attributes `extract_passages` reads. A passage id hashes the
    # dedupe key, so a passage built here is the passage a casefile builds.
    dedupe_key: str
    raw_text: str


def _refuse_journal(path: Path) -> None:
    """Refuse a file whose write-ahead log or rollback journal holds bytes."""
    # An immutable read ignores both. A zero-byte one is what a cleanly closed
    # connection can leave behind and holds nothing to ignore.
    for suffix in ("-wal", "-journal"):
        try:
            size = os.stat(f"{path}{suffix}").st_size
        except FileNotFoundError:
            continue
        except OSError:
            size = None
        if size is None or size > 0:
            raise LedgerReadError(
                "ledger_has_journal",
                f"A non-empty {suffix} file sits beside the ledger, and an immutable "
                "read would ignore it. Read a copy taken after its writer closed",
            )


def resolve_ledger_path(ledger_path: str | os.PathLike) -> Path:
    """Resolve an existing regular ledger file; never create anything."""
    try:
        path = Path(ledger_path).expanduser().resolve(strict=True)
        mode = path.stat().st_mode
    except (FileNotFoundError, NotADirectoryError, RuntimeError):
        raise LedgerReadError(
            "ledger_not_found", "No ledger file at that path; nothing was created",
        ) from None
    except OSError:
        raise LedgerReadError(
            "ledger_unreadable", "The ledger path could not be examined",
        ) from None
    if not stat.S_ISREG(mode):
        raise LedgerReadError("ledger_not_a_file", "The ledger path is not a regular file")
    _refuse_journal(path)
    return path


def _file_state(path: Path) -> tuple[int, int, int, int]:
    state = os.stat(path)
    return (state.st_size, state.st_mtime_ns, state.st_ino, state.st_dev)


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _parse_utc(value: object) -> datetime | None:
    """Read stored timestamp text as UTC; naive text is already UTC."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
        if moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _utc_day(value: object) -> str | None:
    moment = _parse_utc(value)
    return moment.date().isoformat() if moment is not None else None


def _timestamp(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(_TIMESTAMP_FORMAT)


def _unsupported(message: str) -> LedgerReadError:
    return LedgerReadError("ledger_schema_unsupported", message)


def _schema_version(connection: sqlite3.Connection) -> int:
    table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'observation'"
    ).fetchone()
    if table is None:
        raise _unsupported("The file holds no observation table")
    columns = {row[1] for row in connection.execute('PRAGMA table_info("observation")')}
    if not REQUIRED_COLUMNS <= columns:
        raise _unsupported("The observation table lacks a column this command reads")
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if not isinstance(version, int) or version > SCHEMA_VERSION:
        raise _unsupported(
            f"The ledger schema is newer than the supported v{SCHEMA_VERSION}"
        )
    return version


def _select(connection: sqlite3.Connection, all_revisions: bool) -> dict:
    """Choose the rows to count from ids, record keys and revision links only."""
    # Newest is the largest (created_at, id) in a source record, the order
    # core/observations.py uses to choose what a new revision supersedes. This
    # runs in Python: a correlated subquery on (source, source_record_id) has
    # no index to use and did not finish on the real ledger in two minutes.
    rows = connection.execute(
        "SELECT id, source, source_record_id, created_at, supersedes_id, fetched_at "
        "FROM observation"
    ).fetchall()
    newest: dict[tuple, tuple[str, int]] = {}
    superseded: set[int] = set()
    fetched = []
    for row_id, source, record_id, created_at, supersedes_id, fetched_at in rows:
        # A row with no source record id is its own record.
        key = ("record", source, record_id) if record_id is not None else ("row", row_id)
        rank = (created_at if isinstance(created_at, str) else "", row_id)
        current = newest.get(key)
        if current is None or rank > current:
            newest[key] = rank
        if supersedes_id is not None:
            superseded.add(supersedes_id)
        moment = _parse_utc(fetched_at)
        if moment is not None:
            fetched.append(moment)
    newest_ids = {rank[1] for rank in newest.values()}
    # The stored links and the chosen order should say the same thing: the
    # newest row is superseded by nothing, and every older one by something.
    disagreements = sum(
        1 for row in rows if (row[0] in newest_ids) == (row[0] in superseded)
    )
    return {
        "selected": {row[0] for row in rows} if all_revisions else newest_ids,
        "observations_stored": len(rows),
        "source_records": len(newest),
        "revision_link_disagreements": disagreements,
        "max_id": max((row[0] for row in rows), default=None),
        "first_fetched_at": min(fetched, default=None),
        "last_fetched_at": max(fetched, default=None),
    }


def _scan(connection: sqlite3.Connection, selected: set[int], day_basis: str) -> dict:
    profiles = {profile.source_id: profile for profile in source_profiles()}
    by_status: Counter = Counter(dict.fromkeys(SOURCE_IDENTITY_STATUSES, 0))
    excluded: Counter = Counter()
    unregistered: set = set()
    basis_counts = {"published_at": 0, "fetched_at": 0, "undated": 0}
    # cells: (day, source) -> [observations counted, with a mention, spans]
    cells: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    # rows: (day, source, actor) -> [observations mentioning, spans]
    rows: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
    modes = dict.fromkeys(MATCH_MODES, 0)
    forms = dict.fromkeys(ALIAS_FORMS, 0)
    totals = Counter()
    scans_truncated: list[int] = []
    passages_truncated: list[int] = []

    cursor = connection.execute(
        "SELECT id, dedupe_key, source, canonical_url, synthetic, published_at, "
        "fetched_at, raw_text FROM observation ORDER BY id"
    )
    for (
        row_id, dedupe_key, source, canonical_url, synthetic,
        published_at, fetched_at, raw_text,
    ) in cursor:
        if row_id not in selected:
            continue
        if not isinstance(dedupe_key, str) or not isinstance(raw_text, str):
            raise _unsupported("An observation row holds a dedupe key or text that is not text")
        profile = profiles.get(source) if isinstance(source, str) else None
        identity = source_identity_context(
            source,
            canonical_url if isinstance(canonical_url, str) else None,
            synthetic=bool(synthetic),
        )
        status = identity["status"]
        by_status[status] += 1
        if status != COUNTED_IDENTITY_STATUS:
            # Only a registered id is printed; an unregistered one is a string
            # the collector stored, so it is counted and kept out of the output.
            excluded[(profile.source_id if profile else None, status)] += 1
            if profile is None:
                unregistered.add(source)
            continue

        if day_basis == DAY_BASIS_FETCHED:
            day, basis = _utc_day(fetched_at), "fetched_at"
        else:
            day, basis = _utc_day(published_at), "published_at"
            if day is None:
                day, basis = _utc_day(fetched_at), "fetched_at"
        if day is None:
            basis_counts["undated"] += 1
            continue
        basis_counts[basis] += 1

        passages = extract_passages(_StoredRecord(dedupe_key, raw_text))
        scan = scan_actor_mentions(passages)
        totals["observations_scanned"] += 1
        totals["passages_scanned"] += scan.passages_scanned
        totals["candidates_examined"] += scan.candidates_examined
        totals["mentions_emitted"] += len(scan.mentions)
        if not passages:
            totals["observations_without_passages"] += 1
        if scan.truncated:
            scans_truncated.append(row_id)
        if any(passage.truncated for passage in passages):
            passages_truncated.append(row_id)
        # Only the actor id, mode and spelling label are read from a mention;
        # its matched characters and the passage text stay where they are.
        per_actor = Counter(mention.actor_id for mention in scan.mentions)
        for mention in scan.mentions:
            modes[mention.match_mode] = modes.get(mention.match_mode, 0) + 1
            forms[mention.alias_form] = forms.get(mention.alias_form, 0) + 1
        cell = cells[(day, source)]
        cell[0] += 1
        if per_actor:
            totals["observations_with_mentions"] += 1
            cell[1] += 1
        for actor_id, spans in per_actor.items():
            row = rows[(day, source, actor_id)]
            row[0] += 1
            row[1] += spans
            cell[2] += spans

    return {
        "by_status": by_status,
        "excluded": excluded,
        "unregistered": len(unregistered),
        "basis_counts": basis_counts,
        "cells": cells,
        "rows": rows,
        "modes": modes,
        "forms": forms,
        "totals": totals,
        "scans_truncated": scans_truncated,
        "passages_truncated": passages_truncated,
    }


def _truncation(scans_truncated: list[int], passages_truncated: list[int]) -> dict:
    limit = MAX_LISTED_TRUNCATED_IDS
    omitted = max(0, len(scans_truncated) - limit) + max(0, len(passages_truncated) - limit)
    return {
        "complete": not scans_truncated and not passages_truncated,
        "scans_truncated": len(scans_truncated),
        "passages_truncated": len(passages_truncated),
        "scans_truncated_observation_ids": sorted(scans_truncated)[:limit],
        "passages_truncated_observation_ids": sorted(passages_truncated)[:limit],
        "ids_listed_at_most": limit,
        "ids_omitted": omitted,
    }


def _payload(
    *,
    path: Path,
    db_bytes: int,
    db_sha256: str,
    schema_version: int,
    selection: dict,
    counts: dict,
    all_revisions: bool,
    day_basis: str,
    generated_at: datetime,
) -> dict:
    publishers = {
        profile.source_id: profile.publisher_actor_id for profile in source_profiles()
    }
    cells = counts["cells"]
    totals = counts["totals"]

    rows = []
    actor_totals = {
        profile.actor_id: {
            "observations_mentioning": 0, "spans": 0,
            "publisher_self_observations": 0, "publisher_self_spans": 0,
            "sources": set(), "days": set(),
        }
        for profile in actor_profiles()
    }
    source_totals: dict[str, dict] = {}
    for (day, source_id), (observations, with_mentions, spans) in cells.items():
        state = source_totals.setdefault(source_id, {
            "observations_counted": 0, "observations_with_mentions": 0, "spans": 0,
            "actors": set(), "publisher_self_observations": 0,
            "publisher_self_spans": 0, "days": set(),
        })
        state["observations_counted"] += observations
        state["observations_with_mentions"] += with_mentions
        state["spans"] += spans
        state["days"].add(day)
    for (day, source_id, actor_id), (observations, spans) in sorted(counts["rows"].items()):
        self_mention = actor_id == publishers.get(source_id)
        rows.append({
            "day": day,
            "source_id": source_id,
            "actor_id": actor_id,
            "observations_mentioning": observations,
            "spans": spans,
            "publisher_self_mention": self_mention,
            "observations_counted": cells[(day, source_id)][0],
        })
        actor = actor_totals[actor_id]
        actor["observations_mentioning"] += observations
        actor["spans"] += spans
        actor["sources"].add(source_id)
        actor["days"].add(day)
        source = source_totals[source_id]
        source["actors"].add(actor_id)
        if self_mention:
            actor["publisher_self_observations"] += observations
            actor["publisher_self_spans"] += spans
            source["publisher_self_observations"] += observations
            source["publisher_self_spans"] += spans

    by_actor = sorted(
        (
            {
                "actor_id": actor_id,
                "observations_mentioning": state["observations_mentioning"],
                "spans": state["spans"],
                "publisher_self_observations": state["publisher_self_observations"],
                "publisher_self_spans": state["publisher_self_spans"],
                "sources_mentioning": len(state["sources"]),
                "days_mentioning": len(state["days"]),
            }
            for actor_id, state in actor_totals.items()
        ),
        key=lambda item: (-item["observations_mentioning"], -item["spans"], item["actor_id"]),
    )
    by_source = [
        {
            "source_id": source_id,
            "publisher_actor_id": publishers.get(source_id),
            "observations_counted": state["observations_counted"],
            "observations_with_mentions": state["observations_with_mentions"],
            "spans": state["spans"],
            "actors_mentioned": len(state["actors"]),
            "publisher_self_observations": state["publisher_self_observations"],
            "publisher_self_spans": state["publisher_self_spans"],
            "days": len(state["days"]),
        }
        for source_id, state in sorted(source_totals.items())
    ]
    by_day_source = [
        {
            "day": day,
            "source_id": source_id,
            "observations_counted": observations,
            "observations_with_mentions": with_mentions,
            "spans": spans,
        }
        for (day, source_id), (observations, with_mentions, spans) in sorted(cells.items())
    ]
    days = sorted({day for day, _ in cells})

    by_status = counts["by_status"]
    excluded_total = sum(
        count for status, count in by_status.items() if status != COUNTED_IDENTITY_STATUS
    )
    selected = len(selection["selected"])
    return {
        "contract": MENTION_COUNTS_VERSION,
        "generated_at": _timestamp(generated_at),
        "ledger": {
            "file_name": path.name,
            "db_bytes": db_bytes,
            "db_sha256": db_sha256,
            "schema_version": schema_version,
            "observations": selection["observations_stored"],
            "max_id": selection["max_id"],
            "first_fetched_at": _timestamp(selection["first_fetched_at"]),
            "last_fetched_at": _timestamp(selection["last_fetched_at"]),
        },
        "contracts": {
            "sources": SOURCE_CONTRACT,
            "actors": ACTOR_CONTRACT,
            "source_identity": SOURCE_IDENTITY_CONTRACT,
            "actor_mentions": ACTOR_MENTION_VERSION,
            "passages": PASSAGE_TRANSFORM_VERSION,
        },
        "registry_artifacts_sha256": registry_artifact_sha256(),
        "registry": {
            "actors": len(actor_profiles()),
            "sources": len(source_profiles()),
        },
        "filters": {
            "revisions": REVISIONS_ALL if all_revisions else REVISIONS_NEWEST,
            "day_basis": day_basis,
            "source_identity": _SOURCE_IDENTITY_FILTER,
            "publisher_self_mentions": _SELF_MENTION_FILTER,
        },
        "selection": {
            "observations_stored": selection["observations_stored"],
            "source_records": selection["source_records"],
            "revisions_superseded": (
                selection["observations_stored"] - selection["source_records"]
            ),
            "observations_selected": selected,
            "observations_excluded_by_source_identity": excluded_total,
            "observations_undated": counts["basis_counts"]["undated"],
            "observations_counted": totals["observations_scanned"],
            "revision_link_disagreements": selection["revision_link_disagreements"],
        },
        "source_identity": {
            "counted_status": COUNTED_IDENTITY_STATUS,
            "observations_by_status": dict(sorted(by_status.items())),
            "excluded": [
                {"source_id": source_id, "status": status, "observations": count}
                for (source_id, status), count in sorted(
                    counts["excluded"].items(),
                    key=lambda item: (item[0][0] is None, item[0][0] or "", item[0][1]),
                )
            ],
            "unregistered_source_ids": counts["unregistered"],
        },
        "day_basis": dict(counts["basis_counts"]),
        "days": {
            "first": days[0] if days else None,
            "last": days[-1] if days else None,
            "distinct": len(days),
        },
        "scan": {
            "contract": ACTOR_MENTION_VERSION,
            "observations_scanned": totals["observations_scanned"],
            "observations_with_mentions": totals["observations_with_mentions"],
            "observations_without_passages": totals["observations_without_passages"],
            "passages_scanned": totals["passages_scanned"],
            "candidates_examined": totals["candidates_examined"],
            "mentions_emitted": totals["mentions_emitted"],
            "match_modes": dict(counts["modes"]),
            "alias_forms": dict(counts["forms"]),
            "limits": {
                "candidates_per_passage": MAX_MENTION_CANDIDATES_PER_PASSAGE,
                "mentions_per_passage": MAX_MENTIONS_PER_PASSAGE,
                "mentions_per_observation": MAX_MENTIONS_PER_OBSERVATION,
                "passages_per_observation": MAX_PASSAGES,
                "passage_bytes": MAX_PASSAGE_BYTES,
                "observation_bytes": MAX_TOTAL_INPUT_BYTES,
            },
            "truncation": _truncation(
                counts["scans_truncated"], counts["passages_truncated"]
            ),
        },
        "rows": rows,
        "totals": {
            "by_actor": by_actor,
            "by_source": by_source,
            "by_day_source": by_day_source,
        },
        "scope": SCOPE,
    }


def count_actor_mentions(
    ledger_path: str | os.PathLike,
    *,
    all_revisions: bool = False,
    day_basis: str = DAY_BASIS_PUBLISHED,
    now: datetime | None = None,
) -> dict:
    """Count registered-actor mentions per UTC day and source, read-only."""
    if day_basis not in DAY_BASES:
        raise ValueError(f"day_basis must be one of: {', '.join(DAY_BASES)}")
    generated_at = now if now is not None else datetime.now(timezone.utc)
    path = resolve_ledger_path(ledger_path)

    before = _file_state(path)
    try:
        db_bytes, db_sha256 = _hash_file(path)
    except OSError:
        raise LedgerReadError(
            "ledger_unreadable", "The ledger file could not be read",
        ) from None
    if db_bytes != before[0]:
        raise LedgerReadError(
            "ledger_changed_during_read",
            "The ledger file changed while it was read; no counts were produced",
        )

    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error:
        raise LedgerReadError(
            "ledger_unreadable", "The ledger file could not be opened as SQLite",
        ) from None
    try:
        connection.execute("PRAGMA query_only = ON")
        schema_version = _schema_version(connection)
        selection = _select(connection, all_revisions)
        counts = _scan(connection, selection["selected"], day_basis)
    except sqlite3.DatabaseError:
        raise LedgerReadError(
            "ledger_unreadable", "The ledger file could not be read as SQLite",
        ) from None
    finally:
        connection.close()

    # Best effort: a writer that kept size, mtime and inode would go unseen.
    if _file_state(path) != before:
        raise LedgerReadError(
            "ledger_changed_during_read",
            "The ledger file changed while it was read; no counts were produced",
        )
    # A writer that opened the file during the read leaves its log here.
    _refuse_journal(path)

    return _payload(
        path=path,
        db_bytes=db_bytes,
        db_sha256=db_sha256,
        schema_version=schema_version,
        selection=selection,
        counts=counts,
        all_revisions=all_revisions,
        day_basis=day_basis,
        generated_at=generated_at,
    )


def write_rows_csv(payload: dict, handle) -> int:
    """Write the payload's rows as CSV; return how many rows were written."""
    writer = csv.writer(handle, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    written = 0
    for row in payload["rows"]:
        writer.writerow([
            ("true" if row[column] else "false")
            if isinstance(row[column], bool) else row[column]
            for column in CSV_COLUMNS
        ])
        written += 1
    return written


__all__ = [
    "COUNTED_IDENTITY_STATUS", "CSV_COLUMNS", "DAY_BASES", "DAY_BASIS_FETCHED",
    "DAY_BASIS_PUBLISHED", "MAX_LISTED_TRUNCATED_IDS", "MENTION_COUNTS_VERSION",
    "REQUIRED_COLUMNS", "REVISIONS_ALL", "REVISIONS_NEWEST", "SCOPE",
    "SOURCE_IDENTITY_STATUSES", "LedgerReadError", "count_actor_mentions",
    "resolve_ledger_path", "write_rows_csv",
]
