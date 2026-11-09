# The export archive

The collector's ledger lives in one place on GitHub: the newest `ww3-db-*`
Actions cache, which GitHub deletes once nobody has read it for 7 days. The
archive is the durable history. Every export the `ww3-export` workflow makes is
collected, decrypted, checked and kept on the lab Mac, which is the archive's
home. This file is its runbook.

The databases hold publishers' text (headlines, feed summaries). The archive
keeps them privately; nothing in it is published or redistributed, and every
line it prints or logs is a count, an identifier or a timestamp.

A verified copy means one narrow thing: the database is byte for byte the one
the export job described in its manifest, and GitHub records the upload as
this repository's own export workflow on `main`. It does not mean the
observations are true or complete, or that collection was healthy while they
were gathered. A blocked feed and a quiet one look the same inside a database;
`watch`'s staleness report exists for that.

## Where it lives

On the lab Mac, under one root: `WW3_ARCHIVE_ROOT`, default `~/ww3-archive`,
mode 700.

```text
<root>/
  gnupg/        the archive keyring: the lab key's secret half and nothing else
  exports/      every ciphertext, <UTC date>-<run id>.tar.gz.gpg, mode 0600, never pruned
  restored/     verified databases, ww3-<last fetch date>-<run id>.db, mode 0600;
                the newest 14 are kept
    latest.db   symlink to the newest verified copy, swapped in one rename
    suspect/    fork-suspected databases, never pruned
  index.json    one entry per artifact seen: ids, digests, manifest summary, verdict
  status.json   the last result of each subcommand
  logs/         lab-archive.log (one line per event) and launchd's stdout and stderr
```

Each export is a full copy of the cumulative, append-only ledger, so a missed
export loses no observation by itself: the next one carries everything the
last one did. What a gap risks is a ledger lost or forked meanwhile with no
copy taken.

## The two keys

| key file | fingerprint | role |
|---|---|---|
| `ops/ww3-export-lab.pub.asc` | `1D9E F065 69F9 4BFE B1C0  0D80 66EF A0B2 F44A 4CE0` | The archive key: ed25519 with a cv25519 encryption subkey, created on the lab Mac on 2026-11-08, without a passphrase. Its secret half exists only in `<root>/gnupg` and in the offline backup described below. |
| `ops/ww3-export.pub.asc` | `5D93 7004 D085 4C78 CD6E  9DFE AE98 62B4 C7F1 26D5` | The legacy key, created on another machine on 2026-10-22. Who holds its secret half is unconfirmed. It stays a recipient so that whoever does can still open new exports; remove it (the file, its `--recipient-file` and its pinned fingerprint, in one PR) once its holder is confirmed or the key is retired. |

`ww3-export.yml` pins both fingerprints and stops before restoring anything if
either key file is missing or holds a different key.

Encryption gives confidentiality only. Both public halves are public, so a
ciphertext the lab key opens says nothing about who made it. What ties an
artifact to this collector is GitHub's record of the run that uploaded it,
which every pull checks first.

The lab key has no passphrase because an unattended job cannot type one. What
protects it is the root's 0700 mode and whatever protects the lab Mac's disk
and user session: anyone with that session can read every export.

## What runs on the lab Mac

| launchd label | when | what |
|---|---|---|
| `dev.ww3-intelligence.lab.archive-pull` | daily, 04:20 local | `pull --dispatch`, then `watch` (`ops/lab/archive-daily.sh`) |
| `dev.ww3-intelligence.lab.dispatch-ingest` | every 30 minutes | `dispatch-ingest` |

```bash
ops/lab/install.sh             # copy, render and load both jobs (run again to reload)
ops/lab/install.sh --no-load   # copy and render only, to read the plists first
ops/lab/uninstall.sh           # unload and remove the jobs; the archive is untouched
```

`install.sh` copies `ops/lab_archive.py` and the wrapper to
`~/.local/libexec/ww3/` (`WW3_LIBEXEC`), so the jobs never run code from a
checkout that a branch switch could change. Its header lists the other
settings. By hand, run the installed copy:

```bash
python3.13 -I ~/.local/libexec/ww3/lab_archive.py pull
python3.13 -I ~/.local/libexec/ww3/lab_archive.py watch
```

Both jobs are LaunchAgents, so they run only while the archive's user is
logged in. A Mac asleep at 04:20 runs the daily job once on waking; a Mac that
is off or logged out runs nothing, and the weekly scheduled export is then the
floor. `gh` must be signed in with a token that can dispatch and enable this
repository's workflows; for a classic token that is the `repo` scope. If `gh`
keeps that token in the login keychain, a LaunchAgent reaches it only while
that keychain is unlocked. When `gh` cannot authenticate, the rate-limit check,
which is each subcommand's first GitHub call, fails, and `status.json` says so.

## How a pull verifies

1. The GitHub API's core allowance must have more than 300 requests left, or
   nothing else happens. This holds for every subcommand that calls GitHub.
