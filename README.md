# WW3 Observation Framework

A small, local framework for collecting geopolitical source records and
building inspectable interpretations over them.

The default product is an immutable observation ledger plus deterministic
source projections. It is not an oracle, and it does not need an LLM,
dashboard, background service, or vector database.

Versioned source and actor registries add explicit publisher, editorial-control,
authority-scope, and identity context. They contain no truth, credibility,
power, reliability, or motivation scores. Source identity here means only a
claimed source ID checked against an HTTPS canonical URL host in the local
registry; it is not publisher or transport authentication.

## Default data flow

```text
bounded source adapter / agent JSON
              ↓
       immutable Observation
              ↓
 versioned source projection ──→ CLI / JSON / optional terminal or dashboard UI
              ├── explicit casefile ──→ passages + source context + actor mentions
              │                            ├── optional local RhetorLint marks
              │                            └── assessment validation
              └── explicit model opt-in ──→ AnalysisRun → derived Event/branches
```

SQLite is canonical. A projection is a reproducible view over a stored
Observation, not another stored claim. The Event JSONL file belongs only to the
optional model-derived compatibility layer.

## Lightweight path

The core install contains Click, Peewee, Requests, feedparser, and
python-dotenv. It can:

- fetch bounded USGS or RSS batches, one news feed at a time;
- fetch bounded GDACS multi-hazard records;
- accept a normalized JSON record or JSON array from a file or stdin;
- normalize and append immutable Observations with exact-content dedupe;
- link changed content under the same source record as a revision;
- render bounded, JSON-safe structured source fields (with small source-specific
  measurement/alert fact projections) without inference;
- expose an agent-readable observation inbox and interpretation output;
- build bounded casefiles with exact visible passages, claimed source/host
  consistency context, and conservative registered-actor alias matches.

It does not assign significance, credibility, geopolitical category,
confirmation, power, escalation, impact, intent, or probability. Actor matches
identify visible text spans only. Those analytical claims remain in an explicit
assessment or optional model-derived layer.

## Install

Core collection and projection only:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Optional surfaces are separate so a collector does not pull in Pandas,
Plotly, Streamlit, Rich, or the test stack:

```bash
.venv/bin/pip install -r requirements-tui.txt
.venv/bin/pip install -r requirements-dashboard.txt
.venv/bin/pip install -r requirements-dev.txt
```

## Collect and interpret

Preview a real source fetch without opening or modifying SQLite:

```bash
.venv/bin/python ww3.py ingest usgs --dry-run
.venv/bin/python ww3.py ingest gdacs --dry-run --limit 50
.venv/bin/python ww3.py ingest news --dry-run --limit 100
```

Capture records without model work:

```bash
.venv/bin/python ww3.py ingest usgs
.venv/bin/python ww3.py ingest gdacs --limit 100
.venv/bin/python ww3.py ingest news --limit 200
.venv/bin/python ww3.py observations
.venv/bin/python ww3.py observations --json
.venv/bin/python ww3.py interpret OBSERVATION_ID
.venv/bin/python ww3.py interpret OBSERVATION_ID --json
```

Inspect the versioned registries without opening SQLite:

```bash
.venv/bin/python ww3.py sources
.venv/bin/python ww3.py sources --json
```

The active RSS set distinguishes newsroom reports, first-party statements, and
`institutional-publication` records. USGS supplies primary measurements and
GDACS supplies `automated-alert` records. These roles describe the record, not
its truth or credibility. A first-party or institutional publication can
establish that an actor published, announced, or enacted something within its
authority scope; it does not establish that every claim inside the publication
is true.
Likewise, multiple publishers do not by themselves establish independent
corroboration: they may share a wire, quote one origin, or repeat the same
claim.

Agents and local tools can pipe one record or an array directly into the same
collection boundary:

```bash
printf '%s' '{"source":"manual","type":"statement","raw_text":{"title":"Source statement"}}' \
  | .venv/bin/python ww3.py capture --json
```

