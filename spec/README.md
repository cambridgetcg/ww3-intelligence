# Assessment contract

`assessment.schema.json` describes the portable JSON shape of
`ww3.assessment/v1`. The runtime validator in `interpretation/assessment.py`
adds checks that JSON Schema alone cannot perform against the local immutable
ledger.

## Workflow

First build the exact casefile that the assessment will cite:

```bash
.venv/bin/python ww3.py casefile 17 23 41 --json > casefile.json
```

An assessment repeats those ordered Observation IDs and the casefile
`fingerprint_sha256`. Each claim identifies one Observation and passage, then
uses `span.start` and `span.end` with `unicode-code-point` offsets. `actual`
must equal the exact text at that span. The proposition remains an attributed
representation of that evidence; it is not silently promoted to fact. Every
claim also supplies `subject_actor_ids`, which is distinct from the attributed
speaker, reported speaker, or publisher.

Publisher attribution is accepted only when the actor matches the source
registry publisher and the Observation's claimed source ID has an HTTPS
canonical URL host consistent with that registry entry. This host check is not
publisher authentication or proof of how the record was fetched.

## Power bases

`power_bases` records inspectable capacity descriptions, never scores. Each
entry has a unique `power_basis_id`, a registered actor, a domain, description,
workflow state, and a time scope with at least one timezone-aware RFC 3339
bound. Its required `scope` has a typed `relationship`, non-empty `geographies`
and `operations`, and registered `related_actor_ids`; `bilateral-leverage`
requires exactly one counterparty. Every related actor must occur jointly with
the focal actor in supporting verbatim capacity evidence: both must be listed
as claim subjects and visibly named in the exact span. These fields bound the
claim instead of generalizing a local or temporary capability into universal
power. Its evidence links name a claim, mark the effect as `supports` or
`contradicts`, and explain the reason. At least one supporting link must point
to a `capacity` claim whose `subject_actor_ids` includes the same actor.
That claim must be `verbatim`, with `proposition == actual`, and its exact span
must visibly contain a registered alias resolved to the actor. Limitations and
concrete falsifiers are required.

## Intent hypotheses

Intent may be recorded only as a falsifiable hypothesis for a registered actor.
Every entry has a unique `hypothesis_id`, topic, hypothesis, rationale,
workflow state, and bounded time scope. Its evidence uses the same explicit
claim/effect/reason links, with at least one supporting link. That support must
include a `reported-action`, `public-position`, or `denial` claim whose
`subject_actor_ids` includes the hypothesis actor. A capacity claim alone is
invalid evidence of intent. Supporting power/intent evidence must also be
`verbatim` and visibly name the actor; a paraphrase cannot satisfy this gate.
At least one alternative explanation and one concrete falsifier are required.
There is deliberately no confidence or probability field for intent.

Validate a file or bounded UTF-8 JSON from stdin:

```bash
.venv/bin/python ww3.py validate-assessment assessment.json
cat assessment.json | .venv/bin/python ww3.py validate-assessment - --json
```

Runtime validation checks the current Observation IDs, recomputes the casefile
fingerprint, confirms passage ownership and exact spans, resolves registered
subject/attribution actors, enforces host-consistent publisher attribution, and
verifies all power/intent/contradiction claim references. It returns normalized
JSON but does not store the assessment.

A valid packet is structurally evidence-linked. Validation does not establish
that its claims are true, that its sources independently corroborate one
another, that the assessor chose the correct subject/claim/evidence labels, or
that a power description or intent rationale is sound. Publication/enactment is
not a blanket truth finding, publisher diversity is not corroboration, mention
volume is not power, and RhetorLint marks are not evidence of truth, deception,
or intent. Canonical-host consistency is not authentication, and the legacy
Observation `source_confidence` value is not source credibility or truth.
