# Changelog

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
