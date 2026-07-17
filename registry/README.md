# Source and actor registries

`sources.json` and `actors.json` are versioned claimed-identity and source-role
context for collection and casefiles. `core/registry.py` validates their shape
before use; this is not publisher or transport authentication.

## What the fields mean

A source profile records its endpoint and delivery shape, the publishing actor
when known, a broad content role, an editorial-control group, an authority
scope, and an explicit caveat. These fields help an agent distinguish, for
example, a first-party statement or institutional publication from newsroom
reporting, a primary measurement, or an automated alert. In particular,
`institutional-publication` and `automated-alert` are descriptive roles, not
truth, reliability, or credibility findings.

`canonical_hosts` supports a narrow consistency check: a casefile compares the
Observation's claimed source ID with the host of its canonical URL and requires
HTTPS. A match is not cryptographic publisher authentication, transport
attestation, or proof that the registered collector fetched the record.
Synthetic, missing, insecure, mismatched/spoofed, unregistered, or unverifiable
hosts do not contribute a publisher actor or known editorial-control-group or
content-role context. Their claimed source context remains visible with its
failure status.

An editorial-control group identifies known common publisher control. It does
not prove that two different groups are independent: publishers may use the
same wire, quote one origin, or repeat the same assertion. Publisher diversity
is therefore not corroboration.

Authority scope is also narrow. An official release or enacted decision can
evidence what the institution published, announced, or enacted in that scope;
it does not make every factual claim inside the text true. Automated USGS or
GDACS records describe measured or alerted conditions, not cause or hostile
intent.

Actor profiles contain stable IDs, display names, kinds, and conservative text
aliases. Casefiles report exact alias spans as visible mentions. Mention count
does not measure power, responsibility, agreement, importance, or intent. The
scan has candidate/per-passage/per-observation limits and may be incomplete;
casefiles expose truncation metadata rather than claiming a complete actor
ranking.

Neither registry accepts truth, credibility, reliability, power, motivation,
or intent scores. Analytical judgments belong in attributed, evidence-linked
assessment claims, not identity metadata.

The Observation `source_confidence` field predates these registries and remains
only a collector-supplied capture/parse hint. New RSS, USGS, and GDACS adapters
set it to `not-assessed`. It is not factual confidence, source credibility,
corroboration, or truth.

## Collection roles

RSS profiles with `collection_enabled: true` supply the bounded `ingest news`
rotation. USGS and GDACS use their dedicated structured adapters. A profile
with collection disabled may support discovery context but is not fetched by
normal collection.

Inspect the parsed registry without opening the database:

```bash
.venv/bin/python ww3.py sources
.venv/bin/python ww3.py sources --json
```

When adding a source, use a credential-free HTTPS endpoint, register its
publisher actor when known, choose the narrowest honest authority scope, assign
known common editorial control consistently, list the exact acceptable
canonical hosts, and state the source-specific caveat. Endpoint availability
and source authenticity must be verified separately; a valid registry entry is
configuration, not a health or authentication guarantee.
