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
records total), and GDACS (15 minutes, 50 records).

The optional dashboard starts on the Source Observations page:

```bash
.venv/bin/streamlit run dashboard/app.py
```

## Published documentation

GitHub Pages publishes a static documentation/spec surface at
<https://cambridgetcg.github.io/ww3-intelligence/>. The deployment copies only
the files under `site/` plus the source registry, actor registry, and assessment
JSON Schema. It does not host the Python collector, SQLite ledger, feeds,
observations, RhetorLint process, dashboard, or model-derived analysis.

`.github/workflows/pages.yml` performs that bounded deployment after pushes to
`main`. `.github/workflows/ci.yml` runs the isolated test suite separately.

## Hosted prototype

The live observation dashboard is at
<https://ww3-intelligence.fly.dev/>.

The checked-in Fly configuration runs one always-on, 512 MiB shared-CPU Machine
in London with one encrypted 1 GiB volume mounted at `/data`. A small supervisor
performs the SQLite schema/integrity preflight, then runs the Streamlit dashboard
and the collection-only USGS/RSS/GDACS watcher on that same Machine so both use
the canonical `/data/ww3.db` ledger. Source fetches remain parallel while ledger
writes are serialized.

```bash
fly apps create ww3-intelligence --org personal --yes
fly volumes create ww3_data --app ww3-intelligence --region lhr --size 1 --yes
fly deploy --ha=false --remote-only --yes
```

The hosted runtime fixes `WW3_DISABLE_LLM=1`. It does not provide or execute a
Claude model path, RhetorLint engine, assessment author, or automatic
truth/corroboration/power/intent inference. Its Streamlit health path proves
web-process liveness. The supervisor separately requires the `/data` mount,
checks free space, fails startup if SQLite cannot initialize/pass integrity, and
stops the Machine if every collection group becomes stale. These checks are not
an alerting service or a universal availability guarantee. Runtime processes
drop root privileges after preparing the mounted volume. One Machine and one
local volume are a lightweight prototype, not high availability or a complete
backup strategy.

## Evidence and capability limits

The ordinary collection/projection command path guarantees that framework code
does not invoke Claude or write model-derived Events. It preserves bounded
source responses, immutable normalized content, captured provenance,
exact-content dedupe, and owner-only local state where POSIX permissions apply.

CLI source collection defaults to 200 records per run. News divides that cap
across the configured feeds; direct RSS adapter calls default to 100 records per
feed and direct USGS adapter calls to 200. Callers can choose smaller bounded
limits explicitly.

It does not establish source truth, authenticity, corroboration, independence,
freshness, completeness, intent, or power. Publication or enactment is not a
blanket truth finding; publisher diversity is not corroboration; actor mention
volume is not power. Canonical-host consistency is not authentication, and
legacy `source_confidence` is not credibility. It does not yet cluster
syndicated reports, retain every repeated fetch or metadata-only change,
preserve A→B→A as a third occurrence, encrypt the database, anonymize network
traffic, or impose a
universal memory/wall-clock bound. HTTP reads are capped and timed; parsers
still materialize each bounded response. Feed failures are reported by source
and are not evidence that no event occurred.

The active source and actor registries are finite, hand-curated, and currently
English-only. There is no live-verified official China adapter or multilingual
local-reporting layer yet. Assessment validation checks exact lineage and
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