2. With `--dispatch`, it requests one export on `--ref` (default `main`) and
   waits for that run: the run `gh` names, or else the first
   `workflow_dispatch` run created after the request (allowing 60 s of clock
   difference), polled about every 20 s for at most 15 minutes. A failed or
   slow export is reported, and the pull still collects what is waiting.
3. It lists the unexpired `ww3-export-*` artifacts and skips every one already
   in `index.json`.
4. Provenance: the run that uploaded the artifact must be
   `.github/workflows/ww3-export.yml`, triggered by `schedule` or
   `workflow_dispatch`, on `main` (or the `--ref` given), in this repository
   with this repository as its head. Anything else is `ignored` and never
   downloaded.
5. The zip (at most 64 MiB) is downloaded. Its SHA-256 is recorded, and so is
   whether it matches the digest GitHub reports; that comparison is recorded,
   not enforced, because whether GitHub serves the uploaded zip byte for byte
   has not been verified here.
6. The zip must hold exactly one member, a plain `*.gpg` file name of at most
   64 MiB. That ciphertext is stored in `exports/` before anything else
   happens, and kept whatever follows.
7. It is decrypted with the archive keyring into a 0700 working directory
   under the root, with gpg's output capped at 2 GiB and no passphrase prompt
   allowed. gpg must report a message encrypted to public keys, opened with
   the archive key, whose integrity check passed. `gpg --decrypt` also
   succeeds on a message that was never encrypted; such an export is
   rejected, because in a public repository it was readable by anyone.
8. Only `ww3.db` and `manifest.json` are extracted, as regular files, by bare
   name. Any other member, link, path or duplicate rejects the export.
9. The manifest must be `ww3-export/1` and name the same run.
10. The database must match it: SHA-256 equal to `db_sha256`, size equal to
    `db_bytes`, `PRAGMA integrity_check` answering `ok` on a read-only
    connection, `COUNT(*)` equal to `observations` and `MAX(id)` equal to
    `max_id`.
11. Continuity: neither the observation count nor the max id may fall below
    the last accepted export before it. If either does, the export is
    `fork-suspected`: its database is kept in `restored/suspect/`, `latest.db`
    stays where it was, and the pull exits non-zero.
12. A verified database moves to `restored/` and becomes `latest.db` if it is
    the newest. The newest 14 restored copies are kept; every ciphertext is.

It never deletes a GitHub artifact. Another collector may still need one, and
each expires 7 days after upload.

| verdict | meaning | tried again? |
|---|---|---|
| `ok` | verified and kept | no |
| `ok-reset` | was fork-suspected; a person accepted it as a new ledger | no |
| `fork-suspected` | verified, but the ledger shrank | no; see below |
| `rejected` | its content failed a check (zip, encryption, tar, manifest, digest, integrity, counts) | no |
| `ignored` | not uploaded by this repository's export workflow on `main` | no |
| (none) | the download, `gh` or `gpg` failed | yes, at every pull while the artifact lasts |

Every verdict other than `ok` makes that pull exit non-zero once.

## Restore by hand

The archive's own checks can be repeated without the script:

```bash
ARCHIVE=~/ww3-archive
export GNUPGHOME="$ARCHIVE/gnupg"
work="$(mktemp -d "$ARCHIVE/manual.XXXXXX")" && cd "$work"
gpg --decrypt "$ARCHIVE/exports/2026-11-09-<run id>.tar.gz.gpg" | tar -xzf - ww3.db manifest.json
shasum -a 256 ww3.db
python3 -c 'import json; m = json.load(open("manifest.json")); print(m["db_sha256"], m["observations"], m["max_id"])'
sqlite3 'file:ww3.db?mode=ro' 'PRAGMA integrity_check;'
sqlite3 'file:ww3.db?mode=ro' 'SELECT COUNT(*), MAX(id) FROM observation;'
```

The digest, the two counts and `ok` must agree with the manifest. To read the
database with this repository's CLI, copy it and point `DB_PATH` at the copy:
the CLI may migrate a schema it opens, and an archived file should stay the one
its manifest describes. Remove `$work` afterwards.

There is no path that puts an archived database back into the Actions cache.
The cache can only be written from inside a workflow run, and nothing here
takes a database in. After a loss the archive holds the history and a new
ledger starts on GitHub (below); joining the two is a later decision.

## Back up the lab key, offline

This is done by a person at the lab Mac. No agent or script runs it, and the
key never goes into this repository, a chat, an issue, or any cloud storage or
synced folder.

```bash
export GNUPGHOME=~/ww3-archive/gnupg
export GPG_TTY="$(tty)"   # the passphrase prompt needs the terminal; stdin is a pipe
gpg --armor --export-secret-keys 1D9EF06569F94BFEB1C00D8066EFA0B2F44A4CE0 \
  | gpg --symmetric --armor --output /Volumes/<offline medium>/ww3-archive-key.asc.gpg
```

The key has no passphrase, so an unwrapped export would be the key itself.
The second `gpg` asks for a passphrase to wrap it; write that passphrase down
and keep it apart from the medium. Then prove the backup opens an export,
because a backup never restored is a hope:

```bash
drill="$(mktemp -d)"
gpg --decrypt /Volumes/<offline medium>/ww3-archive-key.asc.gpg | GNUPGHOME="$drill" gpg --import
GNUPGHOME="$drill" gpg --decrypt ~/ww3-archive/exports/<one export>.tar.gz.gpg | tar -tzf -
gpgconf --homedir "$drill" --kill all && rm -rf "$drill"
```

The listing should show `ww3.db` and `manifest.json`. `exports/` holds only
ciphertext, so copying it off the lab Mac is the cheap way to let the archive
outlive the Mac; the key backup is what makes such a copy readable.

## A cache miss, and a deliberate reset

Ingest restores the ledger with `fail-on-cache-miss`. A run that finds no
`ww3-db-*` cache fails rather than collecting into an empty database and
saving it as the newest key, which every later run and export would have
restored while the real ledger aged out unread. A run of red ingests is the
signal. The usual causes are eviction after the schedules stopped (next
section), a cache-service failure, or a cache deleted by hand.

If the cache is gone, so is the ledger on GitHub; the archive holds it up to
the last pull. The `start_new_ledger` input of the ingest workflow's manual
dispatch is the one deliberate way to begin again. History stays in the
archive; it is not carried into the new ledger.

The first export after a reset is smaller than the last accepted one, so the
export job warns against `ledger/status.json`, and the pull marks it
`fork-suspected`, keeps it in `restored/suspect/` and leaves `latest.db` on the
old ledger. Once a person has confirmed the reset was deliberate, by finding
the ingest run that used `start_new_ledger`, they accept it:

```bash
python3.13 -I ~/.local/libexec/ww3/lab_archive.py accept-reset <export run id>
```

Later exports are compared against the accepted one. `accept-reset` works on
files already in the archive and makes no GitHub call.

## The 60-day rule

GitHub documents that in a public repository, scheduled workflows are disabled
automatically when no repository activity has occurred in 60 days. Both
workflows would then read `disabled_inactivity`, a disabled workflow cannot be
dispatched either, nothing would read the cache, and 7 days later it and the
ledger would be gone. Ingest commits nothing, so it never resets that clock.

Two mitigations, because neither is proven alone:

- Scheduled exports commit `ledger/status.json` to `main`, once a week. GitHub
  does not define repository activity. A push to the default branch is the
  plainest kind, but whether a push made with `GITHUB_TOKEN` counts is not
  documented, so this is a likely guard rather than a verified one. Any merged
  pull request resets the clock too.
- `watch` reads both workflows' state every day and runs `gh workflow enable`
  on any that GitHub disabled for inactivity. It logs `WORKFLOW-RE-ENABLED`,
  warns on stderr and exits non-zero, so it is seen. This works only while the
  lab Mac runs. A workflow disabled by hand (`disabled_manually`) is reported
  and left alone.

The daily dispatched export also reads the cache, and a read is what keeps a
cache entry from eviction.

## Cadence

Between 2026-09-01 and 2026-11-08 GitHub delivered about 5.8 runs a day of the
48 that the `*/30` cron asks for, with a median gap of about 4 hours and a
longest of 9.7. GitHub documents that scheduled events can be delayed or
dropped under load. Feeds whose window is shorter than such a gap lose items
for good.

`dispatch-ingest` asks for a run every 30 minutes from the lab Mac: one list
call, and one dispatch only when no ingest run is queued or running and the
newest is at least 20 minutes old. The `*/30` cron stays as the fallback for
when the lab Mac is asleep, off or logged out. Dispatch events are not
documented as dropped the way schedule events are, but their delivery has not
been measured here yet; `watch` flags any 6 hours without an ingest run.

## What `watch` reports

- The newest ingest run: its age and conclusion. Flagged when none was
  created in 6 hours (`--ingest-max-age-hours`) or the newest did not succeed.
- Each workflow's state, re-enabling as above.
- For each source in `restored/latest.db`, the hours since its newest
  `fetched_at`. Flagged over 72 hours (`--stale-hours`), or 36 for `usgs`;
  `--source-stale-hours SOURCE=HOURS` or `SOURCE=off` changes one source.
  A record is stored once per content revision, so this is the time since the
  collector last captured something new from that source. A source that
  publishes rarely is stale here while collection is healthy; a blocked one
  looks the same. The hours include the archive's own lag, because
  `latest.db` is only as fresh as the last pull, up to about a day.
- The newest verified export's age, flagged over 8 days
  (`--export-max-age-days`), and how many exports hold each verdict.

## What the archive does not do

- It is one Mac. It is neither off-site nor redundant unless `exports/` is
  copied elsewhere.
- It alerts no one. Problems reach a person through the exit status,
  `status.json` and the logs.
- It does not repair a ledger, merge a fork, or put history back on GitHub.
- It does not merge the retired Fly runtime's ledger (2026-07-17 to
  2026-09-01) into the Actions ledger. That ledger sits apart under `fly-era/`
  in the archive root, copied from the Fly volume and checked against it before
  the app was deleted on 2026-11-09; `pull` and `watch` never touch it.
