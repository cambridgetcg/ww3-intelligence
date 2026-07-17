# WW3 Observation Framework

## Purpose

Local, collection-first geopolitical source framework. Its normal path captures
immutable Observations and computes deterministic, inference-free source
projections. A constrained local Claude transformation remains an explicit,
optional derived layer.

It does not establish truth, corroborate a source, infer risk in the projection
layer, infer power or intent from mentions, provide a universal prompt-injection
sandbox, or deploy a service.

## Runtime layers

Core:

- Python 3.14
- Peewee + SQLite
- Click, Requests, feedparser, python-dotenv
- bounded RSS, USGS, and GDACS adapters plus a local/agent JSON capture command
- versioned source/publisher and actor-identity registries
- deterministic casefiles with bounded actor visibility and claimed source-host
  consistency, plus strict evidence-linked assessment validation

Optional:

- Rich terminal observation feed (`requirements-tui.txt`)
- Streamlit + Plotly + Pandas dashboard (`requirements-dashboard.txt`)
- local Claude Code CLI for explicit JSON transformation; no Anthropic Python
  SDK/API-key path
- local RhetorLint 0.1 reference checkout plus Node, used only by the explicit
  rhetoric command or `casefile --rhetorlint`; both are trusted local code
  running with the current user's privileges

Model execution requires an explicit analysis action and an explicit false
`WW3_DISABLE_LLM` value (`0`, `false`, `no`, or `off`). Collection never checks
or calls the model boundary.

## Contracts

1. `core/collection.py` is the no-analysis batch boundary.
2. `core/observations.py` normalizes and appends source records.
3. Observation content/provenance is immutable and deduplicated by a
   deterministic source-record/content-revision SHA-256 key. Distinct content
   revisions are linked with `supersedes`.
4. `core/projections.py` reads only stored Observations and returns a versioned,
   bounded JSON-safe view. It must not infer significance, category, credibility,
   confirmation, actors, escalation, impact, scenarios, or probability.
5. `registry/sources.json` records source role, delivery, publisher/control
   lineage, authority scope, and caveats. `registry/actors.json` records identity
   and aliases. Roles such as `institutional-publication` and `automated-alert`
   describe records, not their truth or credibility. Neither registry may
   contain analytical scores or motivations.
6. A first-party publication or enactment can establish what was published or
   enacted in the cited scope; it does not establish the truth of all embedded
   claims. Publisher diversity is not independent corroboration. Actor mention
   volume is visibility, not power.
7. Source identity means only a claimed source ID whose canonical URL has an
   HTTPS host consistent with the registry. It is not authentication or proof
   of collection. Missing, insecure, spoofed/mismatched, unregistered, and
   unverifiable hosts do not enter publisher/control-group/content-role counts.
8. `source_confidence` is a legacy collector capture/parse hint. New adapters
   set it to `not-assessed`; it is not truth, factual confidence, or source
   credibility.
9. `interpretation/casefile.py` packages exact visible passages, source-host
   consistency, and bounded actor-alias spans. Actor visibility may be
   incomplete, so preserve per-observation scan metadata and aggregate status.
   It does not infer intent or adjudicate truth.
10. RhetorLint marks visible language only. It cannot establish truth,
   deception, credibility, intent, or actor attribution, and its `strip` output
   remains omitted to preserve evidential qualifiers. Configured
   `RHETORLINT_ROOT`/`RHETORLINT_NODE` code executes with user privileges; the
   boundary is not a filesystem, process, or network sandbox.
11. `ww3.assessment/v1` binds claims to exact passage spans and registered
   subject actors. A power basis is a non-scored, time-scoped description with
   required relational/geographic/operational scope and reasoned evidence
   links, including a supporting verbatim capacity claim that visibly names its
   actor, plus limitations and falsifiers. Every related actor requires joint
   supporting verbatim capacity evidence that names and subject-links both;
   bilateral leverage has exactly one counterparty. Intent hypotheses require
   unique IDs,
   rationale/time scope, reasoned supporting/contradicting links, alternatives,
   and falsifiers. Support must include a reported-action, public-position, or
   denial claim about the actor. Power/intent support must be verbatim and
   visibly name the actor; capability or paraphrase alone is invalid. Publisher
   attribution requires the registered publisher and HTTPS host consistency.
   No intent confidence/probability is allowed.
12. Mutable model state belongs in AnalysisRun, entered only through
   `core/pipeline.py:analyze_observation` or its explicit compatibility wrappers.
13. Model output must pass `analysis/schema.py`; derived Event/branch writes stay
   in one fenced transaction. One observation cannot confirm itself.
14. SQLite is canonical. Event JSONL is a model-layer compatibility view.

## Data flow

```text
adapter / JSON → collect_leaf → Observation → project_observation → CLI/JSON/UI
                                  ├→ explicit casefile
                                  │    ├→ registry context + passage spans
                                  │    ├→ optional local RhetorLint marks
                                  │    └→ explicit assessment validation
                                  └→ explicit analyze_observation
                                       → AnalysisRun → Event + branches → JSONL
```

No projection table is needed: projection output is deterministic for its
`PROJECTION_VERSION`. Change that version whenever the projection contract
changes materially.