The minimum input contract is `source`, `type`, and `raw_text`. `raw_text` may
be a string or JSON value. Useful optional fields include `source_record_id`,
`canonical_url`, `published_at`, `fetched_at`, `location_name`, `geo_lat`,
`geo_lon`, `source_confidence`, and `synthetic`.

`capture` accepts at most 16 MiB and 1,000 records per invocation. Larger agent
exports should be split into explicit batches.

`source_confidence` is a legacy collector-supplied capture/parse hint. New RSS,
USGS, and GDACS adapters set it to `not-assessed`. It is not source credibility,
factual confidence, corroboration, or truth.

## Casefiles, rhetoric, and assessments

Build a deterministic evidence packet from explicit Observation IDs, or omit
the IDs to use the newest bounded set:

```bash
.venv/bin/python ww3.py casefile 17 23 41 --json
.venv/bin/python ww3.py casefile --limit 20 --json
```

A casefile includes source context, a claimed-ID/canonical-host consistency
status, bounded visible-text passages, stable passage IDs, and registered-actor
mention spans. Only non-synthetic records whose claimed source ID has an HTTPS
canonical host consistent with the registry enter known publisher,
editorial-control-group, and content-role counts. Missing, insecure, mismatched,
unregistered, or unverifiable hosts do not. Even a consistent host is not
cryptographic publisher authentication or proof that the registered collector
fetched the record; the resulting publisher/control-group counts are not a
corroboration result.

Beside those counts, a casefile measures how much visible text its own records
share with each other. That is deliberately the narrowest thing worth saying: a
high measure does not establish that two records have a common origin, and a
low one is not evidence that they are independent, since two newsrooms
reporting one event in their own words — and a translation of a single wire
story — both share almost no tokens. It exists because five outlets running one
piece of copy otherwise present as five publishers and five control groups, and
the reader is left to notice that alone.

Actor matching is bounded and can be incomplete. Per-observation scan metadata
and the casefile `actor_visibility_status` report truncation. Mention volume and
emitted ordering are visibility in selected text, not a complete actor list or
a measure of power, responsibility, agreement, or intent.

Casefiles accept at most 100 unique stored Observations and default to the most
recent 20 when IDs are omitted. `--rhetorlint` is capped at eight Observations,
16 MiB of returned annotation JSON, and 8,192 marks across the casefile. For
each Observation the bridge analyzes at most eight passages and 64 KiB of
visible text, with a five-second local runtime timeout.

RhetorLint is a separate, explicit local annotation step:

```bash
.venv/bin/python ww3.py rhetoric OBSERVATION_ID --json
.venv/bin/python ww3.py casefile 17 23 --rhetorlint --json
```

The bridge discovers a sibling checkout at `../rhetorlint-spec`, or reads an
explicit `RHETORLINT_ROOT`. `RHETORLINT_NODE` may select a Node executable;
otherwise `node` is resolved from `PATH`. It verifies and records the local
reference-core and English-rules hashes, then executes exact copies from a
one-use temporary snapshot. The bridge itself has no npm/npx/download path, but
`RHETORLINT_ROOT` and `RHETORLINT_NODE` select code that executes with the
current user's privileges. This is a trusted-local-code boundary, not a
filesystem, process, or network sandbox; use only a checkout and executable you
trust.

RhetorLint marks language patterns in visible source text. Its marks are not
findings of truth, deception, credibility, actor intent, or corroboration, and
zero matches means only that the current English rules matched nothing. The
RhetorLint `strip` output is deliberately omitted because removing words such
as “reportedly” or “allegedly” can erase evidential qualifiers.

Agent or human assessments can then be checked against the immutable casefile:

```bash
.venv/bin/python ww3.py validate-assessment assessment.json
.venv/bin/python ww3.py validate-assessment assessment.json --json
cat assessment.json | .venv/bin/python ww3.py validate-assessment - --json
```

The `ww3.assessment/v1` validator requires each claim to cite an exact passage
span and name its registered `subject_actor_ids`. Speaker/reporter/publisher
attribution is separate; publisher mode additionally requires a matching
registry publisher and HTTPS canonical-host consistency, which is still not
authentication.

