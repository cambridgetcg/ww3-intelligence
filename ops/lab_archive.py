#!/usr/bin/env python3
"""Collect, decrypt, verify and keep the ww3 exports on the lab Mac.

The ``ww3-export`` workflow leaves each encrypted copy of the observation
database as a GitHub artifact for seven days.  This script is the archive's
collector: it downloads every export it has not seen, keeps the ciphertext,
decrypts it with the archive key held in ``<root>/gnupg``, checks the database
against the manifest the export job wrote, and keeps the newest verified copies
as plain SQLite files.  ``ops/ARCHIVE.md`` is the runbook.

Subcommands::

    pull [--dispatch] [--ref REF]  collect every export not yet archived;
                                   --dispatch first requests a fresh one
    watch                          health report; re-enables a workflow that
                                   GitHub disabled for inactivity
    dispatch-ingest                request one ingest run unless one is running
                                   or has just been created
    accept-reset RUN_ID            accept a fork-suspected export as the start
                                   of a new ledger (no GitHub call)

It uses only the standard library and the ``gh`` and ``gpg`` executables, so it
runs from a copy outside any checkout.  It prints and logs counts, identifiers
and timestamps, never publisher text.  It never deletes a GitHub artifact:
another collector may still need one, and each expires on its own.

Environment: ``WW3_GH`` and ``WW3_GPG`` (executables, default found on PATH),
``WW3_REPO`` (default ``cambridgetcg/ww3-intelligence``) and
``WW3_ARCHIVE_ROOT`` (default ``~/ww3-archive``).
"""

from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

DEFAULT_REPO = "cambridgetcg/ww3-intelligence"
EXPORT_WORKFLOW = "ww3-export.yml"
INGEST_WORKFLOW = "ww3-ingest.yml"
EXPORT_WORKFLOW_PATH = f".github/workflows/{EXPORT_WORKFLOW}"
ARTIFACT_PREFIX = "ww3-export-"
EXPORT_EVENTS = frozenset({"schedule", "workflow_dispatch"})

INDEX_SCHEMA = "ww3-lab-archive-index/1"
STATUS_SCHEMA = "ww3-lab-archive-status/1"
MANIFEST_SCHEMA = "ww3-export/1"

MIB = 1024 * 1024
# The artifact zip and the ciphertext inside it are each refused above this.
MAX_CIPHERTEXT_BYTES = 64 * MIB
# Decrypted and decompressed sizes.  The database is about 40 MiB in November
# 2026; these bounds leave room for years of growth, not for a bomb.
MAX_PLAINTEXT_BYTES = 2048 * MIB
MAX_DB_BYTES = 2048 * MIB
MAX_MANIFEST_BYTES = 1 * MIB
EXPORT_MEMBERS = {"ww3.db": MAX_DB_BYTES, "manifest.json": MAX_MANIFEST_BYTES}

MIN_CORE_REMAINING = 300
DISPATCH_WAIT = timedelta(minutes=15)
DISPATCH_POLL_SECONDS = 20.0
# GitHub's clock and this Mac's may disagree by a little; a run created this
# long before the dispatch call still counts as the one it started.
DISPATCH_CLOCK_SKEW = timedelta(seconds=60)
INGEST_MIN_GAP = timedelta(minutes=20)
KEEP_RESTORED = 14
DEFAULT_STALE_HOURS = 72.0
DEFAULT_SOURCE_STALE_HOURS = {"usgs": 36.0}
DEFAULT_EXPORT_MAX_AGE_DAYS = 8.0
DEFAULT_INGEST_MAX_AGE_HOURS = 6.0
GH_TIMEOUT_SECONDS = 600
GPG_TIMEOUT_SECONDS = 600

ACCEPTED = frozenset({"ok", "ok-reset"})

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[bytes]"]


class ArchiveError(Exception):
    """A failure worth retrying later: nothing is recorded against the artifact."""


class Rejected(Exception):
    """An export whose content failed a check; the verdict is recorded."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    """Run gh without prompts, pagers, colour or update checks."""
    env = dict(
        os.environ,
        GH_PROMPT_DISABLED="1",
        GH_NO_UPDATE_NOTIFIER="1",
        GH_PAGER="cat",
        NO_COLOR="1",
    )
    return subprocess.run(
        list(argv), capture_output=True, env=env, timeout=GH_TIMEOUT_SECONDS, check=False
    )


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_time(value: str | None) -> datetime | None:
    """Read a GitHub or SQLite timestamp as an aware UTC datetime."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # The ledger stores naive UTC; GitHub always says Z.
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(MIB), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _first_line(data: bytes | str | None, limit: int = 160) -> str:
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else (data or "")
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return line[:limit] or "no message"


