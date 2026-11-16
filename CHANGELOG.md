# Changelog

## 0.1.11 — 2026-11-13

Small fixes from a review of how collected records are processed. None of them
changes how a record's stored text, source record id, content hash or dedupe
key is computed, so nothing already stored is re-filed as a revision.

- `feed --watch` fetches every enabled news feed each cycle again and stores
  each one that answers. Since 2026-07-17 its capture wrapper stored a batch
  and then dropped the result, so the collector loop failed when it read the
  batch's counts. Each news cycle stored at most the first feed that answered
  (normally `bbc_world`), never fetched the feeds after it, and skipped the
  aggregate `news` heartbeat. Every USGS and GDACS cycle stored its batch but
  printed `cycle failed (AttributeError)` and recorded no record count. The
  wrapper now returns the batch. The scheduled `ingest` on Actions and
  `python -m core.collector` never used this wrapper. The earlier tests
  replaced the wrapper with a mock that answers any attribute, which is how
  this went unnoticed; the new ones drive the real capture and replace only
  the fetch.
- A feed's body is read as the document it is. feedparser opens bytes as a file
  name when it can, so a 200 body that was exactly a path on the collector
  (absolute, or relative to the working directory, which is the checkout on
  Actions) would have been replaced by that file, read with no byte bound and
  outside the fetch's deadline. No such answer is known to have reached the
  ledger. The body now reaches feedparser as a stream.