Power is represented only as an evidence-linked basis, never a score. Each
entry has a unique `power_basis_id` and identifies an actor, domain,
`time_scope`, workflow state, limitations, and falsifiers. Its required `scope`
classifies the relationship and lists the geographies, operations, and related
registered actors; bilateral leverage must name exactly one counterparty. Every
related actor must appear jointly with the focal actor, as both a claim subject
and a visible alias, in supporting verbatim capacity evidence. This keeps a
situational capacity from silently becoming a universal power claim. Intent is
a falsifiable hypothesis with a
unique `hypothesis_id`, `rationale`, `time_scope`, workflow state, and evidence
links that state `supports` or `contradicts` plus a reason. At least one
supporting claim must be verbatim action/public-position/denial evidence whose
exact span visibly names the actor; capability or a paraphrase alone is invalid.
Alternatives and falsifiers are
required, and there is intentionally no intent-confidence or probability
field. Successful validation establishes structural evidence links, not that a
claim, power basis, or hypothesis is true. See `spec/assessment.schema.json`
and `spec/README.md`.

## Optional model-derived analysis

Model analysis requires two independent choices:

1. explicitly run `analyze-observation ID` or pass `ingest --analyze`; and
2. explicitly set `WW3_DISABLE_LLM=0` (also accepts `false`, `no`, or `off`).

```bash
.venv/bin/python ww3.py analyze-observation OBSERVATION_ID
.venv/bin/python ww3.py runs
.venv/bin/python ww3.py list
.venv/bin/python ww3.py provenance EVENT_ID
```

Enabling the setting alone never triggers analysis. If analysis is disabled or
the local Claude CLI is unavailable, the CLI exits before claiming an
AnalysisRun. The existing fenced lease, schema validation, one-source
confirmation cap, and atomic Event/branch write remain in place.

`migrate-legacy-event EVENT_ID` is only for old `RawLeaf` rows. It is not the
normal observation analysis command.

Migration note: the former `analyze EVENT_ID` command is now
`migrate-legacy-event EVENT_ID`, and ordinary `ingest SOURCE` no longer creates
derived Events unless `--analyze` is supplied. Update any local scripts to use
Observation IDs with `analyze-observation` for the new path.

## Command effects

| Command | External network | Local writes | Model |
|---|---|---|---|
| `ingest SOURCE --dry-run` | selected feed HTTP | none | never |
| `ingest SOURCE` | selected feed HTTP | Observations | never |
| `sources` | none | none | never |
| `capture` | none | Observations | never |
| `observations`, `interpret` | none | initializes ledger if absent; otherwise reads | never |
| `casefile ...` (without rhetoric) | none | initializes ledger if absent; otherwise reads | never |
| `rhetoric ID`, `casefile --rhetorlint` | no bridge fetch; configured code is network-unrestricted | ledger init plus unrestricted local code; WW3 stores no annotations | no WW3 model call; configured code unrestricted |
| `validate-assessment FILE` | none | initializes ledger; assessment is not stored | never |
| `feed --watch` | USGS/RSS/GDACS HTTP | Observations | never |
| `analyze-observation ID` | local Claude may contact its provider | AnalysisRun, Event/branches, event JSONL | explicit |
| `ingest SOURCE --analyze` | feed HTTP plus model provider | Observations and derived analysis | explicit |

The optional terminal feed is collection-first:

```bash
.venv/bin/python ww3.py feed
.venv/bin/python ww3.py feed --watch
```

The watcher intentionally has no analysis mode. Analyze selected Observation
IDs separately so collection intervals cannot be stalled by long model calls.
It keeps independent bounded cycles for USGS (5 minutes), RSS (10 minutes, 200
records total), and GDACS (15 minutes, up to 50 records from each of its two
queries).

The optional dashboard starts on the Source Observations page:

```bash
.venv/bin/streamlit run dashboard/app.py
```

Its Collection Cycles panel reports when each group and each registered feed
last recorded a capture success. That is a statement about this collector, not
about a publisher: a stale cycle says no capture completed recently, and a fresh
one is not evidence that anything captured is true. Every score the dashboard
shows names what wrote it — a specific AnalysisRun, or an operator.

