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
its English name, its name in its own language, and the long name its own
institutions use, except where rival authorities use different ones
(Afghanistan carries none). Taiwan's `中華民國` also names the Republic of China
of 1912 to 1949, so a passage about the earlier state counts for Taiwan. Taiwan
carries no English "Republic of China", which would read as Taiwan any spelling
of the PRC's long name that China does not carry. Most states also carry
"<demonym> government", and their names in Russian, Ukrainian, Chinese,
Japanese, Korean, Arabic, Persian and Hebrew. "<demonym> government" counts
for the state whichever authority a passage means by it. That is uneven:
"Yemeni government" is kept although it can mean the authorities in Sanaa,
which Ansar Allah runs, while Palestine carries no "Palestinian government",
which can mean the Palestinian Authority or the authority in Gaza. Other actors
carry their English and own-language names. A bare demonym or adjective
(Russian, Houthi) is never an alias.

A short form whose other sense is common, or that names a wider area, is left
out where the actor carries a qualified form in the same script, even one that
reporting rarely uses, so some mentions go unseen: `قناة الجزيرة`, not
`الجزيرة`; `دولة قطر`, not `قطر`; `대한민국`, not `한국` (Korea as a whole) or
`남한`. Where a short form is the actor's only name in its script, it is kept,
and it can match other words. Hangul carries only a leading guard and Kana
none, so `인도`, `카타르` and `대만` also match the start of `인도네시아`,
`카타르시스` and `대만족`, and `インド` and `シリア` match inside `インドネシア`
and `シリアル`. `카타르` is also the Korean word for catarrh. `حزب الله`,
Hezbollah's only Arabic name here, is also a Quranic phrase and part of
`كتائب حزب الله`. An alias whose lower-case form is an ordinary word is
case-sensitive (`Le Monde`, `Polska`, `Катар`).

The matcher treats each apostrophe character as a different character, so a
name with an apostrophe is seen in a spelling only if it carries that
spelling. The long names of China and North Korea carry U+0027 and U+2019.
"Republic of Korea", South Korea's alias, lies inside North Korea's long name,
so North Korea's also carries U+2018, U+02BC and no apostrophe at all; any
other spelling of it, such as `Peoples'`, still reads as South Korea.
Azerbaijan's Hebrew name carries both the geresh (U+05F3) and U+0027.

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
