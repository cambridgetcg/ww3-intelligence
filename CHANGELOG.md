# Changelog

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
