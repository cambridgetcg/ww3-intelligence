# Changelog

## 0.1.7 — 2026-11-08

- Read USGS up to the present moment. The query sent whole dates, so its window
  ended at 00:00 UTC today and nothing from the current UTC day could be read
  until the first run after the next midnight; on 2026-11-08 the ledger held
  none of that day's ten M4+ events. The scheduled run now asks for up to 1,000
  events: the old 200 was reached in 78 of 199 logged runs between 2026-10-11
  and 2026-10-31, which cut the oldest part of the seven-day window.
- Read each source to its own bound. `--limit` now bounds the USGS query
  (default 200), the GDACS list (100) or each enabled news feed (default 100,
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
- Ask GDACS for current events. The adapter sent only `limit`, which GDACS's
  list does not take, so it received the default list: on 2026-11-08, 82 Orange
  and Red events whose end dates reached back a year, none of them current. The
  first scheduled GDACS run captured exactly those 82. They stay in the ledger,
  which is append-only, as what that query returned that day. The query now
  asks for every alert level over the last three days, at most 100 events,
  newest end date first. That day a three-day window filled all 100 places with
  events whose end date fell in the previous 22 hours, 89 of them Green
  wildfire alerts, so a full page means more matched than were read. That the
  window applies to the end date is observed, not documented.
- Keep the time of day in IAEA's dates. IAEA writes "26-11-06  14:46", with no
  zone; feedparser dropped the time, so every IAEA item was stored at 00:00 UTC
  on its date. The time is now kept, read as UTC as every zoneless time is
  here, and written without an offset, beside the publisher's own string
  (`published_as_served`). If IAEA means Vienna time, the stored times are one
  or two hours late. Each IAEA item still in its feed is filed once as a
  revision (15 on 2026-11-08). No other feed's stored text changes: a replay of
  every entry of the ten other enabled feeds gave identical bytes.
- Keep the scheduled ingest's ledger (`ww3-ingest.yml`):
  - A cache miss fails the run before anything is collected. Until now a miss
    started an empty ledger, saved it as the newest entry, and every later run
    and export built on that while the real one aged out. A manual run with
    `start_new_ledger` is the deliberate way to begin one; it only permits a
    miss.
  - The ledger is checked before it is saved: SQLite's integrity check, and an
    observation count and highest id no lower than what was restored. One that
    fails is not saved.
  - Every source runs whatever the others did, and a checked ledger is saved
    even when one failed. The run then fails if USGS or the news feeds failed;
    GDACS may fail alone and leaves a warning.
  - The job holds a read-only token. The event-stream commit step is gone: the
    file is gitignored, so it never committed anything. The job installs
    `requirements-ingest.lock`, exactly what ingest imports, pinned with hashes
    at the versions production resolved on 2026-11-08, so no content hash
    changes with this release.
- Test on Python 3.11, which the scheduled ingest runs, as well as 3.14, and
  check in CI that the ingest lock alone covers the ingest path.

**Upgrading:** `ingest news --limit N` meant N records across all feeds; it now
means up to N from each feed. A `--limit` above a source's own bound (1,000 for
USGS, 100 for GDACS, 500 for a feed) is refused rather than silently cut.

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