## Published documentation

A reviewed copy of `site/`, with the source registry, actor registry, and
assessment JSON Schema, is served at
<https://cambridgetcg.github.io/ww3-intelligence/> from the
cambridgetcg.github.io repository. It does not host the Python collector,
SQLite ledger, feeds, observations, RhetorLint process, dashboard, or
model-derived analysis.

This repository is public, and it no longer deploys that copy: project Pages is
off, and the workflow that deployed it was removed in 0.1.8 after failing on
every push since Pages was switched off. Switching project Pages on again could
put this repository's unreviewed `site/` at the same address as the reviewed
copy. `.github/workflows/ci.yml` runs the isolated test suite.

## Production collection

Collection runs on GitHub Actions, with no host of its own, and the lab Mac
keeps the history.

- `.github/workflows/ww3-ingest.yml` collects USGS, the enabled news feeds, and
  GDACS into one SQLite ledger. The lab Mac asks for a run every 30 minutes;
  the workflow's own `*/30` cron is the fallback, and GitHub delivers far fewer
  of those ticks than it is asked for (about 5.8 a day between September and
  November 2026).
- The ledger passes from run to run in the Actions cache. Ingest restores it
  with `fail-on-cache-miss`, so a missing cache fails the run instead of
  quietly starting an empty ledger, and saves it back only from a guarded step
  that `ww3-ingest.yml` describes. Its `start_new_ledger` dispatch input is the
  one deliberate way to begin a new ledger.
- Each run asks USGS for up to 1,000 events up to the moment of the request,
  reads up to 500 entries from each enabled feed (every current feed in full)
  rather than sharing one total between them, and asks GDACS two questions
  rather than taking its default list of past Orange and Red events: every
  alert level with an end date in the last three days, one page of 100, which
  holds about the newest day and is nearly all Green; and Orange and Red
  alerts with an end date in the last 30 days, which reads the older ones that
  page leaves out. An event both return is stored once. An answer that is not
  a feed (a challenge page, an error page, a body that does not parse as one)
  counts as that feed failing, and each failed or empty feed is named in a
  warning and in the run's step summary, beside per-feed counts. IAEA items
  keep the time of day their feed states instead of midnight.
- Each source's step has its own time limit (USGS 2 minutes, the news feeds
  10, GDACS 3) inside the job's 20. A fetch's timeouts do not bound a whole
  answer, so an endpoint that keeps sending slowly fails its own step, the way
  a failed source does, rather than the whole job timing out before the ledger
  is checked and saved. A step stopped that way names no failed or empty feed
  and writes no counts; the runner's timed-out error says which step it was.
  This is read from the runner's source and replayed locally; no step has
  reached its limit on GitHub yet.
- `.github/workflows/ww3-export.yml` encrypts a copy of the ledger to the
  archive's keys every Monday, and once a day more when the lab Mac asks.
  Scheduled exports also commit `ledger/status.json`, counts and a digest with
  no publisher text, so that the public repository shows activity. Whether a
  push made with `GITHUB_TOKEN` counts as activity for GitHub's 60-day rule is
  not documented, so the lab's `watch` also re-enables a workflow GitHub has
  disabled for inactivity.
- The lab Mac collects every export, decrypts and verifies it, and keeps it:
  `ops/lab_archive.py`, its LaunchAgents in `ops/lab/`, and the runbook in
  [`ops/ARCHIVE.md`](ops/ARCHIVE.md).

The cache is still the only live copy; while the lab Mac runs, the archive is
at most about a day behind it. None of this is high availability or an alerting
service. A failing source shows in its run's warnings and summary and in
`ops/lab_archive.py watch`, and reaches nobody who is not looking.

## The retired Fly runtime

From 2026-07-17 to 2026-09-01 one Fly Machine in London ran the collection-only
watcher and the Streamlit dashboard over a single SQLite volume. It was retired
when collection moved to GitHub Actions. Its dashboard no longer answers, and
there is no public live ledger.

