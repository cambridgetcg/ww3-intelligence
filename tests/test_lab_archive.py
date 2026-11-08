"""The lab archive collector, against a fake gh and a throwaway gpg key.

No test reaches GitHub: every gh call goes through an injected runner that
answers from a table and records what was asked.  gpg is real, because what
the archive promises is that it can open what the export job encrypts.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops import lab_archive

REPO = "cambridgetcg/ww3-intelligence"
NOW = datetime(2026, 11, 9, 4, 20, tzinfo=UTC)
GPG = os.environ.get("WW3_GPG") or shutil.which("gpg")
ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = f"api repos/{REPO}/actions/artifacts?per_page=100&page=1"


def _short_tmp() -> str:
    # gpg-agent puts its socket in the keyring directory, and a Unix socket
    # path is limited to about 104 bytes; pytest's tmp_path on macOS is longer.
    for candidate in ("/tmp", tempfile.gettempdir()):
        if os.path.isdir(candidate) and os.access(candidate, os.W_OK):
            return candidate
    return tempfile.gettempdir()


def _gpg(home: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [GPG, "--homedir", str(home), "--batch", "--quiet", *args],
        capture_output=True,
        check=True,
        timeout=120,
    )


@pytest.fixture(scope="module")
def keyring():
    if not GPG:
        pytest.skip("gpg is not installed")
    home = Path(tempfile.mkdtemp(prefix="w3a", dir=_short_tmp()))
    os.chmod(home, 0o700)
    try:
        _gpg(home, "--pinentry-mode", "loopback", "--passphrase", "",
             "--quick-gen-key", "ww3 archive test <archive-test@invalid>",
             "future-default", "default", "never")
        pub = home / "test.pub.asc"
        pub.write_bytes(_gpg(home, "--armor", "--export").stdout)
        yield SimpleNamespace(home=home, pub=pub)
    finally:
        gpgconf = shutil.which("gpgconf") or str(Path(GPG).with_name("gpgconf"))
        subprocess.run([gpgconf, "--homedir", str(home), "--kill", "all"],
                       capture_output=True, check=False)
        shutil.rmtree(home, ignore_errors=True)


class FakeGh:
    """Answers gh calls from a table; anything unlisted fails like a 404."""

    def __init__(self, remaining: int = 4000):
        self.routes: dict[str, object] = {}
        self.calls: list[str] = []
        self.artifacts: list[dict] = []
        self.route("api rate_limit", {"resources": {"core": {"remaining": remaining}}})
        self.route(ARTIFACTS, lambda: {"total_count": len(self.artifacts),
                                       "artifacts": list(self.artifacts)})

    def route(self, call: str, answer: object) -> None:
        self.routes[call] = answer

    def __call__(self, argv):
        assert argv[0] == "gh"
        call = " ".join(argv[1:])
        self.calls.append(call)
        if call not in self.routes:
            return subprocess.CompletedProcess(argv, 1, b"", b"HTTP 404: Not Found")
        answer = self.routes[call]
        if callable(answer):
            answer = answer()
        if not isinstance(answer, bytes):
            answer = json.dumps(answer).encode()
        return subprocess.CompletedProcess(argv, 0, answer, b"")

    def count(self, prefix: str) -> int:
        return sum(1 for call in self.calls if call.startswith(prefix))

    def publish(self, artifact_id, run_id, zipped, *, created, branch="main",
                event="workflow_dispatch", head_repo=REPO):
        self.artifacts.append({
            "id": artifact_id,
            "name": f"ww3-export-{run_id}",
            "size_in_bytes": len(zipped),
            "expired": False,
            "created_at": created,
            "expires_at": "2026-11-20T00:00:00Z",
            "digest": "sha256:" + hashlib.sha256(zipped).hexdigest(),
            "workflow_run": {"id": run_id, "head_branch": branch},
        })
        self.route(f"api repos/{REPO}/actions/runs/{run_id}", {
            "id": run_id,
            "path": ".github/workflows/ww3-export.yml",
            "event": event,
            "head_branch": branch,
            "head_sha": "0" * 40,
            "status": "completed",
            "conclusion": "success",
            "repository": {"full_name": REPO},
            "head_repository": {"full_name": head_repo},
        })
        self.route(f"api repos/{REPO}/actions/artifacts/{artifact_id}/zip", zipped)


def make_db(path: Path, rows) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE observation (id INTEGER PRIMARY KEY, source TEXT NOT NULL,"
                " fetched_at TEXT NOT NULL)")
    con.executemany("INSERT INTO observation (source, fetched_at) VALUES (?, ?)", rows)
    con.commit()
    con.close()
    return path


def make_export(keyring, work: Path, run_id: int, rows, *, manifest_changes=None,
                extra_members=(), zip_members=None) -> bytes:
    """Build an artifact zip the way ww3-export.yml does, with optional damage."""
    where = work / str(run_id)
    db = make_db(where / "ww3.db", rows)
    data = db.read_bytes()
    con = sqlite3.connect(db)
    count, max_id = con.execute("SELECT COUNT(*), MAX(id) FROM observation").fetchone()
    by_source = dict(con.execute("SELECT source, COUNT(*) FROM observation GROUP BY source"))
    first, last = con.execute("SELECT MIN(fetched_at), MAX(fetched_at) FROM observation").fetchone()
    con.close()
    manifest = {
        "schema": "ww3-export/1", "cache_key": f"ww3-db-{run_id - 1}", "run_id": str(run_id),
        "commit": "0" * 40, "db_sha256": hashlib.sha256(data).hexdigest(),
        "db_bytes": len(data), "observations": count, "max_id": max_id,
        "by_source": by_source, "first_fetched_at": first, "last_fetched_at": last,
    }
    manifest.update(manifest_changes or {})
    members = [("ww3.db", data), ("manifest.json", json.dumps(manifest).encode()),
               *extra_members]
    tarball = where / "ww3-export.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        for name, payload in members:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    cipher = where / "ww3-export.tar.gz.gpg"
    _gpg(keyring.home, "--trust-model", "always", "--recipient-file", str(keyring.pub),
         "--output", str(cipher), "--encrypt", str(tarball))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        for name in zip_members or ["ww3-export.tar.gz.gpg"]:
            zipped.writestr(name, cipher.read_bytes())
    return buffer.getvalue()


def rows(n, *, day="2026-11-08", source="bbc_world"):
    return [(source, f"{day} {hour % 24:02d}:00:00") for hour in range(n)]


class Clock:
    def __init__(self, start=NOW):
        self.now = start
        self.ticks = 0.0

    def sleep(self, seconds):
        self.ticks += seconds
        self.now += timedelta(seconds=seconds)


def make_ctx(tmp_path: Path, gh: FakeGh, keyring=None, clock: Clock | None = None):
    clock = clock or Clock()
    archive = lab_archive.Archive(
        tmp_path / "archive",
        gnupg=keyring.home if keyring is not None else tmp_path / "no-keyring",
    )
    return lab_archive.Context(
        archive=archive,
        github=lab_archive.GitHub("gh", REPO, gh),
        gpg=GPG or "gpg",
        now=lambda: clock.now,
        sleep=clock.sleep,
        monotonic=lambda: clock.ticks,
        out=io.StringIO(),
        err=io.StringIO(),
    )


def index_of(ctx):
    return json.loads(ctx.archive.index_path.read_text())


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --------------------------------------------------------------------- pull


def test_the_keyring_defaults_to_the_archive_root(tmp_path):
    assert lab_archive.Archive(tmp_path).gnupg == tmp_path / "gnupg"


def test_a_good_export_verifies_and_becomes_latest(tmp_path, keyring):
    gh = FakeGh()
    gh.publish(9001, 501, make_export(keyring, tmp_path / "build", 501, rows(5)),
               created="2026-11-09T04:21:00Z")
    ctx = make_ctx(tmp_path, gh, keyring)

    assert lab_archive.run(ctx, ["pull"]) == 0

    archive = ctx.archive
    entry = index_of(ctx)["artifacts"]["9001"]
    assert entry["verdict"] == "ok"
    assert entry["manifest"]["observations"] == 5
    assert entry["zip_digest_match"] is True
    assert os.readlink(archive.latest) == "ww3-2026-11-08-501.db"
    cipher = archive.exports / "2026-11-09-501.tar.gz.gpg"
    assert mode(cipher) == 0o600
    assert mode(archive.restored / "ww3-2026-11-08-501.db") == 0o600
    assert mode(archive.root) == 0o700
    assert list(archive.work.iterdir()) == []
    con = sqlite3.connect(f"{archive.latest.resolve().as_uri()}?mode=ro", uri=True)
    assert con.execute("SELECT COUNT(*) FROM observation").fetchone()[0] == 5
    con.close()

    status = json.loads(archive.status_path.read_text())["subcommands"]["pull"]
    assert status["ok"] is True and status["counts"]["ok"] == 1
    assert "artifact-ok" in archive.log_path.read_text()
    # The archive only reads from GitHub: no method other than GET, no deletion.
    assert not any("-X" in call or "DELETE" in call.upper() for call in gh.calls)

    # A second pull downloads nothing it already holds.
    assert lab_archive.run(ctx, ["pull"]) == 0
    assert gh.count(f"api repos/{REPO}/actions/artifacts/9001/zip") == 1


def test_a_sha_mismatch_is_rejected_and_the_ciphertext_kept(tmp_path, keyring):
    gh = FakeGh()
    gh.publish(9001, 501, make_export(keyring, tmp_path / "build", 501, rows(5),
                                      manifest_changes={"db_sha256": "0" * 64}),
               created="2026-11-09T04:21:00Z")
    ctx = make_ctx(tmp_path, gh, keyring)

    assert lab_archive.run(ctx, ["pull"]) == 1

    entry = index_of(ctx)["artifacts"]["9001"]
    assert (entry["verdict"], entry["reason"]) == ("rejected", "sha-mismatch")
    assert not ctx.archive.latest.exists()
    assert (ctx.archive.exports / "2026-11-09-501.tar.gz.gpg").exists()
    assert list(ctx.archive.restored.glob("ww3-*.db")) == []


def test_a_shrinking_ledger_is_fork_suspected_and_latest_stays(tmp_path, keyring):
    gh = FakeGh()
    gh.publish(9001, 501, make_export(keyring, tmp_path / "build", 501, rows(5)),
               created="2026-11-08T04:21:00Z")
    gh.publish(9002, 502, make_export(keyring, tmp_path / "build", 502,
                                      rows(2, day="2026-11-09")),
               created="2026-11-09T04:21:00Z")
    ctx = make_ctx(tmp_path, gh, keyring)

    assert lab_archive.run(ctx, ["pull"]) == 1

    index = index_of(ctx)
    assert index["artifacts"]["9001"]["verdict"] == "ok"
    forked = index["artifacts"]["9002"]
    assert (forked["verdict"], forked["reason"]) == ("fork-suspected", "fork-suspected")
    assert "observations 5->2" in forked["detail"]
    assert os.readlink(ctx.archive.latest) == "ww3-2026-11-08-501.db"
    assert (ctx.archive.suspect / "ww3-2026-11-09-502.db").exists()

    # A person who knows the reset was deliberate accepts the new ledger.
    assert lab_archive.run(ctx, ["accept-reset", "502"]) == 0
    index = index_of(ctx)
    assert index["artifacts"]["9002"]["verdict"] == "ok-reset"
    assert os.readlink(ctx.archive.latest) == "ww3-2026-11-09-502.db"
    assert not (ctx.archive.suspect / "ww3-2026-11-09-502.db").exists()


@pytest.mark.parametrize("name", ["notes.txt", "../ww3.db", "nested/ww3.db", "/ww3.db"])
def test_a_tar_with_an_extra_or_traversal_member_is_rejected(tmp_path, keyring, name):
    gh = FakeGh()
    gh.publish(9001, 501, make_export(keyring, tmp_path / "build", 501, rows(3),
                                      extra_members=[(name, b"not part of an export")]),
               created="2026-11-09T04:21:00Z")
    ctx = make_ctx(tmp_path, gh, keyring)

    assert lab_archive.run(ctx, ["pull"]) == 1

    entry = index_of(ctx)["artifacts"]["9001"]
    assert (entry["verdict"], entry["reason"]) == ("rejected", "tar-members")
    assert not ctx.archive.latest.exists()
    assert not (ctx.archive.root / "ww3.db").exists()
    assert list(ctx.archive.work.iterdir()) == []


@pytest.mark.parametrize("members", [
    ["ww3-export.tar.gz.gpg", "second.gpg"],
    ["../ww3-export.tar.gz.gpg"],
    ["ww3-export.tar.gz"],
])
def test_an_artifact_zip_that_is_not_one_plain_gpg_member_is_rejected(tmp_path, keyring, members):
    gh = FakeGh()
    gh.publish(9001, 501, make_export(keyring, tmp_path / "build", 501, rows(3),
                                      zip_members=members),
               created="2026-11-09T04:21:00Z")
    ctx = make_ctx(tmp_path, gh, keyring)

    assert lab_archive.run(ctx, ["pull"]) == 1

    entry = index_of(ctx)["artifacts"]["9001"]
    assert (entry["verdict"], entry["reason"]) == ("rejected", "zip-members")
    assert list(ctx.archive.exports.iterdir()) == []


@pytest.mark.parametrize("override", [{"branch": "pr-head"}, {"head_repo": "someone/fork"},
                                      {"event": "pull_request"}])
def test_an_artifact_another_run_uploaded_is_ignored_unread(tmp_path, keyring, override):
    gh = FakeGh()
    gh.publish(9001, 501, make_export(keyring, tmp_path / "build", 501, rows(3)),
               created="2026-11-09T04:21:00Z", **override)
    ctx = make_ctx(tmp_path, gh, keyring)

    assert lab_archive.run(ctx, ["pull"]) == 1

    entry = index_of(ctx)["artifacts"]["9001"]
    assert (entry["verdict"], entry["reason"]) == ("ignored", "provenance")
    assert gh.count(f"api repos/{REPO}/actions/artifacts/9001/zip") == 0


def test_a_failed_decrypt_is_retried_rather_than_recorded(tmp_path, keyring):
    gh = FakeGh()
    gh.publish(9001, 501, make_export(keyring, tmp_path / "build", 501, rows(3)),
               created="2026-11-09T04:21:00Z")
    ctx = make_ctx(tmp_path, gh)  # no keyring: gpg cannot open it

    assert lab_archive.run(ctx, ["pull"]) == 1
    assert index_of(ctx)["artifacts"] == {}
    # The ciphertext is kept for when the key is back.
    assert (ctx.archive.exports / "2026-11-09-501.tar.gz.gpg").exists()


def test_a_missing_gpg_is_retried_rather_than_recorded(tmp_path, keyring):
    gh = FakeGh()
    gh.publish(9001, 501, make_export(keyring, tmp_path / "build", 501, rows(3)),
               created="2026-11-09T04:21:00Z")
    ctx = make_ctx(tmp_path, gh, keyring)
    ctx.gpg = str(tmp_path / "no-such-gpg")

    assert lab_archive.run(ctx, ["pull"]) == 1
    assert index_of(ctx)["artifacts"] == {}

    ctx.gpg = GPG
    assert lab_archive.run(ctx, ["pull"]) == 0
    assert index_of(ctx)["artifacts"]["9001"]["verdict"] == "ok"


def test_a_missing_gh_fails_before_any_work(tmp_path):
    def no_gh(argv):
        raise FileNotFoundError(argv[0])

    ctx = make_ctx(tmp_path, FakeGh())
    ctx.github.runner = no_gh

    assert lab_archive.run(ctx, ["pull"]) == 1
    status = json.loads(ctx.archive.status_path.read_text())["subcommands"]["pull"]
    assert status["ok"] is False
    assert "could not run" in status["problems"][0]


def test_an_export_that_was_never_encrypted_is_rejected(tmp_path, keyring):
    # gpg --decrypt also "opens" a message made with gpg --store, which no key
    # protects; in a public repository such an artifact is readable by anyone.
    zipped = make_export(keyring, tmp_path / "build", 501, rows(3))
    tarball = (tmp_path / "build" / "501" / "ww3-export.tar.gz")
    stored = tmp_path / "build" / "501" / "stored.gpg"
    _gpg(keyring.home, "--output", str(stored), "--store", str(tarball))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive_zip:
        archive_zip.writestr("ww3-export.tar.gz.gpg", stored.read_bytes())
    assert zipped != buffer.getvalue()
    gh = FakeGh()
    gh.publish(9001, 501, buffer.getvalue(), created="2026-11-09T04:21:00Z")
    ctx = make_ctx(tmp_path, gh, keyring)

    assert lab_archive.run(ctx, ["pull"]) == 1

    entry = index_of(ctx)["artifacts"]["9001"]
    assert (entry["verdict"], entry["reason"]) == ("rejected", "not-encrypted")
    assert not ctx.archive.latest.exists()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE,
                                  tarfile.FIFOTYPE])
def test_untar_refuses_a_link_or_special_member_by_an_expected_name(tmp_path, kind):
    tarball = tmp_path / "export.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        manifest = b"{}"
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
        link = tarfile.TarInfo("ww3.db")
        link.type = kind
        link.linkname = "manifest.json" if kind == tarfile.LNKTYPE else "/etc/passwd"
        tar.addfile(link)
    into = tmp_path / "into"
    into.mkdir()

    with pytest.raises(lab_archive.Rejected) as caught:
        lab_archive.untar_export(tarball, into)
    assert caught.value.code == "tar-members"
    assert not (into / "ww3.db").exists() and not (into / "ww3.db").is_symlink()


def test_only_the_newest_restored_copies_are_kept(tmp_path, keyring, monkeypatch):
    monkeypatch.setattr(lab_archive, "KEEP_RESTORED", 2)
    gh = FakeGh()
    for step in range(3):
        run_id = 501 + step
        gh.publish(9001 + step, run_id,
                   make_export(keyring, tmp_path / "build", run_id,
                               rows(3 + step, day=f"2026-11-0{6 + step}")),
                   created=f"2026-11-0{6 + step}T04:21:00Z")
    ctx = make_ctx(tmp_path, gh, keyring)

    assert lab_archive.run(ctx, ["pull"]) == 0

    restored = sorted(path.name for path in ctx.archive.restored.glob("ww3-*.db"))
    assert restored == ["ww3-2026-11-07-502.db", "ww3-2026-11-08-503.db"]
    assert len(list(ctx.archive.exports.glob("*.gpg"))) == 3
    assert os.readlink(ctx.archive.latest) == "ww3-2026-11-08-503.db"
    assert index_of(ctx)["artifacts"]["9001"]["restored"] is None


def test_pull_dispatch_waits_for_the_run_it_started(tmp_path):
    gh = FakeGh()
    clock = Clock()
    run = {"id": 777, "event": "workflow_dispatch", "status": "queued",
           "created_at": "2026-11-09T04:20:30Z"}
    listing = f"api repos/{REPO}/actions/workflows/ww3-export.yml/runs?event=workflow_dispatch&per_page=10"
    gh.route(f"workflow run ww3-export.yml --ref main --repo {REPO}", b"")
    # The new run appears only after the first poll, and finishes after the second.
    gh.route(listing, lambda: {"workflow_runs": [run] if clock.ticks >= 20 else []})

    def run_state():
        if clock.ticks >= 40:
            return {**run, "status": "completed", "conclusion": "success"}
        return {**run, "status": "in_progress"}

    gh.route(f"api repos/{REPO}/actions/runs/777", run_state)
    ctx = make_ctx(tmp_path, gh, clock=clock)

    assert lab_archive.run(ctx, ["pull", "--dispatch"]) == 0

    status = json.loads(ctx.archive.status_path.read_text())["subcommands"]["pull"]
    assert (status["dispatched_run"], status["dispatch_conclusion"]) == (777, "success")


def test_pull_dispatch_gives_up_after_fifteen_minutes(tmp_path):
    gh = FakeGh()
    clock = Clock()
    gh.route(f"workflow run ww3-export.yml --ref main --repo {REPO}", b"")
    gh.route(f"api repos/{REPO}/actions/workflows/ww3-export.yml/runs?event=workflow_dispatch&per_page=10",
             {"workflow_runs": []})
    ctx = make_ctx(tmp_path, gh, clock=clock)

    assert lab_archive.run(ctx, ["pull", "--dispatch"]) == 1
    assert clock.ticks == pytest.approx(15 * 60, abs=lab_archive.DISPATCH_POLL_SECONDS)
    # It still looked for exports already waiting.
    assert gh.count(ARTIFACTS) == 1


# -------------------------------------------------------------------- watch


def _healthy_watch_routes(gh: FakeGh, *, ingest_state="active", export_state="active"):
    gh.route(f"api repos/{REPO}/actions/workflows/ww3-ingest.yml/runs?per_page=1",
             {"workflow_runs": [{"id": 1, "status": "completed", "conclusion": "success",
                                 "created_at": "2026-11-09T03:50:00Z"}]})
    gh.route(f"api repos/{REPO}/actions/workflows/ww3-ingest.yml", {"state": ingest_state})
    gh.route(f"api repos/{REPO}/actions/workflows/ww3-export.yml", {"state": export_state})
    gh.route(f"workflow enable ww3-ingest.yml --repo {REPO}", b"")
    gh.route(f"workflow enable ww3-export.yml --repo {REPO}", b"")


def _seed_latest(ctx, source_rows):
    ctx.archive.prepare()
    db = make_db(ctx.archive.restored / "ww3-2026-11-09-501.db", source_rows)
    lab_archive.repoint_latest(ctx.archive, db)
    ctx.archive.save_index({"schema": lab_archive.INDEX_SCHEMA, "latest": "9001", "artifacts": {
        "9001": {"id": 9001, "run_id": 501, "created_at": "2026-11-08T04:21:00Z",
                 "verdict": "ok", "restored": f"restored/{db.name}",
                 "manifest": {"observations": len(source_rows)}},
    }})


def test_watch_re_enables_a_workflow_disabled_for_inactivity(tmp_path):
    gh = FakeGh()
    _healthy_watch_routes(gh, ingest_state="disabled_inactivity")
    ctx = make_ctx(tmp_path, gh)
    _seed_latest(ctx, [("usgs", "2026-11-09 02:00:00")])

    assert lab_archive.run(ctx, ["watch"]) == 1

    assert f"workflow enable ww3-ingest.yml --repo {REPO}" in gh.calls
    assert f"workflow enable ww3-export.yml --repo {REPO}" not in gh.calls
    assert "WORKFLOW-RE-ENABLED" in ctx.archive.log_path.read_text()
    assert "disabled for inactivity" in ctx.err.getvalue()


def test_watch_leaves_a_manually_disabled_workflow_alone(tmp_path):
    gh = FakeGh()
    _healthy_watch_routes(gh, export_state="disabled_manually")
    ctx = make_ctx(tmp_path, gh)
    _seed_latest(ctx, [("usgs", "2026-11-09 02:00:00")])

    assert lab_archive.run(ctx, ["watch"]) == 1
    assert not any(call.startswith("workflow enable") for call in gh.calls)


def test_watch_flags_a_stale_source_against_its_own_threshold(tmp_path):
    gh = FakeGh()
    _healthy_watch_routes(gh)
    ctx = make_ctx(tmp_path, gh)
    # usgs 40 h old (over its 36 h), bbc_world 40 h old (under the default 72 h).
    _seed_latest(ctx, [("usgs", "2026-11-07 12:20:00"), ("bbc_world", "2026-11-07 12:20:00")])

    assert lab_archive.run(ctx, ["watch"]) == 1
    status = json.loads(ctx.archive.status_path.read_text())["subcommands"]["watch"]
    assert status["stale_sources"] == ["usgs"]
    assert status["sources"]["usgs"] == pytest.approx(40.0)

    assert lab_archive.run(ctx, ["watch", "--source-stale-hours", "usgs=48"]) == 0


def test_watch_flags_an_old_archive(tmp_path):
    gh = FakeGh()
    _healthy_watch_routes(gh)
    ctx = make_ctx(tmp_path, gh, clock=Clock(NOW + timedelta(days=9)))
    _seed_latest(ctx, [("usgs", "2026-11-18 02:00:00")])
    gh.route(f"api repos/{REPO}/actions/workflows/ww3-ingest.yml/runs?per_page=1",
             {"workflow_runs": [{"id": 1, "status": "completed", "conclusion": "success",
                                 "created_at": "2026-11-18T03:50:00Z"}]})

    assert lab_archive.run(ctx, ["watch"]) == 1
    status = json.loads(ctx.archive.status_path.read_text())["subcommands"]["watch"]
    assert status["problems"] == ["newest verified export is over 8 days old"]


# ---------------------------------------------------------- dispatch-ingest


INGEST_RUNS = f"api repos/{REPO}/actions/workflows/ww3-ingest.yml/runs?per_page=10"
INGEST_DISPATCH = f"workflow run ww3-ingest.yml --ref main --repo {REPO}"


@pytest.mark.parametrize("status", ["in_progress", "queued"])
def test_dispatch_ingest_skips_while_a_run_is_active(tmp_path, status):
    gh = FakeGh()
    gh.route(INGEST_RUNS, {"workflow_runs": [
        {"id": 2, "status": status, "created_at": "2026-11-09T03:00:00Z"},
        {"id": 1, "status": "completed", "created_at": "2026-11-09T02:00:00Z"},
    ]})
    gh.route(INGEST_DISPATCH, b"")
    ctx = make_ctx(tmp_path, gh)

    assert lab_archive.run(ctx, ["dispatch-ingest"]) == 0
    assert INGEST_DISPATCH not in gh.calls


def test_dispatch_ingest_skips_a_run_created_minutes_ago(tmp_path):
    gh = FakeGh()
    gh.route(INGEST_RUNS, {"workflow_runs": [
        {"id": 1, "status": "completed", "created_at": "2026-11-09T04:05:00Z"}]})
    gh.route(INGEST_DISPATCH, b"")
    ctx = make_ctx(tmp_path, gh)

    assert lab_archive.run(ctx, ["dispatch-ingest"]) == 0
    assert INGEST_DISPATCH not in gh.calls


def test_dispatch_ingest_dispatches_when_nothing_is_due(tmp_path):
    gh = FakeGh()
    gh.route(INGEST_RUNS, {"workflow_runs": [
        {"id": 1, "status": "completed", "created_at": "2026-11-09T03:30:00Z"}]})
    gh.route(INGEST_DISPATCH, b"")
    ctx = make_ctx(tmp_path, gh)

    assert lab_archive.run(ctx, ["dispatch-ingest"]) == 0
    assert gh.calls == ["api rate_limit", INGEST_RUNS, INGEST_DISPATCH]


@pytest.mark.parametrize("argv", [["pull"], ["watch"], ["dispatch-ingest"]])
def test_a_low_rate_limit_stops_before_any_work(tmp_path, argv):
    gh = FakeGh(remaining=250)
    ctx = make_ctx(tmp_path, gh)

    assert lab_archive.run(ctx, argv) == 1
    assert gh.calls == ["api rate_limit"]
    status = json.loads(ctx.archive.status_path.read_text())["subcommands"][argv[0]]
    assert status["ok"] is False


# ----------------------------------------------------------------- workflow


def test_the_export_workflow_encrypts_to_every_key_it_pins(keyring):
    workflow = (ROOT / ".github" / "workflows" / "ww3-export.yml").read_text()
    recipients = re.findall(r"--recipient-file (\S+)", workflow)
    pinned = dict(re.findall(r"(?m)^\s+\w+_KEY: (\S+)\n\s+\w+_FPR: ([0-9A-F]{40})$", workflow))
    assert sorted(recipients) == sorted(pinned) and len(recipients) == 2
    for path, fingerprint in pinned.items():
        listing = _gpg(keyring.home, "--with-colons", "--show-keys", str(ROOT / path)).stdout
        first = next(line for line in listing.decode().splitlines() if line.startswith("fpr:"))
        assert first.split(":")[9] == fingerprint
