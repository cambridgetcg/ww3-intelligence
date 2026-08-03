"""Agent-readable, deterministic casefiles over immutable Observations."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable
from itertools import islice

from core.db import Observation
from core.projections import PROJECTION_VERSION, project_observation
from core.registry import (
    ACTOR_CONTRACT,
    SOURCE_CONTRACT,
    SOURCE_IDENTITY_CONTRACT,
    get_actor_profile,
    get_source_profile,
    registry_artifact_sha256,
    source_identity_context,
)
from interpretation.actors import ACTOR_MENTION_VERSION, scan_actor_mentions
from interpretation.rhetorlint import (
    PASSAGE_TRANSFORM_VERSION,
    RhetorLintError,
    analyze_observation_rhetoric,
    extract_passages,
)
from interpretation.shared_text import SHARED_TEXT_VERSION, shared_visible_text


CASEFILE_VERSION = "ww3.casefile/v1"
MAX_CASEFILE_OBSERVATIONS = 100
MAX_RHETORIC_OBSERVATIONS = 8
MAX_RHETORIC_CASEFILE_BYTES = 16 * 1024 * 1024
MAX_RHETORIC_CASEFILE_MARKS = 8_192
SOURCE_IDENTITY_VERSION = SOURCE_IDENTITY_CONTRACT


def _reload_stored_observations(observations: Iterable[Observation]) -> list[Observation]:
    items = list(islice(iter(observations), MAX_CASEFILE_OBSERVATIONS + 1))
    if len(items) > MAX_CASEFILE_OBSERVATIONS:
        raise ValueError(
            f"casefile exceeds {MAX_CASEFILE_OBSERVATIONS} observations"
        )
    if any(not isinstance(item, Observation) or item.id is None for item in items):
        raise TypeError("casefile requires stored Observations")
    ids = [item.id for item in items]
    if len(set(ids)) != len(ids):
        raise ValueError("casefile observations must be unique")
    rows = {
        item.id: item
        for item in Observation.select().where(Observation.id.in_(ids))
    }
    missing = [item for item in ids if item not in rows]
    if missing:
        raise TypeError("casefile requires stored Observations")
    return [rows[item] for item in ids]


# The fingerprint binds immutable capture metadata and the contracts that read
# it, and `interpretation/assessment.py` recomputes it from stored rows before
# accepting an assessment. A derived measure whose parameters are meant to be
# argued with must therefore stay out of it: retuning a threshold would
# otherwise invalidate every assessment already written against these records.
def _fingerprint_snapshots(
    observations: list[dict],
    registry_artifacts_sha256: dict[str, str],
) -> str:
    encoded = json.dumps({
        "casefile_version": CASEFILE_VERSION,
        "contracts": {
            "sources": SOURCE_CONTRACT,
            "actors": ACTOR_CONTRACT,
            "source_identity": SOURCE_IDENTITY_VERSION,
            "actor_mentions": ACTOR_MENTION_VERSION,
            "passages": PASSAGE_TRANSFORM_VERSION,
            "projection": PROJECTION_VERSION,
        },
        "registry_artifacts_sha256": registry_artifacts_sha256,
        "observations": observations,
    }, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def casefile_fingerprint(observations: list[Observation]) -> str:
    observations = _reload_stored_observations(observations)
    return _fingerprint_snapshots(
        [
            {
                "projection": project_observation(item).as_dict(),
                "source_confidence": item.source_confidence,
            }
            for item in observations
        ],
        registry_artifact_sha256(),
    )


def _unknown_source(source_id: str) -> dict:
    return {
        "source_id": source_id,
        "label": source_id,
        "publisher_actor_id": None,
        "delivery": "unknown",
        "endpoint": None,
        "content_role": "unknown",
        "locale": "unknown",
        "editorial_control_group": None,
        "authority_scope": None,
        "caveat": "Unregistered source; publisher control and authority scope are unknown.",
        "canonical_hosts": [],
        "domains": [],
        "collection_enabled": False,
    }


def build_casefile(
    observations: Iterable[Observation],
    *,
    include_rhetoric: bool = False,
    rhetoric_analyzer=analyze_observation_rhetoric,
) -> dict:
    """Build a bounded evidence packet without truth or intent inference."""
    items = _reload_stored_observations(observations)
    if include_rhetoric and len(items) > MAX_RHETORIC_OBSERVATIONS:
        raise ValueError(
            f"RhetorLint casefile exceeds {MAX_RHETORIC_OBSERVATIONS} observations"
        )
    ids = [item.id for item in items]
    registry_hashes = registry_artifact_sha256()

    records = []
    fingerprint_observations = []
    compared_passages = []
    source_ids = set()
    control_groups = set()
    content_roles = set()
    claimed_content_roles = set()
    registered_sources = set()
    host_consistent_sources = set()
    unknown_sources = set()
    mention_truncated_observations = set()
    rhetoric_bytes = 0
    rhetoric_marks = 0
    rhetoric_budget_exhausted = False
    visibility = defaultdict(lambda: {
        "observation_ids": set(),
        "source_ids": set(),
        "editorial_control_groups": set(),
        "mentions": 0,
    })

    for observation in items:
        projection = project_observation(observation).as_dict()
        fingerprint_observations.append({
            "projection": projection,
            "source_confidence": observation.source_confidence,
        })
        profile = get_source_profile(observation.source)
        source_context = profile.as_dict() if profile else _unknown_source(observation.source)
        identity = source_identity_context(
            observation.source,
            observation.canonical_url,
            synthetic=bool(observation.synthetic),
        )
        trusted_context = identity["status"] == "canonical-host-consistent"
        source_ids.add(observation.source)
        claimed_content_roles.add(source_context["content_role"])
        if profile:
            registered_sources.add(observation.source)
        if trusted_context:
            host_consistent_sources.add(observation.source)
            content_roles.add(source_context["content_role"])
        if trusted_context and source_context["editorial_control_group"]:
            control_groups.add(source_context["editorial_control_group"])
        else:
            unknown_sources.add(observation.source)
        publisher = (
            get_actor_profile(source_context["publisher_actor_id"])
            if trusted_context and source_context["publisher_actor_id"] else None
        )
        passages = extract_passages(observation)
        compared_passages.append((observation.id, passages))
        mention_scan = scan_actor_mentions(passages)
        mentions = mention_scan.mentions
        if mention_scan.truncated:
            mention_truncated_observations.add(observation.id)
        for mention in mentions:
            state = visibility[mention.actor_id]
            state["observation_ids"].add(observation.id)
            state["source_ids"].add(observation.source)
            if trusted_context and source_context["editorial_control_group"]:
                state["editorial_control_groups"].add(
                    source_context["editorial_control_group"]
                )
            state["mentions"] += 1

        rhetoric = {"status": "not-requested"}
        if include_rhetoric:
            if rhetoric_budget_exhausted:
                rhetoric = {"status": "skipped", "code": "rhetorlint_casefile_limit"}
            else:
                try:
                    candidate = rhetoric_analyzer(observation)
                    if not isinstance(candidate, dict) or not isinstance(
                        candidate.get("summary"), dict
                    ):
                        raise TypeError("invalid rhetoric result")
                    candidate_bytes = len(json.dumps(
                        candidate, ensure_ascii=False, separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8"))
                    candidate_marks = candidate["summary"].get("matched_tells", 0)
                    if (
                        not isinstance(candidate_marks, int)
                        or isinstance(candidate_marks, bool)
                        or candidate_marks < 0
                        or rhetoric_bytes + candidate_bytes > MAX_RHETORIC_CASEFILE_BYTES
                        or rhetoric_marks + candidate_marks > MAX_RHETORIC_CASEFILE_MARKS
                    ):
                        rhetoric_budget_exhausted = True
                        rhetoric = {
                            "status": "skipped", "code": "rhetorlint_casefile_limit",
                        }
                    else:
                        rhetoric = candidate
                        rhetoric_bytes += candidate_bytes
                        rhetoric_marks += candidate_marks
                except (
                    RhetorLintError, TypeError, ValueError, OverflowError,
                    RecursionError,
                ) as exc:
                    code = (
                        exc.code if isinstance(exc, RhetorLintError)
                        else "rhetorlint_invalid_output"
                    )
                    rhetoric = {"status": "failed", "code": code}
        records.append({
            "projection": projection,
            "source_context": source_context,
            "source_identity": identity,
            "publisher_actor": publisher.as_dict() if publisher else None,
            "collection_metadata": {
                "legacy_source_confidence": observation.source_confidence,
                "scope": (
                    "Legacy collector-supplied capture/parse hint only; it is not source "
                    "credibility, factual confidence, corroboration, or truth, and new "
                    "adapters default it to not-assessed."
                ),
            },
            "passages": [
                {
                    "passage_id": passage.passage_id,
                    "field": passage.field,
                    "transform_version": passage.transform_version,
                    "input_sha256": passage.input_sha256,
                    "utf8_bytes": passage.utf8_bytes,
                    "truncated": passage.truncated,
                    "text": passage.text,
                }
                for passage in passages
            ],
            "actor_mentions": [mention.as_dict() for mention in mentions],
            "actor_mention_scan": mention_scan.metadata(),
            "rhetoric": rhetoric,
        })

    actor_visibility = []
    for actor_id, state in visibility.items():
        profile = get_actor_profile(actor_id)
        if profile is None:
            continue
        actor_visibility.append({
            "actor": profile.as_dict(),
            "mention_count": state["mentions"],
            "observation_ids": sorted(state["observation_ids"]),
            "source_ids": sorted(state["source_ids"]),
            "editorial_control_groups": sorted(state["editorial_control_groups"]),
            "scope": (
                "Visible identity mentions only. Mention volume and claimed-source diversity "
                "are not power, responsibility, agreement, or corroboration."
            ),
        })
    actor_visibility.sort(key=lambda item: (
        -len(item["observation_ids"]),
        -item["mention_count"],
        item["actor"]["canonical_name"],
    ))

    return {
        "casefile_version": CASEFILE_VERSION,
        "fingerprint_sha256": _fingerprint_snapshots(
            fingerprint_observations, registry_hashes
        ),
        "contracts": {
            "sources": SOURCE_CONTRACT,
            "actors": ACTOR_CONTRACT,
            "source_identity": SOURCE_IDENTITY_VERSION,
            "actor_mentions": ACTOR_MENTION_VERSION,
            "passages": PASSAGE_TRANSFORM_VERSION,
            "projection": PROJECTION_VERSION,
            "assessment": "ww3.assessment/v1",
            "shared_text": SHARED_TEXT_VERSION,
        },
        "registry_artifacts_sha256": registry_hashes,
        "observation_ids": ids,
        "source_diversity": {
            "source_ids": sorted(source_ids),
            "registered_source_ids": sorted(registered_sources),
            "host_consistent_source_ids": sorted(host_consistent_sources),
            "content_roles": sorted(content_roles),
            "claimed_content_roles": sorted(claimed_content_roles),
            "known_editorial_control_groups": sorted(control_groups),
            "unknown_lineage_source_ids": sorted(unknown_sources),
            # The counts above and the measure below belong together: a reader
            # meeting "five publishers" here meets, in the same object, how much
            # visible text those five records hold in common.
            "shared_visible_text": shared_visible_text(compared_passages),
            "scope": (
                "Only HTTPS canonical-host-consistent records enter known control-group "
                "counts. Host consistency is not publisher authentication. These are still "
                "publication-context counts, not independent corroboration; different "
                "publishers may share a wire, quote one origin, or repeat a claim. "
                "shared_visible_text measures how much text these records share so that "
                "possibility is checkable rather than only stated; it establishes neither "
                "a shared origin where it measures much nor independence where it "
                "measures nothing."
            ),
        },
        "observations": records,
        "actor_visibility": actor_visibility,
        "actor_visibility_status": {
            "complete": not mention_truncated_observations,
            "truncated_observation_ids": sorted(mention_truncated_observations),
            "scope": (
                "Visibility uses bounded alias matching. If incomplete, later aliases or "
                "passages may be omitted; the emitted ordering is not a complete actor "
                "ranking and never represents power."
            ),
        },
        "epistemic_status": {
            "truth": "not-adjudicated",
            "claim_corroboration": "not-assessed",
            "intent": "not-inferred",
            "power": "not-scored",
            "next_layer": (
                "Agent or human assessments must cite observation/passage IDs, keep "
                "alternatives and falsifiers, and treat RhetorLint marks as language "
                "observations rather than intent evidence."
            ),
        },
    }


__all__ = [
    "CASEFILE_VERSION", "MAX_CASEFILE_OBSERVATIONS", "MAX_RHETORIC_OBSERVATIONS",
    "MAX_RHETORIC_CASEFILE_BYTES", "MAX_RHETORIC_CASEFILE_MARKS",
    "SOURCE_IDENTITY_VERSION", "build_casefile", "casefile_fingerprint",
]