def _plain(value: object, limit: int = 120) -> str:
    """One log token: no whitespace, bounded length, never a raw control byte."""
    text = re.sub(r"\s+", "_", str(value))
    text = "".join(ch if ch.isprintable() else "?" for ch in text)
    return text[:limit]


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _write_private(path: Path, data: bytes) -> None:
    """Create ``path`` with mode 0600; refuse to replace anything already there."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def _write_json_atomic(path: Path, data: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.unlink(missing_ok=True)
    _write_private(tmp, (json.dumps(data, indent=1, sort_keys=True) + "\n").encode())
    os.replace(tmp, path)


class GitHub:
    """The few gh calls the archive makes, all through one injectable runner."""

    def __init__(self, gh: str, repo: str, runner: Runner):
        self.gh = gh
        self.repo = repo
        self.runner = runner

    def _run(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        try:
            done = self.runner([self.gh, *args])
        except (OSError, subprocess.SubprocessError) as exc:
            raise ArchiveError(f"gh {args[0]} could not run ({exc.__class__.__name__})") from exc
        if done.returncode != 0:
            raise ArchiveError(
                f"gh {' '.join(args[:2])} failed ({done.returncode}): {_first_line(done.stderr)}"
            )
        return done

    def get(self, path: str) -> Any:
        body = self._run("api", path).stdout or b""
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ArchiveError(f"gh api {path.split('?')[0]} did not answer JSON") from exc

    def get_bytes(self, path: str) -> bytes:
        return self._run("api", path).stdout or b""

    def core_remaining(self) -> int:
        data = self.get("rate_limit")
        try:
            return int(data["resources"]["core"]["remaining"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ArchiveError("gh api rate_limit had no core.remaining") from exc

    def workflow_run(self, workflow: str, ref: str) -> str:
        done = self._run("workflow", "run", workflow, "--ref", ref, "--repo", self.repo)
        return (done.stdout or b"").decode("utf-8", "replace") + (done.stderr or b"").decode(
            "utf-8", "replace"
        )

    def workflow_enable(self, workflow: str) -> None:
        self._run("workflow", "enable", workflow, "--repo", self.repo)

    def runs(self, workflow: str, query: str = "") -> list[dict[str, Any]]:
        data = self.get(f"repos/{self.repo}/actions/workflows/{workflow}/runs?{query}")
        runs = data.get("workflow_runs") if isinstance(data, dict) else None
        if not isinstance(runs, list):
            raise ArchiveError(f"the {workflow} runs listing had no workflow_runs")
        return runs


class Archive:
    """The archive root and everything kept under it."""

    def __init__(self, root: Path, gnupg: Path | None = None):
        self.root = root
        # The archive key lives in its own keyring under the root.  ``gnupg`` is
        # a seam for tests, whose keyrings need a short socket path.
        self.gnupg = gnupg if gnupg is not None else root / "gnupg"
        self.exports = root / "exports"
        self.restored = root / "restored"
        self.suspect = self.restored / "suspect"
        self.logs = root / "logs"
        self.work = root / ".work"
        self.index_path = root / "index.json"
        self.status_path = root / "status.json"
        self.log_path = self.logs / "lab-archive.log"
        self.latest = self.restored / "latest.db"

    def prepare(self) -> list[str]:
        notes = []
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if stat.S_IMODE(self.root.stat().st_mode) & 0o077:
            os.chmod(self.root, 0o700)
            notes.append("root-mode-tightened")
        for directory in (self.exports, self.restored, self.suspect, self.logs, self.work):
            directory.mkdir(mode=0o700, exist_ok=True)
        return notes

    def load_index(self) -> dict[str, Any]:
        try:
            data = json.loads(self.index_path.read_text("utf-8"))
        except FileNotFoundError:
            return {"schema": INDEX_SCHEMA, "latest": None, "artifacts": {}}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            # Never rebuild over an index that exists but cannot be read: it is
            # the record of every verdict, and a person should look first.
            raise ArchiveError(f"index.json could not be read ({exc.__class__.__name__})") from exc
        if not isinstance(data, dict) or data.get("schema") != INDEX_SCHEMA:
            raise ArchiveError("index.json is not a ww3-lab-archive-index/1 document")
        data.setdefault("latest", None)
        data.setdefault("artifacts", {})
        return data

    def save_index(self, index: dict[str, Any]) -> None:
        _write_json_atomic(self.index_path, index)


@dataclass
class Context:
    archive: Archive
    github: GitHub
    gpg: str
    now: Callable[[], datetime] = utcnow
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    out: TextIO = field(default_factory=lambda: sys.stdout)
    err: TextIO = field(default_factory=lambda: sys.stderr)
    subcommand: str = ""

    def log(self, event: str, **fields: object) -> None:
        tokens = [stamp(self.now()), self.subcommand or "-", _plain(event)]
        tokens += [f"{_plain(key)}={_plain(value)}" for key, value in fields.items()]
        self.archive.logs.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.archive.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(" ".join(tokens) + "\n")

    def say(self, line: str) -> None:
        print(line, file=self.out)

    def warn(self, line: str) -> None:
        print(line, file=self.err)

    def record_status(self, result: dict[str, Any]) -> None:
        try:
            status = json.loads(self.archive.status_path.read_text("utf-8"))
            if not isinstance(status, dict) or status.get("schema") != STATUS_SCHEMA:
                status = {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            status = {}
        status["schema"] = STATUS_SCHEMA
        status["updated_at"] = stamp(self.now())
        status.setdefault("subcommands", {})[self.subcommand] = result
        _write_json_atomic(self.archive.status_path, status)


# ---------------------------------------------------------------- shared checks


def require_rate_limit(ctx: Context) -> int:
    remaining = ctx.github.core_remaining()
    if remaining <= MIN_CORE_REMAINING:
        raise ArchiveError(
            f"GitHub API core remaining is {remaining} (need more than {MIN_CORE_REMAINING})"
        )
    return remaining


# ------------------------------------------------------------------------ pull


def dispatch_export(ctx: Context, ref: str) -> dict[str, Any]:
    """Request one export on ``ref`` and wait for that run to finish."""
    requested_at = ctx.now()
    said = ctx.github.workflow_run(EXPORT_WORKFLOW, ref)
    ctx.log("export-dispatched", ref=ref)
    # Recent gh prints the new run's URL; older ones print nothing useful.
    match = re.search(r"/actions/runs/(\d+)", said)
    run_id: int | None = int(match.group(1)) if match else None
    deadline = ctx.monotonic() + DISPATCH_WAIT.total_seconds()
    while True:
        if run_id is None:
            earliest = requested_at - DISPATCH_CLOCK_SKEW
            candidates = []
            for run in ctx.github.runs(EXPORT_WORKFLOW, "event=workflow_dispatch&per_page=10"):
                created = parse_time(run.get("created_at"))
                if run.get("event") == "workflow_dispatch" and created and created >= earliest:
                    candidates.append((created, int(run["id"])))
            if candidates:
                run_id = min(candidates)[1]
        if run_id is not None:
            run = ctx.github.get(f"repos/{ctx.github.repo}/actions/runs/{run_id}")
            if run.get("status") == "completed":
                conclusion = run.get("conclusion") or "unknown"
                ctx.log("export-finished", run=run_id, conclusion=conclusion)
                return {"run_id": run_id, "conclusion": conclusion}
        if ctx.monotonic() >= deadline:
            raise ArchiveError(
                "the dispatched export "
                + (f"run {run_id} " if run_id is not None else "")
                + "did not finish within 15 minutes"
            )
        ctx.sleep(DISPATCH_POLL_SECONDS)


def list_export_artifacts(ctx: Context) -> list[dict[str, Any]]:
    """Every unexpired ww3-export-* artifact in the repository."""
    found: list[dict[str, Any]] = []
    page = 1
    while True:
        data = ctx.github.get(
            f"repos/{ctx.github.repo}/actions/artifacts?per_page=100&page={page}"
        )
        batch = data.get("artifacts") if isinstance(data, dict) else None
        if not isinstance(batch, list):
            raise ArchiveError("the artifacts listing had no artifacts")
        found += [
            item
            for item in batch
            if str(item.get("name", "")).startswith(ARTIFACT_PREFIX) and not item.get("expired")
        ]
        total = data.get("total_count") or 0
        if len(batch) < 100 or page * 100 >= total:
            return found
        page += 1


def _artifact_run_id(artifact: dict[str, Any]) -> int:
    run = artifact.get("workflow_run") or {}
    suffix = str(artifact.get("name", ""))[len(ARTIFACT_PREFIX) :]
    if run.get("id") is None or str(run["id"]) != suffix:
        raise Rejected("provenance", "the artifact's name and its workflow run disagree")
    return int(run["id"])


def check_provenance(ctx: Context, run_id: int, allowed_refs: set[str]) -> dict[str, Any]:
    """Tie an artifact to this repository's own export workflow.

    The archive key's public half is public, so anyone can make a ciphertext it
    opens.  What says an artifact came from this collector is GitHub's record of
    the run that uploaded it: the workflow file, the event, the branch, and a head
    repository that is this one rather than a fork.
    """
    run = ctx.github.get(f"repos/{ctx.github.repo}/actions/runs/{run_id}")
    repo = ctx.github.repo.lower()
    problems = []
    if str(run.get("path", "")).split("@")[0] != EXPORT_WORKFLOW_PATH:
        problems.append("workflow")
    if run.get("event") not in EXPORT_EVENTS:
        problems.append("event")
    if run.get("head_branch") not in allowed_refs:
        problems.append("branch")
    if str((run.get("repository") or {}).get("full_name", "")).lower() != repo:
        problems.append("repository")
    if str((run.get("head_repository") or {}).get("full_name", "")).lower() != repo:
        problems.append("head-repository")
    if problems:
        raise Rejected("provenance", "the uploading run is not this repository's export: "
                       + ", ".join(problems))
    return run


def ciphertext_from_zip(data: bytes) -> tuple[str, bytes]:
    """The one .gpg member of an artifact zip, refusing anything else."""
    if len(data) > MAX_CIPHERTEXT_BYTES:
        raise Rejected("zip-size", "the artifact zip is over 64 MiB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise Rejected("zip-read", "the artifact is not a zip archive") from exc
    with archive:
        members = archive.infolist()
        if len(members) != 1:
            raise Rejected("zip-members", f"the artifact holds {len(members)} members, not 1")
        member = members[0]
        name = member.filename
        if (
            member.is_dir()
            or not name.endswith(".gpg")
            or name.startswith(".")
            or "/" in name
            or "\\" in name
            or name != os.path.basename(name)
        ):
            raise Rejected("zip-members", "the artifact member is not a plain .gpg file name")
        kind = stat.S_IFMT(member.external_attr >> 16)
        if kind not in (0, stat.S_IFREG):
            raise Rejected("zip-members", "the artifact member is not a regular file")
        if member.file_size > MAX_CIPHERTEXT_BYTES:
            raise Rejected("zip-size", "the ciphertext is over 64 MiB")
        try:
            with archive.open(member) as fh:
                blob = fh.read(MAX_CIPHERTEXT_BYTES + 1)
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as exc:
            raise Rejected("zip-read", "the artifact member could not be read") from exc
        if len(blob) > MAX_CIPHERTEXT_BYTES:
            raise Rejected("zip-size", "the ciphertext is over 64 MiB")
        return name, blob


# What gpg must report for an export: encrypted to public keys, opened with one
# of ours, and its integrity check passed.  ``gpg --decrypt`` also succeeds on a
# message that was never encrypted (``gpg --store``), which in a public
# repository would mean the artifact itself was readable by anyone.
REQUIRED_GPG_STATUS = frozenset({"ENC_TO", "DECRYPTION_KEY", "DECRYPTION_OKAY"})


def decrypt(ctx: Context, ciphertext: Path, plaintext: Path) -> None:
    if not ctx.archive.gnupg.is_dir():
        raise ArchiveError("the archive keyring directory does not exist")
    argv = [
        ctx.gpg,
        "--homedir", str(ctx.archive.gnupg),
        "--batch", "--no-tty", "--quiet",
        # The archive key has no passphrase, so nothing may ever ask for one: an
        # unattended job must not wait on a pinentry.
        "--pinentry-mode", "error",
        "--status-fd", "1",
        "--max-output", str(MAX_PLAINTEXT_BYTES),
        "--output", str(plaintext),
        "--decrypt", str(ciphertext),
    ]
    env = dict(os.environ, GNUPGHOME=str(ctx.archive.gnupg))
    try:
        done = subprocess.run(
            argv, capture_output=True, env=env, timeout=GPG_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ArchiveError(f"gpg could not run ({exc.__class__.__name__})") from exc
    if done.returncode != 0 or not plaintext.is_file():
        raise ArchiveError(f"gpg could not decrypt the export: {_first_line(done.stderr)}")
    reported = {
        line.split()[1]
        for line in (done.stdout or b"").decode("utf-8", "replace").splitlines()
        if line.startswith("[GNUPG:] ") and len(line.split()) > 1
    }
    if not REQUIRED_GPG_STATUS <= reported or "NEED_PASSPHRASE_SYM" in reported:
        raise Rejected(
            "not-encrypted",
            "gpg did not report a public-key encrypted, integrity-checked export",
        )


def untar_export(plaintext: Path, into: Path) -> dict[str, Path]:
    """Extract exactly ww3.db and manifest.json, as regular files, by bare name."""
    found: dict[str, Path] = {}
    try:
        with tarfile.open(plaintext, mode="r:gz") as tar:
            for member in tar:
                name = member.name
                if name not in EXPORT_MEMBERS:
                    raise Rejected("tar-members", f"unexpected export member {name[:80]!r}")
                if not member.isreg():
                    raise Rejected("tar-members", f"{name} is not a regular file")
                if name in found:
                    raise Rejected("tar-members", f"{name} appears twice")
                if member.size > EXPORT_MEMBERS[name]:
                    raise Rejected("tar-size", f"{name} is larger than this archive accepts")
                source = tar.extractfile(member)
                if source is None:
                    raise Rejected("tar-members", f"{name} has no content")
                target = into / name
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with source, os.fdopen(fd, "wb") as out:
                    shutil.copyfileobj(source, out, MIB)
                found[name] = target
    except (tarfile.TarError, gzip.BadGzipFile, zlib.error, EOFError) as exc:
        raise Rejected("tar-read", f"the export is not a readable tar.gz ({exc.__class__.__name__})") from exc
    missing = sorted(set(EXPORT_MEMBERS) - set(found))
    if missing:
        raise Rejected("tar-members", "the export lacks " + ", ".join(missing))
    return found


def read_manifest(path: Path, run_id: int) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Rejected("manifest", "manifest.json is not JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise Rejected("manifest", f"manifest.json is not a {MANIFEST_SCHEMA} document")
    if str(manifest.get("run_id")) != str(run_id):
        raise Rejected("manifest", "manifest.json names another export run")
    if not _is_count(manifest.get("observations")):
        raise Rejected("manifest", "manifest.json has no observation count")
    if manifest.get("max_id") is not None and not _is_count(manifest.get("max_id")):
        raise Rejected("manifest", "manifest.json has no usable max_id")
    if not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("db_sha256", ""))):
        raise Rejected("manifest", "manifest.json has no db_sha256")
    return manifest


def _read_only_uri(path: Path) -> str:
    # immutable: nothing writes an archived copy, so SQLite takes no lock and
    # leaves no -wal or -shm file beside it.
    return f"{path.resolve().as_uri()}?mode=ro&immutable=1"


def check_database(db: Path, manifest: dict[str, Any]) -> None:
    """The database is the one the export job described, byte for byte and row for row."""
    if sha256_file(db) != manifest["db_sha256"]:
        raise Rejected("sha-mismatch", "ww3.db does not match the manifest's db_sha256")
    if "db_bytes" in manifest and manifest["db_bytes"] != db.stat().st_size:
        raise Rejected("size-mismatch", "ww3.db is not the size the manifest records")
    try:
        con = sqlite3.connect(_read_only_uri(db), uri=True)
        try:
            check = con.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise Rejected("integrity", "PRAGMA integrity_check did not answer ok")
            count, max_id = con.execute("SELECT COUNT(*), MAX(id) FROM observation").fetchone()
        finally:
            con.close()
    except sqlite3.DatabaseError as exc:
        raise Rejected("sqlite", f"ww3.db could not be read ({exc.__class__.__name__})") from exc
    if count != manifest["observations"] or max_id != manifest["max_id"]:
        raise Rejected(
            "count-mismatch",
            f"ww3.db holds {count} observations (max id {max_id}); "
            f"the manifest says {manifest['observations']} (max id {manifest['max_id']})",
        )


def manifest_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "observations", "max_id", "by_source", "first_fetched_at", "last_fetched_at",
        "db_sha256", "db_bytes", "cache_key", "commit",
    )
    return {key: manifest.get(key) for key in keys}


def _day(value: str | None, *, empty: str) -> str:
    if value is None:
        return empty
    match = re.match(r"(\d{4}-\d{2}-\d{2})", str(value))
    return match.group(1) if match else "undated"


def previous_accepted(index: dict[str, Any], created_at: str) -> dict[str, Any] | None:
    earlier = [
        entry
        for entry in index["artifacts"].values()
        if entry.get("verdict") in ACCEPTED and entry.get("created_at", "") < created_at
    ]
    return max(earlier, key=lambda entry: (entry["created_at"], entry["id"]), default=None)


def shrinkage(manifest: dict[str, Any], previous: dict[str, Any] | None) -> list[str]:
    if previous is None:
        return []
    before = previous.get("manifest") or {}
    shrank = []
    for key in ("observations", "max_id"):
        old, new = before.get(key) or 0, manifest.get(key) or 0
        if new < old:
            shrank.append(f"{key} {old}->{new}")
    return shrank


def _place(source: Path, target: Path) -> None:
    """Move ``source`` to ``target`` without replacing a different file."""
    if target.exists():
        if sha256_file(target) != sha256_file(source):
            raise ArchiveError(f"{target.name} already exists with other content")
        source.unlink()
        return
    os.link(source, target)
    source.unlink()
    os.chmod(target, 0o600)


def repoint_latest(archive: Archive, target: Path) -> None:
    """Swap restored/latest.db to ``target`` in one rename."""
    tmp = archive.restored / f".latest.db.tmp-{os.getpid()}"
    tmp.unlink(missing_ok=True)
    os.symlink(target.name, tmp)
    os.replace(tmp, archive.latest)


def _newer_than_latest(index: dict[str, Any], entry: dict[str, Any]) -> bool:
    current = index["artifacts"].get(str(index.get("latest")))
    return current is None or (entry["created_at"], entry["id"]) > (
        current["created_at"], current["id"]
    )


def collect_artifact(
    ctx: Context, artifact: dict[str, Any], index: dict[str, Any], allowed_refs: set[str]
) -> dict[str, Any]:
    """Archive one artifact and return its index entry (raises ArchiveError to retry)."""
    archive = ctx.archive
    created_at = str(artifact.get("created_at") or "")
    entry: dict[str, Any] = {
        "id": int(artifact["id"]),
        "name": artifact.get("name"),
        "run_id": None,
        "created_at": created_at,
        "expires_at": artifact.get("expires_at"),
        "checked_at": stamp(ctx.now()),
        "verdict": None,
    }
    try:
        run_id = _artifact_run_id(artifact)
        entry["run_id"] = run_id
        run = check_provenance(ctx, run_id, allowed_refs)
        entry["event"] = run.get("event")
        entry["head_branch"] = run.get("head_branch")
        entry["head_sha"] = run.get("head_sha")
        if int(artifact.get("size_in_bytes") or 0) > MAX_CIPHERTEXT_BYTES:
            raise Rejected("zip-size", "the artifact is over 64 MiB")
        data = ctx.github.get_bytes(
            f"repos/{ctx.github.repo}/actions/artifacts/{entry['id']}/zip"
        )
        entry["zip_sha256"] = sha256_bytes(data)
        # Recorded, not enforced: whether GitHub serves the uploaded zip byte for
        # byte is its behaviour, not something this archive has verified.  The
        # content checks below are what decide the verdict.
        digest = str(artifact.get("digest") or "")
        entry["zip_digest_match"] = (
            digest.removeprefix("sha256:") == entry["zip_sha256"] if digest else None
        )
        _, blob = ciphertext_from_zip(data)
        entry["ciphertext_sha256"] = sha256_bytes(blob)
        cipher_name = f"{_day(created_at, empty='undated')}-{run_id}.tar.gz.gpg"
        cipher_path = archive.exports / cipher_name
        if cipher_path.exists():
            if sha256_file(cipher_path) != entry["ciphertext_sha256"]:
                raise ArchiveError(f"{cipher_name} already exists with other content")
        else:
            _write_private(cipher_path, blob)
        entry["ciphertext"] = f"exports/{cipher_name}"

        work = Path(tempfile.mkdtemp(prefix="pull-", dir=archive.work))
        try:
            plaintext = work / "export.tar.gz"
            decrypt(ctx, cipher_path, plaintext)
            members = untar_export(plaintext, work)
            manifest = read_manifest(members["manifest.json"], run_id)
            entry["manifest"] = manifest_summary(manifest)
            check_database(members["ww3.db"], manifest)

            day = _day(manifest.get("last_fetched_at"), empty="empty")
            db_name = f"ww3-{day}-{run_id}.db"
            shrank = shrinkage(manifest, previous_accepted(index, created_at))
            if shrank:
                _place(members["ww3.db"], archive.suspect / db_name)
                entry["restored"] = f"restored/suspect/{db_name}"
                raise Rejected("fork-suspected", "the ledger shrank: " + ", ".join(shrank))
            target = archive.restored / db_name
            _place(members["ww3.db"], target)
            entry["restored"] = f"restored/{db_name}"
        finally:
            shutil.rmtree(work, ignore_errors=True)
    except Rejected as exc:
        entry["verdict"] = "fork-suspected" if exc.code == "fork-suspected" else (
            "ignored" if exc.code == "provenance" else "rejected"
        )
        entry["reason"] = exc.code
        entry["detail"] = exc.detail
        return entry

    entry["verdict"] = "ok"
    if _newer_than_latest(index, entry):
        repoint_latest(archive, archive.root / entry["restored"])
        index["latest"] = str(entry["id"])
    return entry


def prune_restored(ctx: Context, index: dict[str, Any]) -> int:
    """Keep the newest restored copies; every ciphertext stays."""
    kept = sorted(
        (
            entry
            for entry in index["artifacts"].values()
            if entry.get("verdict") in ACCEPTED
            and str(entry.get("restored") or "").startswith("restored/ww3-")
        ),
        key=lambda entry: (entry["created_at"], entry["id"]),
        reverse=True,
    )
    pruned = 0
    for position, entry in enumerate(kept):
        if position < KEEP_RESTORED or str(entry["id"]) == str(index.get("latest")):
            continue
        (ctx.archive.root / entry["restored"]).unlink(missing_ok=True)
        ctx.log("restored-pruned", run=entry.get("run_id"))
        entry["restored"] = None
        entry["restored_pruned_at"] = stamp(ctx.now())
        pruned += 1
    return pruned


def cmd_pull(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    problems: list[str] = []
    result: dict[str, Any] = {"dispatched_run": None, "dispatch_conclusion": None}
    if args.dispatch:
        try:
            dispatched = dispatch_export(ctx, args.ref)
            result["dispatched_run"] = dispatched["run_id"]
            result["dispatch_conclusion"] = dispatched["conclusion"]
            if dispatched["conclusion"] != "success":
                problems.append(f"dispatched export concluded {dispatched['conclusion']}")
        except ArchiveError as exc:
            # A failed request still leaves older exports worth collecting.
            problems.append(f"dispatch: {exc}")
            ctx.log("export-dispatch-failed", error=exc)

    index = ctx.archive.load_index()
    artifacts = list_export_artifacts(ctx)
    fresh = sorted(
        (item for item in artifacts if str(item.get("id")) not in index["artifacts"]),
        key=lambda item: (str(item.get("created_at") or ""), int(item.get("id") or 0)),
    )
    counts = {"ok": 0, "fork-suspected": 0, "rejected": 0, "ignored": 0, "retry": 0}
    allowed_refs = {"main", args.ref}
    for artifact in fresh:
        try:
            entry = collect_artifact(ctx, artifact, index, allowed_refs)
        except ArchiveError as exc:
            counts["retry"] += 1
            problems.append(f"artifact {artifact.get('id')}: {exc}")
            ctx.log("artifact-retry", id=artifact.get("id"), error=exc)
            continue
        index["artifacts"][str(entry["id"])] = entry
        ctx.archive.save_index(index)
        counts[entry["verdict"]] += 1
        summary = entry.get("manifest") or {}
        ctx.log(
            f"artifact-{entry['verdict']}",
            id=entry["id"],
            run=entry.get("run_id"),
            reason=entry.get("reason", "-"),
            observations=summary.get("observations", "-"),
            max_id=summary.get("max_id", "-"),
        )
        if entry["verdict"] != "ok":
            problems.append(f"run {entry.get('run_id')}: {entry['verdict']} ({entry.get('reason')})")
            ctx.warn(f"export run {entry.get('run_id')}: {entry['verdict']} — {entry.get('detail')}")
    pruned = prune_restored(ctx, index)
    ctx.archive.save_index(index)

    latest = index["artifacts"].get(str(index.get("latest"))) or {}
    latest_summary = latest.get("manifest") or {}
    ctx.say(
        f"artifacts listed: {len(artifacts)}, new: {len(fresh)}, ok: {counts['ok']}, "
        f"fork-suspected: {counts['fork-suspected']}, rejected: {counts['rejected']}, "
        f"ignored: {counts['ignored']}, to retry: {counts['retry']}, pruned: {pruned}"
    )
    if latest:
        ctx.say(
            f"latest: run {latest.get('run_id')}, {latest_summary.get('observations')} "
            f"observations (max id {latest_summary.get('max_id')}), "
            f"last fetched {latest_summary.get('last_fetched_at')}"
        )
    result.update(
        artifacts_listed=len(artifacts),
        new=len(fresh),
        counts=counts,
        pruned=pruned,
        latest_run=latest.get("run_id"),
        latest_observations=latest_summary.get("observations"),
        latest_max_id=latest_summary.get("max_id"),
        problems=problems,
    )
    return result


# ------------------------------------------------------------------ accept-reset


def cmd_accept_reset(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    """A person's decision that a shrunken ledger is a deliberate new start."""
    index = ctx.archive.load_index()
    matches = [
        entry
        for entry in index["artifacts"].values()
        if str(entry.get("run_id")) == str(args.run_id) and entry.get("verdict") == "fork-suspected"
    ]
    if len(matches) != 1:
        raise ArchiveError(f"no single fork-suspected export for run {args.run_id}")
    entry = matches[0]
    held = ctx.archive.root / str(entry.get("restored") or "")
    if not entry.get("restored") or not held.is_file():
        raise ArchiveError("the fork-suspected database is no longer in restored/suspect")
    if sha256_file(held) != (entry.get("manifest") or {}).get("db_sha256"):
        raise ArchiveError("the held database no longer matches its manifest")
    target = ctx.archive.restored / held.name
    _place(held, target)
    entry["restored"] = f"restored/{held.name}"
    entry["verdict"] = "ok-reset"
    entry["accepted_at"] = stamp(ctx.now())
    if _newer_than_latest(index, entry):
        repoint_latest(ctx.archive, target)
        index["latest"] = str(entry["id"])
    ctx.archive.save_index(index)
    summary = entry.get("manifest") or {}
    ctx.log("reset-accepted", run=entry["run_id"], observations=summary.get("observations"))
    ctx.say(
        f"accepted run {entry['run_id']} as a new ledger: {summary.get('observations')} "
        f"observations (max id {summary.get('max_id')})"
    )
    return {"run_id": entry["run_id"], "latest": index.get("latest") == str(entry["id"]),
            "problems": []}