- A feed document that ends before it is finished is a failed fetch, with the
  reason `incomplete document: <media type>, <n> bytes` once feedparser has
  recovered an entry from it; one cut before its first item or entry was
  already refused as `not a feed`. Before, a body cut short but framed as
  complete (a clean chunked end, a Content-Length equal to the cut, a dropped
  connection with no framing) was accepted whenever feedparser recovered an
  entry from it. The cut last entry would become a revision of a known item
  with its text gone, which would stay the newest revision after the full text
  returned; or a record filed under its link, when its guid came after the
  cut; or a record with no id. Whether any such answer reached the ledger is
  not known. Now nothing from such an answer is stored, and the next run reads
  the feed again. Two limits. The check reads the error at which feedparser's
  strict parser stopped, and that parser stops at its first error, so a cut is
  not caught in a document whose strict parse had already failed for another
  reason (for example a bare `&`, an undefined entity such as `&nbsp;`, an
  undeclared prefix, whitespace before `<?xml`, a mismatched end tag, or a
  DOCTYPE that feedparser's own rewriting breaks), and it is stored as before;
  whether any enabled feed is in that class is not known. And a document whose
  root is complete but which ends inside a trailing comment or tag is refused
  for that run. Whether any enabled feed is served unfinished on every answer
  is not known; such a feed would now be refused in every run without failing
  the run. Like any failed feed, it would be named, as `incomplete document`,
  in each run's warnings and step summary, and none of that is alerted on. If
  every enabled feed's answer is refused or fails, the news step and so the
  run fail, as for any run in which every feed fails; before, the cut records
  were stored and the run passed.
- Each ingest step has its own time limit: USGS 2 minutes, the news feeds 10,
  GDACS 3, inside a job limit of 20 instead of 15. A fetch's own timeouts do
  not bound a whole answer, so an endpoint that kept sending slowly could hold
  its step until the job timed out. A job that times out is cancelled before
  the guard and the save, so every row that run had captured, from every
  source, would be lost. A step that reaches its own limit now fails like any
  failed source: the guard and the save still run, the run fails for USGS or
  the news feeds, and it warns for GDACS. A step stopped that way writes no
  counts and names no failed or empty feed; the runner's timed-out error names
  the step. This is read from the runner's source and replayed locally;
  GitHub documents only that the step's process is killed, and no step has
  reached its limit on a GitHub runner. A GDACS stall used to end as a
  cancelled run, which the lab's `watch` flags; it now ends as a successful
  run with a warning, so only that source's staleness shows it.
  `ingestion/http.py` is unchanged, so a fetch's own deadline still does not
  hold during a read, and the news limit is a margin over what has been seen,
  not a bound on eleven slow feeds. In 100 runs from 11 to 13 November 2026
  the slowest steps took 2 s, 12 s and 20 s.
- Each ingest step's command replaces its shell (`exec`). The runner stops a
  step by signalling the shell alone. Without `exec` the collector under it
  would have kept writing to the ledger while the next source, the guard and
  the save ran, and a second writer can make the other's insert fail with
  `database is locked`. This is read from the runner's source and replayed
  locally; it has not been observed on a GitHub runner.
- Observations refuse REPLACE as well as UPDATE and DELETE. SQLite's REPLACE
  removes the conflicting row without firing a delete trigger unless a
  connection turns `recursive_triggers` on, so `Observation.replace()`,
  `on_conflict_replace()` or a raw `INSERT OR REPLACE` could rewrite a stored
  observation. No code here issued one. A new `observation_no_replace` trigger
  refuses any insert that collides with a stored row on id or dedupe key, on
  every connection to the file. An existing ledger gets the trigger the next
  time this code initialises it, which every command that opens the ledger
  does, one that only reads it such as `observations` included; for the
  Actions ledger that is its next ingest run. That first open writes the
  trigger, so it changes the file's bytes: point the CLI at a copy of an
  archived database, as `ops/ARCHIVE.md` says. A script outside this
  repository could notice two things:
  `INSERT OR IGNORE` and `ON CONFLICT DO NOTHING` of a duplicate now raise
  instead of passing silently, and a plain duplicate insert fails with
  `observations are append-only` rather than `UNIQUE constraint failed`.
- Opening an older local ledger no longer builds a wrong index. This applies to
  a ledger whose observation table predates revision links (a prototype's;
  v0.1.0 already had them), or whose prediction table holds rows written by
  code from before 2026-07-26, when forecast provenance arrived. `init_db`
  created every index before adding the missing column, and SQLite indexed the
  quoted column name as a string constant: `quick_check` passed,
  `integrity_check` failed, and on the prediction table `ww3.py resolve`
  stopped with "database disk image is malformed". Tables are now created
  first, then the added columns, then the indexes. A new ledger gets the same
  tables, indexes and triggers as before plus the new trigger; only their
  order in `sqlite_master` differs. The Actions ledger started empty on
  2026-09-01 with both columns, so it was not exposed.
- A ledger of that kind that earlier code has already opened keeps the damage;
  this release does not repair it, and startup's `quick_check` does not see
  it. If `PRAGMA integrity_check` names `observation_supersedes_id` or
  `prediction_analysis_run_id`, run
  `REINDEX observation_supersedes_id; REINDEX prediction_analysis_run_id;`
  once. That rebuilds the two indexes from the rows and changes no row. It does
  change the file's bytes, so run it on a working copy, never on an archived
  database.

## 0.1.9 — 2026-11-09

- The retired Fly app and its volume were deleted on 2026-11-09, after the
  volume's ledger (12,226 observations, 2026-07-17 to 2026-09-01) was copied to
  the lab Mac, matched against the file on the volume by SHA-256, and passed
  `PRAGMA integrity_check`. README, AGENTS.md, CLAUDE.md and ops/ARCHIVE.md no
  longer call that clean-up pending. The Fly-era ledger stays a separate
  database; it is not merged into the Actions ledger.

## 0.1.8 — 2026-11-08

- The lab Mac is now the home of the export archive. `ops/lab_archive.py`
  collects every encrypted export, keeps the ciphertext, decrypts it with the
  archive key, and checks the database against its manifest: digest,
  `PRAGMA integrity_check`, row count, and max id. An export whose ledger is
  smaller than the last accepted one is kept aside as fork-suspected and never
  becomes the latest copy. LaunchAgents in `ops/lab/` run the collection daily
  and request an ingest run every 30 minutes; `ops/ARCHIVE.md` is the runbook.
  Until now nothing showed that any export had been collected, decrypted, or
  restored.
- Exports are encrypted to the lab archive key as well as the legacy key of
  2026-10-22, whose holder is unconfirmed. The workflow pins both fingerprints
  and stops if either key file is missing.
- Scheduled exports commit `ledger/status.json` (counts, id range, fetch-time
  span, database digest; no publisher text) to `main`. GitHub disables a public
  repository's schedules after 60 days without activity, and nothing else here
  committed; the lab's daily `watch` also turns a workflow disabled that way
  back on.
- An artifact counts as an export only if GitHub records it as uploaded by this
  repository's own export workflow on `main`. The archive key's public half is
  public, so a ciphertext the key opens says nothing about who made it. An
  export gpg can read without any key (`gpg --store`) is rejected: in a public
  repository that artifact was readable by anyone.
- Remove the GitHub Pages workflow. Project Pages is off, probably since about
  2026-09-25 (a date inferred, not read from GitHub), and the workflow has
  failed on every push since it went off; the public documentation is a
  reviewed copy served from cambridgetcg.github.io.
- The documentation no longer describes the Fly runtime or its dashboard as
  live. It was retired on 2026-09-01 and clean-up of its app and volume is
  pending; the ledger it held before that date was never carried over.
- `ww3-export.yml` moves to `actions/checkout@v7`, `actions/cache/restore@v6`,
  and `actions/upload-artifact@v7`.

A verified archive copy is the database the export job described, byte for
byte and row for row. It is not evidence that the observations are true or
complete, or that every source was being collected while they were gathered.

## 0.1.7 — 2026-11-08

- Read USGS up to the present moment. The query sent whole dates, so its window
  ended at 00:00 UTC today and nothing from the current UTC day could be read
  until the first run after the next midnight; on 2026-11-08 the ledger held
  none of that day's ten M4+ events. The scheduled run now asks for up to 1,000
  events: the old 200 was reached in 78 of 199 logged runs between 2026-10-11
  and 2026-10-31, which cut the oldest part of the seven-day window.
- Read each source to its own bound. `--limit` now bounds the USGS query
  (default 200), each GDACS query (100) or each enabled news feed (default 100,
  at most 500). A shared 200 used to be split evenly across the feeds, about 18
  entries each with eleven, so a long feed lost the rest of its items on every
  run while a short feed's share went unused. The scheduled run reads up to 500
  from each feed, which reads every current feed in full. Entries never fetched
  before are captured once, as first-seen: they are records first seen late,
  not new publications. The watcher's total of 200 a cycle now passes a short
  feed's unused share on to the feeds after it.
- Count an answer that is not a feed as a failed fetch. Each feed is `ok` (at
  least one record), `empty` (a well-formed feed with no items) or `failed`,
  with a short reason: a network or HTTP error, a refused redirect, a body that
  is not a feed, or entries none of which could be used. The Russian ministry's
  address answered each of four requests on 2026-11-08 with a 301-byte "Request
  Rejected" page served as 200 text/html, and run totals suggest it did so in
  most runs, each reported as a success. Failed feeds now count against "fetches
  succeeded"; the command still exits 1 only when every feed fails. The 0.1.6
  line for feeds that answered with no records names only well-formed empty
  feeds; failed ones get their own line. A failed USGS or GDACS fetch is a
  one-line error instead of a traceback. A reason is built from a status, a
  media type and a byte count, never from the body.
- Under GitHub Actions, warn for each failed or empty feed and for a failed
  USGS or GDACS fetch, and write each source's received, first-seen, revision,
  unchanged and invalid counts to the job summary. Counts and this code's own
  reasons only, no publisher text. A failed or empty row is not evidence that
  nothing was published.
- Ask GDACS for current events, in two queries. The adapter sent only `limit`,
  which GDACS's list does not take, so it received the default list: on
  2026-11-08, 82 Orange and Red events whose end dates reached back a year, none
  of them current. The first scheduled GDACS run captured exactly those 82. They
  stay in the ledger, which is append-only, as what that query returned that
  day. Each run now reads the first page, at most 100 events, newest end date
  first, of each of two queries:
  - Every alert level with an end date in the last three days. On 2026-11-08
    this filled all 100 places with events whose end date fell in the previous
    22 hours, 89 of them Green wildfire alerts, and a second page held 100 more,
    all Green; on 2026-11-09 it was again 100 Green events from the previous
    day. So it covers about the newest day rather than three, and an older
    event is not read through it whatever its alert level: three Orange drought
    alerts with an end date inside the window were on neither page.
  - Orange and Red alerts with an end date in the last 30 days, which reads
    those. On 2026-11-09 it returned 10 (7 Orange, 3 Red), none of them current,
    with end dates between 30 and three days back, among them three Orange
    droughts; no event was on both pages.

  An event both queries return is stored once, and its identity and stored
  text come from the event alone, so the page it came from cannot by itself
  file a revision: four Orange and Red events that a third, wider query also
  returned on 2026-11-09 came back byte for byte the same. A full page
  means GDACS may hold more matches than were read; the run names each full
  page, in its output and in the job summary, rather than comparing the merged
  count to the limit. If either query fails, the GDACS fetch fails for that run.
  A query nothing matches is not a failure: GDACS answers it with 204 and no
  body, as it did on 2026-11-09 for Orange and Red over one day, so it is read
  as an empty page and a quiet month of Orange and Red alerts does not cost
  the recent page. That the window applies to the end date is observed, not
  documented.
- Keep the time of day in IAEA's dates. IAEA writes "26-11-06  14:46", with no
  zone; feedparser dropped the time, so every IAEA item was stored at 00:00 UTC
  on its date. The time is now kept, read as UTC as every zoneless time is
  here, and written without an offset, beside the publisher's own string
  (`published_as_served`). If IAEA means Vienna time, the stored times are one
  or two hours late. Each IAEA item still in its feed is filed once as a
  revision (15 on 2026-11-08). No other feed's stored text changed in a replay
  of every entry the nine other answering feeds served that day: each was
  captured with the previous code, then again with this release, and every one
  was known unchanged. The Russian ministry's feed was blocked, so none of its
  entries could be replayed.
- Keep the scheduled ingest's ledger (`ww3-ingest.yml`):
  - A cache miss fails the run before anything is collected. Until now a miss
    would have started an empty ledger and saved it as the newest entry, and
    every later run and export would have built on that while the real one aged
    out. A manual run with `start_new_ledger` is the deliberate way to begin
    one; it only permits a miss. A restore that fails for any other reason
    looks the same as a miss, so tick it only when the repository's cache list
    holds no `ww3-db-` entry.
  - The ledger is checked before it is saved: SQLite's integrity check, and an
    observation count and highest id no lower than what was restored. One that
    fails is not saved.
  - Every source runs whatever the others did, and a checked ledger is saved
    even when one failed. The run then fails if USGS or the news feeds failed;
    GDACS may fail alone and leaves a warning.
  - The job holds a read-only token. The event-stream commit step is gone: the
    file is gitignored, so it never committed anything. The job installs
    `requirements-ingest.lock`, exactly what ingest imports, pinned with hashes
    at the versions production resolved on 2026-11-08, so installing them
    changes no content hash.
- Test on Python 3.11, which the scheduled ingest runs, as well as 3.14, and
  check in CI that the ingest lock alone covers the ingest path.

**Upgrading:** `ingest news --limit N` meant N records across all feeds; it now
means up to N from each feed. `ingest gdacs --limit N` now means up to N from
each of its two queries, so up to 2N records. A `--limit` above a source's own
bound (1,000 for USGS, 100 for each GDACS query, 500 for a feed) is refused
rather than silently cut.

## 0.1.6 — 2026-11-08

- `ingest news` prints one line per feed that answered (records received and,
  outside a dry run, captured) and names every feed that answered with no
  records. Until now a feed returning a challenge page or an empty document
  counted as a success inside the total.

## 0.1.5 — 2026-11-08

- Collect two of the publishers registered in 0.1.3: the Turkish Ministry of
  Foreign Affairs (its Turkish statements feed) and the German Federal Foreign
  Office (its German press releases and speeches). Each endpoint was checked
  against the publisher's own site, fetched with the collector's own code, and
  its item links matched against the canonical hosts it carries.
- Collect GDACS alerts again. The scheduled ingest has run only USGS and the
  news feeds since it moved to GitHub Actions on 2026-09-01, so no GDACS record
  was captured after that date. The step runs after the news feeds and may fail
  on its own without stopping them.
- Record NHK's verified feed (its Japanese international category on
  `news.web.nhk`, where NHK's own feeds now live) but keep it off: NHK's RSS
  terms allow personal use only, with no re-provision by programs, and its
  robots.txt asks AI crawlers to stay away. Whether this ledger fits is a human
  decision.
- Upgrade the Turkish ministry's `http://` item links to `https` on its own
  hosts, the way the IAEA and Russian ministry links already are; each https
  form was fetched and answered without a redirect.
- Reachability was shown only from outside the collector's network. The first
  runner collections must confirm each feed answers there.
- Still off, each for its own reason:
  - `china_mfa`: no feed the ministry offers; its only listing is JSON, which
    would need a new adapter.
  - `iran_mfa`: no feed found, and every host answers a cookieless client with
    a cookie challenge the collector cannot pass.
  - `israel_mfa`: held; gov.il refuses automated clients, so whether an MFA
    feed exists could not be seen.
  - `india_mea`: no feed; every feed path ends at an error page.
  - `ukraine_mfa`: held; the site refuses automated clients, so whether a feed
    exists could not be seen.
  - `japan_mofa`: held; the only official feed carries consular safety notices,
    not diplomatic statements, and enabling it would mislabel the entry.
  - `saudi_mofa`: held; no feed found, and the host does not answer from US
    networks.
  - `france_diplomatie`: no feed on the relaunched site; the legacy feeds are
    refused.
  - `kcna`: no feed on the agency's own domain, and it serves no working TLS.
  - `aljazeera_arabic`: held; every pubDate is UTC clock time labelled +0300,
    so each record would be dated three hours early.
  - `lemonde`: held; the feed's terms restrict it to personal use, and whether
    this collector fits them is not ours to decide.
  - `xinhua`: no live feed; Xinhua's own files stopped in December 2022.

A registry entry is configuration, not authentication: an enabled feed says
where to look, and each record still has to match the hosts it names before it
counts as the publisher speaking.

## 0.1.4 — 2026-08-03

- Match an alias in whichever canonically equivalent spelling a publisher
  served, against the text as stored, so a decomposed record is no longer
  invisible (`ww3.actor-mentions/v3`). Each mention names the spelling that
  matched. Stored content and its identity are untouched: the alias moves to
  meet the text, never the reverse.
- Report rhetoric density on a basis the passage's script can bear, and
  distinguish "no markers found" from "this figure does not apply here"
  (`ww3-rhetorlint/0.3`). A whole Chinese sentence counted as one word, so a
  single marker read as a density of 100 per 100 words.
- Measure shared visible text between the records in a casefile
  (`ww3.shared-text/v1`), beside the publisher and control-group counts that
  contract 6 warns are not corroboration. It reports an arithmetic, not a
  shared origin, and silence in it is not evidence of independence.

**Upgrading:** the casefile fingerprint binds the actor-mention contract, so an
assessment written against records under v2 will fail `validate-assessment`
with a fingerprint mismatch. That is the intended refusal — v3 changes spans
and counts, so a v2-bound assessment must not silently revalidate. Rebuild the
casefile and re-check the assessment against it.

## 0.1.3 — 2026-08-02

- Match actor aliases under the guard their own script can carry, so a mention
  in a language that writes without spaces is visible at all
  (`ww3.actor-mentions/v2`, superseded by v3 below). Every mention records
  which guard applied.
- Carry native-script aliases for registered actors.
- Register first-party publishers beyond the original English set, disabled and
  saying in their own caveat that their endpoints are unverified.
- Report a feed that completes its cycles while parsing no records, which until
  now was indistinguishable from a healthy quiet one.
- Distinguish a source held back by choice from one nobody has confirmed.

## 0.1.2 — 2026-07-26

- Supervise the runtime's children instead of ending the deployment: a dead
  child is respawned with capped backoff, and only an unkeepable one exits.
- Derive the collector staleness grace from the watcher's own cycle lengths, so
  a restarted Machine reading the previous life's heartbeat is no longer taken
  for a dead collector.
- Report per-source capture cycles, so ten of eleven feeds can no longer fail
  while the aggregate heartbeat stays fresh.
- Refuse to run as root without a runtime UID rather than continuing as root.
- Record forecast provenance: horizon predictions now write an AnalysisRun under
  the `horizon-forecast-v1` contract, are idempotent per event, and state their
  authorship. Forecast runs are excluded from evidence provenance.
- Withhold rules whose tell is house typography from the rhetoric bridge,
  naming every withheld rule in the output; the contract version records the
  changed tell counts.
- State on every rendered value what wrote it, replace the KPI over a column
  nothing writes, and say how much of the ledger a bounded page is showing.
- Install the test suite's socket and Claude denials so a test cannot undo them.

## 0.1.1 — 2026-07-17

- Add a headless collection-only watcher shared with the terminal feed.
- Add a supervised Fly runtime for the watcher and Streamlit dashboard.
- Persist SQLite and the optional compatibility stream on one encrypted volume.
- Keep the hosted model path disabled and omit executable model/RhetorLint runtimes.
- Size the always-on London prototype at one shared CPU and 512 MiB RAM.
- Serialize ledger writes, monitor volume/freshness state, drop root after mount
  preparation, and pin the deploy image/dependency versions.

## 0.1.0 — 2026-07-17

- Make immutable Observation collection the lightweight default.
- Add bounded USGS, GDACS, and diversified RSS collection adapters.
- Add versioned source/publisher and actor-identity registries.
- Add deterministic casefiles with exact passages and bounded actor visibility.
- Add the snapshot-verified local RhetorLint bridge.
- Add the evidence-linked `ww3.assessment/v1` validation contract for claims,
  contextual power bases, and falsifiable intent hypotheses.
- Add the static documentation/spec deployment.

This release does not adjudicate truth, establish independent corroboration,
infer intent automatically, or deploy the Python/SQLite collector as a hosted
service.