## Important files

- `ww3.py` — CLI entry point
- `core/collection.py` — collection-only outcomes and batch counts
- `core/observations.py` — normalization, hashing, immutable capture, revisions
- `core/projections.py` — pure versioned source view
- `core/registry.py` — strict claimed-source and actor identity-registry loader
- `core/pipeline.py` — explicit model analysis leases and atomic persistence
- `core/db/__init__.py` — schema, additive migration, integrity/permission gates
- `analysis/claude_cli.py` — fail-closed local Claude boundary
- `analysis/schema.py` — strict model-output validation
- `ingestion/` — bounded RSS/Brave, USGS, and GDACS adapters
- `registry/` — versioned claimed-source/publisher context and actor identities
- `interpretation/casefile.py` — deterministic evidence packets and mention spans
- `interpretation/rhetorlint.py` — bounded bridge to an exact local reference engine
- `interpretation/assessment.py` — runtime evidence-link validator
- `spec/assessment.schema.json` — portable `ww3.assessment/v1` shape
- `cli/feed.py` — optional observation-first terminal feed
- `dashboard/app.py` — optional observation-first dashboard

## Commands

```bash
.venv/bin/python ww3.py ingest usgs --dry-run
.venv/bin/python ww3.py ingest gdacs --limit 50
.venv/bin/python ww3.py ingest news --limit 100
.venv/bin/python ww3.py sources --json
.venv/bin/python ww3.py capture records.json --json
.venv/bin/python ww3.py observations
.venv/bin/python ww3.py interpret OBSERVATION_ID --json
.venv/bin/python ww3.py rhetoric OBSERVATION_ID --json
.venv/bin/python ww3.py casefile OBSERVATION_ID... --rhetorlint --json
.venv/bin/python ww3.py validate-assessment assessment.json --json
.venv/bin/python ww3.py analyze-observation OBSERVATION_ID
.venv/bin/python ww3.py runs --status failed
.venv/bin/python ww3.py provenance EVENT_ID
.venv/bin/python ww3.py feed --watch
.venv/bin/streamlit run dashboard/app.py
.venv/bin/python -m pytest -q
```

`ingest --dry-run` still performs the selected external HTTP fetch, but skips
database initialization, all storage, and model work. Normal `ingest` and
`feed --watch` collect only; the watcher runs independent bounded USGS, RSS,
and GDACS cycles. `ingest --analyze` and `analyze-observation` are the
conspicuous, bounded opt-in model actions; the watcher has no model mode.

RhetorLint discovery checks `RHETORLINT_ROOT` first, then the sibling
`../rhetorlint-spec` checkout. `RHETORLINT_NODE` can select Node; otherwise the
bridge resolves `node` from `PATH`. It verifies and records the local core and
English-rules hashes, then runs exact copies from a one-use temporary snapshot.
The bridge has no npm/npx/download path, but the configured checkout and
executable are trusted local code with ordinary user privileges; they are not
prevented from filesystem, process, or network access.

`casefile --rhetorlint` accepts at most eight Observations and stops accepting
annotation results after 16 MiB or 8,192 marks in aggregate. Each Observation
also retains the per-run passage/input/output and timeout bounds in
`interpretation/rhetorlint.py`.

## Verification boundary

Tests must use disposable DB/stream paths, deny DNS/socket access, replace the
real Claude executable, and set `WW3_DISABLE_LLM=1`. Collection tests must assert
zero AnalysisRun, Event, stream, subprocess, and model calls. Never weaken these
controls to make a test pass.

Tests of the explicit rhetoric path may replace its Node subprocess, but the
ordinary collection and projection boundaries must remain subprocess-free.

Do not run live feeds, enable Claude, mutate ignored local databases, or deploy
without explicit scope.

## Current limits

- No cross-source event clustering, independence, or syndication graph.
- Source and actor registries are finite, hand-curated, and English-only; there
  is no live-verified official China adapter or multilingual/local-reporting
  layer.
- Source/control-group counts are descriptive only; they do not establish
  corroboration, and official publication/enactment is not a blanket truth
  finding.
- Claimed source/HTTPS-host consistency is not publisher authentication;
  inconsistent or missing hosts stay out of publisher/control-group counts.
- `source_confidence` is legacy collection metadata, not credibility or truth.
- Registered actor matches are bounded and may be incomplete. They measure
  visible mentions only, not power or intent.
- RhetorLint matches language patterns only; it does not detect truth,
  deception, or motivation.
- Assessment validation checks lineage and structure, not whether an assessor
  semantically labeled a claim, actor, evidence effect, power basis, or intent
  rationale correctly.
- No automatic forecast resolution/calibration workflow.
- Revision identity does not record every repeated fetch, metadata-only change,
  or A→B→A occurrence.
- Per-response byte caps and per-run record caps are not a universal resource
  sandbox; feed parsers materialize each bounded response.
- Historical Events may lack Observation lineage; existing failed runs remain
  historical and collection does not retry them.
- GitHub Pages deploys documentation and copied portable JSON artifacts only.
  No hosted collector, dashboard, database, feed worker, RhetorLint process, or
  background-service configuration is present.