Before the Fly app and its volume were deleted on 2026-11-09, the volume's
SQLite file was copied to the lab Mac and checked: its SHA-256 matched the file
on the volume, `PRAGMA integrity_check` returned ok, and it holds 12,226
observations fetched from 2026-07-17 to 2026-09-01. It is kept beside the
archive as a separate database. It was never carried into the Actions ledger,
which started empty on 2026-09-01, and the two are not merged: their row ids
overlap, and nothing here reconciles them. Copies that `ops/fly_backup.sh` made
while the Machine ran have not been checked.

`fly.toml`, `Dockerfile`, `requirements-deploy.lock`, `ops/fly_runtime.py`, and
`ops/fly_backup.sh` stay in the tree as a record of the retired deployment.
Nothing runs them, and the Fly app they name no longer exists.

## Evidence and capability limits

The ordinary collection/projection command path guarantees that framework code
does not invoke Claude or write model-derived Events. It preserves bounded
source responses, immutable normalized content, captured provenance,
exact-content dedupe, and owner-only local state where POSIX permissions apply.

CLI source collection is bounded per source. `ingest --limit` bounds the USGS
query (default 200, at most 1,000), each of the two GDACS queries (default and
at most 100, one page each, so up to 200 records, an event both return kept
once), or each enabled news feed (default 100, at most 500), rather than
dividing one total across the feeds, so no feed's share depends on another's.
A GDACS page that is full says so: GDACS may hold more matches. The scheduled
ingest asks for 1,000 USGS events and 500 entries a feed, which reads every
current feed in full, so a feed's own window (how many items it serves and how
far back they reach) is what limits it; a feed that reaches its bound says so.
Direct RSS adapter calls default to 100 records per feed and direct USGS
adapter calls to 200. Callers can choose smaller bounded limits explicitly.

It does not establish source truth, authenticity, corroboration, independence,
freshness, completeness, intent, or power. Publication or enactment is not a
blanket truth finding; publisher diversity is not corroboration; actor mention
volume is not power. Canonical-host consistency is not authentication, and
legacy `source_confidence` is not credibility. It does not yet cluster
syndicated reports, retain every repeated fetch or metadata-only change,
preserve A→B→A as a third occurrence, encrypt the database, anonymize network
traffic, or impose a
universal memory/wall-clock bound. HTTP reads are capped and timed; parsers
still materialize each bounded response. A feed document that ends before it
is finished is a failed fetch, unless feedparser's strict parser had already
stopped at an earlier fault, in which case it is read as before. Feed failures
are reported by source and are not evidence that no event occurred.

The active source registry is finite, hand-curated, and mostly English: since
0.1.5 the German and Turkish foreign ministries' own feeds collect too, and the
other first-party publishers outside the original set stay registered but
disabled until someone fetches their endpoints and confirms they exist and serve
what their entries claim. Actor aliases carry native-script forms and are
matched under the guard each script can bear, so a mention is visible in text
where it previously could not be seen — but a registered alias is only ever
visibility, never power, and no live-verified official China adapter or
multilingual local-reporting layer is collecting yet. Assessment validation checks exact lineage and
contract structure; it cannot establish that an assessor chose the correct
subject actor, claim kind, evidence effect, power description, or intent
rationale.

## Tests

```bash
.venv/bin/python -m pytest -q
```

The suite uses disposable DB and stream paths, blocks DNS/socket access,
replaces the real Claude executable, and verifies that collection never writes
the Event JSONL. It must leave the repository's ignored databases and stream
unchanged.

## Existing local data

Historical SQLite files and JSONL state are ignored by Git and are not rewritten
by tests. Existing derived Events remain available. Older failed model runs are
not deleted or retried by collection. Historical invented geopolitical seed
scripts and reports are deliberately excluded from the public source tree.

A local SQLite file that an earlier version opened may fail
`PRAGMA integrity_check` on `observation_supersedes_id` or
`prediction_analysis_run_id`, if its observation table predates revision links
or its prediction table held rows written before forecast provenance
(2026-07-26).
`REINDEX observation_supersedes_id; REINDEX prediction_analysis_run_id;`
repairs it without changing any row. It changes the file's bytes, so repair a
copy of an archived database, never the archived file.
