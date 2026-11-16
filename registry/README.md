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

## Actor kinds and aliases

An actor's `kind` is one of a closed set that `core/registry.py` checks when it
loads the registry. A kind names a class of identity. It is not a standing, a
legitimacy or a measure of power.

- `state`: a polity with its own government, under the name most
  English-language reporting uses. Taiwan and Palestine are registered this
  way; the kind is not a position on sovereignty, recognition or membership of
  the United Nations.
- `government-body`: an office, ministry, department or administration of a
  government, such as the Kremlin or the Palestinian Authority.
- `armed-movement`: a political or religious movement that fields its own armed
  force, such as Hamas, Hezbollah, Ansar Allah (the Houthis) or the Taliban.
  The kind takes no position on a movement's legitimacy, on any government's
  designation of it, or on authority it holds over territory or people.
- `international-organization`, `alliance`, `central-bank`, `newsroom`,
  `alert-system`, `foundation`, `company`, `think-tank` and `person`: what
  their names say.

A new kind changes that set, this list and the test that checks they agree.

An alias is a name the actor is known by, matched as written. A state carries
its English name, its name in its own language, and its official long name
where reporting uses one and the name takes no side (Afghanistan carries none).
Most states also carry "<demonym> government", and their names in Russian,
Ukrainian, Chinese, Japanese, Korean, Arabic, Persian and Hebrew; a form that
is ambiguous is left out. Other actors carry their English and own-language
names. A bare demonym or adjective (Russian, Houthi) is never an alias. A short
form that is also a common word, or that names a wider area, is left out for a
qualified one: `قناة الجزيرة`, not `الجزيرة`; `دولة قطر`, not `قطر`; and no
`한국`, which means Korea as a whole. An alias whose lower-case form is an
ordinary word is case-sensitive (`Le Monde`, `Polska`). The matcher treats a
straight and a typographic apostrophe as different characters, so a name with
an apostrophe is seen in both spellings only if it carries both, as the long
names of China and North Korea do.

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