# ------------------------------------------------------------------------ watch


def _parse_thresholds(pairs: Sequence[str]) -> dict[str, float | None]:
    thresholds: dict[str, float | None] = dict(DEFAULT_SOURCE_STALE_HOURS)
    for pair in pairs:
        source, _, hours = pair.partition("=")
        if not source or not hours:
            raise ArchiveError(f"--source-stale-hours wants SOURCE=HOURS, not {pair!r}")
        if hours == "off":
            thresholds[source] = None
            continue
        try:
            thresholds[source] = float(hours)
        except ValueError as exc:
            raise ArchiveError(f"--source-stale-hours {pair!r} is not a number") from exc
    return thresholds


def source_ages(latest: Path, now: datetime) -> list[tuple[str, float | None]]:
    con = sqlite3.connect(_read_only_uri(latest), uri=True)
    try:
        rows = con.execute(
            "SELECT source, MAX(fetched_at) FROM observation GROUP BY source ORDER BY source"
        ).fetchall()
    finally:
        con.close()
    ages = []
    for source, newest in rows:
        moment = parse_time(newest)
        ages.append((str(source), None if moment is None else (now - moment).total_seconds() / 3600))
    return ages


def cmd_watch(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    now = ctx.now()
    problems: list[str] = []
    result: dict[str, Any] = {}

    runs = ctx.github.runs(INGEST_WORKFLOW, "per_page=1")
    if runs:
        newest = runs[0]
        created = parse_time(newest.get("created_at"))
        age = None if created is None else (now - created).total_seconds() / 3600
        state = newest.get("conclusion") or newest.get("status") or "unknown"
        result["ingest_newest_run"] = {"id": newest.get("id"), "age_hours": age, "state": state}
        ctx.say(f"ingest: newest run {newest.get('id')}, "
                f"{'?' if age is None else f'{age:.1f}'} h ago, {state}")
        if age is None or age > args.ingest_max_age_hours:
            problems.append(f"no ingest run created in {args.ingest_max_age_hours:g} h")
        if newest.get("status") == "completed" and newest.get("conclusion") != "success":
            problems.append(f"newest ingest run concluded {newest.get('conclusion')}")
    else:
        result["ingest_newest_run"] = None
        problems.append("no ingest run listed")
        ctx.say("ingest: no runs listed")

    states = {}
    for workflow in (INGEST_WORKFLOW, EXPORT_WORKFLOW):
        data = ctx.github.get(f"repos/{ctx.github.repo}/actions/workflows/{workflow}")
        state = str(data.get("state") or "unknown")
        states[workflow] = state
        if state == "disabled_inactivity":
            # The 60-day rule fired.  Turning the workflow back on is the one
            # change watch makes, and it says so as loudly as it can.
            ctx.github.workflow_enable(workflow)
            states[workflow] = "re-enabled"
            ctx.log("WORKFLOW-RE-ENABLED", workflow=workflow, was=state)
            ctx.warn(f"WARNING: {workflow} was disabled for inactivity; re-enabled it.")
            problems.append(f"{workflow} was disabled_inactivity and has been re-enabled")
        elif state != "active":
            problems.append(f"{workflow} is {state}")
        ctx.say(f"workflow {workflow}: {states[workflow]}")
    result["workflows"] = states

    thresholds = _parse_thresholds(args.source_stale_hours)
    stale: list[str] = []
    latest = ctx.archive.latest
    if latest.exists():
        ages = source_ages(latest, now)
        result["sources"] = {source: age for source, age in ages}
        ctx.say("sources in restored/latest.db, hours since the newest fetched_at:")
        for source, age in ages:
            limit = thresholds.get(source, args.stale_hours)
            flag = ""
            if limit is not None and (age is None or age > limit):
                flag = f"  STALE (over {limit:g} h)"
                stale.append(source)
            ctx.say(f"  {source:<32} {'?' if age is None else f'{age:7.1f}'}{flag}")
        if stale:
            problems.append(f"{len(stale)} source(s) stale: " + ", ".join(stale))
    else:
        result["sources"] = None
        problems.append("no restored/latest.db yet")
        ctx.say("sources: no restored/latest.db yet")
    result["stale_sources"] = stale

    index = ctx.archive.load_index()
    accepted = [entry for entry in index["artifacts"].values() if entry.get("verdict") in ACCEPTED]
    newest_export = max(accepted, key=lambda entry: (entry["created_at"], entry["id"]), default=None)
    verdicts: dict[str, int] = {}
    for entry in index["artifacts"].values():
        verdicts[str(entry.get("verdict"))] = verdicts.get(str(entry.get("verdict")), 0) + 1
    result["verdicts"] = verdicts
    if newest_export is None:
        result["newest_export_age_days"] = None
        problems.append("no export archived yet")
        ctx.say("archive: no export archived yet")
    else:
        created = parse_time(newest_export["created_at"])
        age_days = None if created is None else (now - created).total_seconds() / 86400
        result["newest_export_age_days"] = age_days
        ctx.say(f"archive: newest verified export is run {newest_export.get('run_id')}, "
                f"{'?' if age_days is None else f'{age_days:.1f}'} days old; verdicts "
                + ", ".join(f"{key} {value}" for key, value in sorted(verdicts.items())))
        if age_days is None or age_days > args.export_max_age_days:
            problems.append(f"newest verified export is over {args.export_max_age_days:g} days old")

    result["problems"] = problems
    return result


# ------------------------------------------------------------- dispatch-ingest


def cmd_dispatch_ingest(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    now = ctx.now()
    runs = ctx.github.runs(INGEST_WORKFLOW, "per_page=10")
    active = [run for run in runs if run.get("status") != "completed"]
    if active:
        ctx.say(f"ingest: run {active[0].get('id')} is {active[0].get('status')}; not dispatching")
        ctx.log("ingest-skipped", reason="run-active", run=active[0].get("id"))
        return {"dispatched": False, "reason": "run-active", "problems": []}
    created = [moment for moment in (parse_time(run.get("created_at")) for run in runs) if moment]
    if created and now - max(created) < INGEST_MIN_GAP:
        minutes = (now - max(created)).total_seconds() / 60
        ctx.say(f"ingest: newest run was created {minutes:.0f} min ago; not dispatching")
        ctx.log("ingest-skipped", reason="recent", minutes=f"{minutes:.0f}")
        return {"dispatched": False, "reason": "recent", "problems": []}
    ctx.github.workflow_run(INGEST_WORKFLOW, "main")
    ctx.say("ingest: dispatched on main")
    ctx.log("ingest-dispatched", ref="main")
    return {"dispatched": True, "reason": None, "problems": []}


# ------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lab_archive.py",
        description="Collect, verify and keep the encrypted ww3 exports (ops/ARCHIVE.md).",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    pull = sub.add_parser("pull", help="archive every export not yet seen")
    pull.add_argument("--dispatch", action="store_true", help="request a fresh export first")
    pull.add_argument("--ref", default="main", help="ref to dispatch the export on (default main)")

    watch = sub.add_parser("watch", help="report collector and archive health")
    watch.add_argument("--stale-hours", type=float, default=DEFAULT_STALE_HOURS,
                       help="flag a source with no fetch for this long (default 72)")
    watch.add_argument("--source-stale-hours", action="append", default=[],
                       metavar="SOURCE=HOURS",
                       help="per-source threshold, or SOURCE=off (default usgs=36)")
    watch.add_argument("--export-max-age-days", type=float, default=DEFAULT_EXPORT_MAX_AGE_DAYS,
                       help="flag when the newest verified export is older (default 8)")
    watch.add_argument("--ingest-max-age-hours", type=float, default=DEFAULT_INGEST_MAX_AGE_HOURS,
                       help="flag when no ingest run was created this recently (default 6)")

    sub.add_parser("dispatch-ingest", help="request one ingest run unless one is due anyway")

    accept = sub.add_parser("accept-reset", help="accept a fork-suspected export as a new ledger")
    accept.add_argument("run_id", type=int)
    return parser


COMMANDS = {
    "pull": cmd_pull,
    "watch": cmd_watch,
    "dispatch-ingest": cmd_dispatch_ingest,
    "accept-reset": cmd_accept_reset,
}
# accept-reset works on files already here and makes no GitHub call.
OFFLINE = frozenset({"accept-reset"})


def context_from_env(runner: Runner = run_command) -> Context:
    root = Path(os.environ.get("WW3_ARCHIVE_ROOT") or Path.home() / "ww3-archive").expanduser()
    gh = os.environ.get("WW3_GH") or shutil.which("gh") or "gh"
    gpg = os.environ.get("WW3_GPG") or shutil.which("gpg") or "gpg"
    repo = os.environ.get("WW3_REPO") or DEFAULT_REPO
    return Context(archive=Archive(root), github=GitHub(gh, repo, runner), gpg=gpg)


def run(ctx: Context, argv: Sequence[str]) -> int:
    args = build_parser().parse_args(list(argv))
    ctx.subcommand = args.subcommand
    started = ctx.now()
    for note in ctx.archive.prepare():
        ctx.log(note)
    lock_fd = os.open(ctx.archive.root / ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            ctx.warn("another lab_archive run holds the archive lock")
            ctx.log("lock-busy")
            return 1
        ctx.log("start")
        try:
            if args.subcommand not in OFFLINE:
                remaining = require_rate_limit(ctx)
                ctx.log("rate-limit", core_remaining=remaining)
            result = COMMANDS[args.subcommand](ctx, args)
        except (ArchiveError, sqlite3.Error, OSError) as exc:
            message = str(exc) if isinstance(exc, ArchiveError) else exc.__class__.__name__
            ctx.warn(f"{args.subcommand} failed: {message}")
            ctx.log("failed", error=message)
            ctx.record_status({"at": stamp(started), "ok": False, "exit": 1, "problems": [message]})
            return 1
        except Exception as exc:
            # A bug, not a verdict: leave a trace in the status and log, then let
            # the traceback reach launchd's error log.
            ctx.log("crashed", error=exc.__class__.__name__)
            ctx.record_status({"at": stamp(started), "ok": False, "exit": 1,
                               "problems": [f"crashed: {exc.__class__.__name__}"]})
            raise
        problems = result.get("problems") or []
        code = 1 if problems else 0
        for problem in problems:
            ctx.warn(f"problem: {problem}")
        ctx.log("done", problems=len(problems))
        ctx.record_status({"at": stamp(started), "ok": not problems, "exit": code, **result})
        return code
    finally:
        os.close(lock_fd)


def main(argv: Sequence[str] | None = None) -> int:
    return run(context_from_env(), sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
