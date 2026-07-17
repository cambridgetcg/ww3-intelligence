"""Strict validation for agent/human assessments over a WW3 casefile.

The contract keeps public claims separate from observed source records and
forces every intent hypothesis to cite claims, alternatives, and falsifiers.
It deliberately has no truth score, power score, intent probability, or
RhetorLint-derived motive field.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from core.db import Observation
from core.registry import get_actor_profile, get_source_profile, source_identity_context
from interpretation.actors import text_mentions_actor
from interpretation.casefile import casefile_fingerprint
from interpretation.rhetorlint import extract_passages


ASSESSMENT_CONTRACT = "ww3.assessment/v1"
ASSESSMENT_SCOPE = (
    "Claims remain attributed representations; power bases are evidence-linked "
    "descriptions with relational, geographic, operational, and temporal scope, not "
    "scores; intent entries are falsifiable hypotheses, not mind-reading. Publisher "
    "attribution requires registry and HTTPS canonical-host consistency, which is not "
    "authentication. Capability alone cannot support intent. Power and intent support "
    "must be verbatim and visibly name the actor. Validation does not make any entry true."
)
CLAIM_KINDS = {
    "measurement", "reported-action", "public-position", "forecast",
    "denial", "allegation", "capacity",
}
REPRESENTATIONS = {"verbatim", "paraphrase"}
ATTRIBUTION_MODES = {
    "explicit-speaker", "reported-speaker", "publisher", "unresolved",
}
WORKFLOW_STATES = {"draft", "active", "contested", "retired"}
PRODUCER_KINDS = {"human", "agent", "model"}
POWER_DOMAINS = {
    "military", "economic", "diplomatic", "institutional", "technological",
    "information", "logistics", "legal", "humanitarian", "other",
}
POWER_RELATIONSHIPS = {
    "self-capacity", "bilateral-leverage", "institutional-role",
    "network-position", "situational-access", "other",
}
EVIDENCE_EFFECTS = {"supports", "contradicts"}
INTENT_EVIDENCE_CLAIM_KINDS = {"reported-action", "public-position", "denial"}
_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


class AssessmentValidationError(ValueError):
    """Assessment violates the evidence-linking contract."""


def _object(value: Any, path: str, allowed: set[str], required: set[str]) -> dict:
    if not isinstance(value, dict):
        raise AssessmentValidationError(f"{path} must be an object")
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown:
        raise AssessmentValidationError(
            f"{path} has unsupported fields: {', '.join(sorted(unknown))}"
        )
    if missing:
        raise AssessmentValidationError(
            f"{path} is missing fields: {', '.join(sorted(missing))}"
        )
    return value


def _text(value: Any, path: str, maximum: int = 5_000, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AssessmentValidationError(f"{path} must be non-empty text")
    value = value.strip()
    if len(value) > maximum:
        raise AssessmentValidationError(f"{path} exceeds {maximum} characters")
    return value


def _exact_text(value: Any, path: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise AssessmentValidationError(f"{path} must be bounded non-empty text")
    return value


def _identifier(value: Any, path: str) -> str:
    value = _text(value, path, 200)
    if not _ID.fullmatch(value):
        raise AssessmentValidationError(f"{path} is not a valid identifier")
    return value


def _array(value: Any, path: str, maximum: int, *, minimum: int = 0) -> list:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise AssessmentValidationError(
            f"{path} must contain between {minimum} and {maximum} items"
        )
    return value


def _choice(value: Any, path: str, choices: set[str]) -> str:
    value = _text(value, path, 100)
    if value not in choices:
        raise AssessmentValidationError(f"{path} has unsupported value {value!r}")
    return value


def _integer(value: Any, path: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AssessmentValidationError(f"{path} must be an integer >= {minimum}")
    return value


def _known_actor(value: Any, path: str, *, optional=False) -> str | None:
    if value is None and optional:
        return None
    actor_id = _identifier(value, path)
    if get_actor_profile(actor_id) is None:
        raise AssessmentValidationError(f"{path} is not in the actor identity registry")
    return actor_id


def _unique_texts(value: Any, path: str, maximum: int, *, minimum=0) -> list[str]:
    result = [
        _text(item, f"{path}[{index}]", 2_000)
        for index, item in enumerate(_array(value, path, maximum, minimum=minimum))
    ]
    if len(set(result)) != len(result):
        raise AssessmentValidationError(f"{path} contains duplicates")
    return result


def _evidence_links(
    value: Any,
    path: str,
    claims_by_id: dict[str, dict],
) -> list[dict]:
    result = []
    seen = set()
    for index, raw_link in enumerate(_array(value, path, 100, minimum=1)):
        item_path = f"{path}[{index}]"
        link = _object(
            raw_link, item_path,
            {"claim_id", "effect", "reason"},
            {"claim_id", "effect", "reason"},
        )
        claim_id = _identifier(link["claim_id"], f"{item_path}.claim_id")
        if claim_id not in claims_by_id:
            raise AssessmentValidationError(f"{item_path} references an unknown claim ID")
        if claim_id in seen:
            raise AssessmentValidationError(f"{path} contains duplicate claim IDs")
        seen.add(claim_id)
        result.append({
            "claim_id": claim_id,
            "effect": _choice(link["effect"], f"{item_path}.effect", EVIDENCE_EFFECTS),
            "reason": _text(link["reason"], f"{item_path}.reason", 2_000),
        })
    return result


def _power_scope(value: Any, path: str, actor_id: str) -> dict:
    data = _object(
        value,
        path,
        {"relationship", "geographies", "operations", "related_actor_ids"},
        {"relationship", "geographies", "operations", "related_actor_ids"},
    )
    related_actor_ids = [
        _known_actor(item, f"{path}.related_actor_ids[{index}]")
        for index, item in enumerate(_array(
            data["related_actor_ids"], f"{path}.related_actor_ids", 20
        ))
    ]
    if len(set(related_actor_ids)) != len(related_actor_ids):
        raise AssessmentValidationError(f"{path}.related_actor_ids contains duplicates")
    if actor_id in related_actor_ids:
        raise AssessmentValidationError(
            f"{path}.related_actor_ids must not repeat the power-basis actor"
        )
    relationship = _choice(
        data["relationship"], f"{path}.relationship", POWER_RELATIONSHIPS
    )
    if relationship == "bilateral-leverage" and len(related_actor_ids) != 1:
        raise AssessmentValidationError(
            f"{path}.related_actor_ids needs exactly one counterparty for bilateral leverage"
        )
    return {
        "relationship": relationship,
        "geographies": _unique_texts(
            data["geographies"], f"{path}.geographies", 20, minimum=1
        ),
        "operations": _unique_texts(
            data["operations"], f"{path}.operations", 20, minimum=1
        ),
        "related_actor_ids": related_actor_ids,
    }


def _timestamp(value: Any, path: str) -> tuple[str | None, datetime | None]:
    if value is None:
        return None, None
    raw = _text(value, path, 50)
    if not _RFC3339.fullmatch(raw):
        raise AssessmentValidationError(f"{path} must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise AssessmentValidationError(f"{path} must be an RFC 3339 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AssessmentValidationError(f"{path} must include a timezone")
    normalized = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return normalized, parsed


def _time_scope(value: Any, path: str) -> dict[str, str | None]:
    data = _object(value, path, {"start", "end"}, {"start", "end"})
    start, start_dt = _timestamp(data["start"], f"{path}.start")
    end, end_dt = _timestamp(data["end"], f"{path}.end")
    if start_dt is None and end_dt is None:
        raise AssessmentValidationError(f"{path} needs at least one temporal bound")
    if start_dt is not None and end_dt is not None and start_dt > end_dt:
        raise AssessmentValidationError(f"{path}.start must not be after its end")
    return {"start": start, "end": end}


def _producer(value: Any) -> dict:
    data = _object(
        value,
        "producer",
        {"kind", "name", "version"},
        {"kind", "name", "version"},
    )
    return {
        "kind": _choice(data["kind"], "producer.kind", PRODUCER_KINDS),
        "name": _text(data["name"], "producer.name", 200),
        "version": _text(data["version"], "producer.version", 200),
    }


def _load_observations(ids: list[int]) -> list[Observation]:
    rows = {
        item.id: item
        for item in Observation.select().where(Observation.id.in_(ids))
    }
    missing = [item for item in ids if item not in rows]
    if missing:
        raise AssessmentValidationError(
            f"observation_ids contain missing IDs: {', '.join(map(str, missing))}"
        )
    return [rows[item] for item in ids]


def validate_assessment(payload: Any) -> dict:
    """Validate and normalize one evidence-linked assessment packet."""
    root = _object(
        payload,
        "assessment",
        {
            "contract", "casefile_fingerprint_sha256", "observation_ids",
            "producer", "claims", "power_bases", "intent_hypotheses",
            "contradictions", "unknowns", "scope",
        },
        {
            "contract", "casefile_fingerprint_sha256", "observation_ids",
            "producer", "claims", "power_bases", "intent_hypotheses",
            "contradictions", "unknowns",
        },
    )
    if root["contract"] != ASSESSMENT_CONTRACT:
        raise AssessmentValidationError("unsupported assessment contract")
    if "scope" in root and root["scope"] != ASSESSMENT_SCOPE:
        raise AssessmentValidationError("assessment scope text does not match the contract")
    raw_ids = _array(root["observation_ids"], "observation_ids", 100, minimum=1)
    observation_ids = [
        _integer(item, f"observation_ids[{index}]", 1)
        for index, item in enumerate(raw_ids)
    ]
    if len(set(observation_ids)) != len(observation_ids):
        raise AssessmentValidationError("observation_ids must be unique")
    observations = _load_observations(observation_ids)
    observations_by_id = {item.id: item for item in observations}
    fingerprint = _text(
        root["casefile_fingerprint_sha256"], "casefile_fingerprint_sha256", 64
    )
    if not _SHA256.fullmatch(fingerprint) or fingerprint != casefile_fingerprint(observations):
        raise AssessmentValidationError("casefile fingerprint does not match observations")

    passages = {}
    passage_owner = {}
    for observation in observations:
        for passage in extract_passages(observation):
            passages[passage.passage_id] = passage
            passage_owner[passage.passage_id] = observation.id

    claims = []
    claim_ids = set()
    for index, raw_claim in enumerate(_array(root["claims"], "claims", 200)):
        path = f"claims[{index}]"
        claim = _object(
            raw_claim,
            path,
            {
                "claim_id", "observation_id", "passage_id", "span", "actual",
                "proposition", "representation", "claim_kind", "subject_actor_ids",
                "attribution",
            },
            {
                "claim_id", "observation_id", "passage_id", "span", "actual",
                "proposition", "representation", "claim_kind", "subject_actor_ids",
                "attribution",
            },
        )
        claim_id = _identifier(claim["claim_id"], f"{path}.claim_id")
        if claim_id in claim_ids:
            raise AssessmentValidationError(f"duplicate claim_id {claim_id!r}")
        claim_ids.add(claim_id)
        observation_id = _integer(claim["observation_id"], f"{path}.observation_id", 1)
        passage_id = _text(claim["passage_id"], f"{path}.passage_id", 100)
        if passage_id not in passages or passage_owner[passage_id] != observation_id:
            raise AssessmentValidationError(f"{path}.passage_id is not owned by its observation")
        span = _object(
            claim["span"], f"{path}.span",
            {"start", "end", "offset_encoding"},
            {"start", "end", "offset_encoding"},
        )
        if span["offset_encoding"] != "unicode-code-point":
            raise AssessmentValidationError(f"{path}.span has unsupported offset encoding")
        start = _integer(span["start"], f"{path}.span.start")
        end = _integer(span["end"], f"{path}.span.end")
        passage = passages[passage_id]
        if end < start or end > len(passage.text):
            raise AssessmentValidationError(f"{path}.span is out of range")
        actual = _exact_text(claim["actual"], f"{path}.actual", 16_384)
        if passage.text[start:end] != actual:
            raise AssessmentValidationError(f"{path}.actual does not match the passage span")
        attribution = _object(
            claim["attribution"], f"{path}.attribution",
            {"actor_id", "mode"}, {"actor_id", "mode"},
        )
        mode = _choice(
            attribution["mode"], f"{path}.attribution.mode", ATTRIBUTION_MODES
        )
        actor_id = _known_actor(
            attribution["actor_id"], f"{path}.attribution.actor_id", optional=True
        )
        if mode == "unresolved" and actor_id is not None:
            raise AssessmentValidationError(
                f"{path}.attribution unresolved mode requires a null actor"
            )
        if mode != "unresolved" and actor_id is None:
            raise AssessmentValidationError(f"{path}.attribution needs a known actor")
        if mode == "publisher":
            observation = observations_by_id[observation_id]
            source_profile = get_source_profile(observation.source)
            if (
                source_profile is None
                or source_profile.publisher_actor_id != actor_id
                or source_identity_context(
                    observation.source,
                    observation.canonical_url,
                    synthetic=bool(observation.synthetic),
                )["status"]
                != "canonical-host-consistent"
            ):
                raise AssessmentValidationError(
                    f"{path}.attribution publisher lacks registry and host consistency"
                )
        subject_actor_ids = [
            _known_actor(item, f"{path}.subject_actor_ids[{subject_index}]")
            for subject_index, item in enumerate(_array(
                claim["subject_actor_ids"], f"{path}.subject_actor_ids", 20
            ))
        ]
        if len(set(subject_actor_ids)) != len(subject_actor_ids):
            raise AssessmentValidationError(f"{path}.subject_actor_ids contains duplicates")
        proposition = _text(claim["proposition"], f"{path}.proposition")
        representation = _choice(
            claim["representation"], f"{path}.representation", REPRESENTATIONS
        )
        if representation == "verbatim" and proposition != actual:
            raise AssessmentValidationError(
                f"{path}.proposition must equal actual when representation is verbatim"
            )
        claims.append({
            "claim_id": claim_id,
            "observation_id": observation_id,
            "passage_id": passage_id,
            "span": {"start": start, "end": end, "offset_encoding": "unicode-code-point"},
            "actual": actual,
            "proposition": proposition,
            "representation": representation,
            "claim_kind": _choice(claim["claim_kind"], f"{path}.claim_kind", CLAIM_KINDS),
            "subject_actor_ids": subject_actor_ids,
            "attribution": {"actor_id": actor_id, "mode": mode},
        })

    claims_by_id = {item["claim_id"]: item for item in claims}

    power_bases = []
    power_basis_ids = set()
    for index, raw_basis in enumerate(_array(root["power_bases"], "power_bases", 100)):
        path = f"power_bases[{index}]"
        basis = _object(
            raw_basis,
            path,
            {
                "power_basis_id", "actor_id", "domain", "description",
                "scope", "time_scope", "workflow_state", "evidence",
                "limitations", "falsifiers",
            },
            {
                "power_basis_id", "actor_id", "domain", "description",
                "scope", "time_scope", "workflow_state", "evidence",
                "limitations", "falsifiers",
            },
        )
        power_basis_id = _identifier(
            basis["power_basis_id"], f"{path}.power_basis_id"
        )
        if power_basis_id in power_basis_ids:
            raise AssessmentValidationError(
                f"duplicate power_basis_id {power_basis_id!r}"
            )
        power_basis_ids.add(power_basis_id)
        actor_id = _known_actor(basis["actor_id"], f"{path}.actor_id")
        power_scope = _power_scope(basis["scope"], f"{path}.scope", actor_id)
        evidence = _evidence_links(basis["evidence"], f"{path}.evidence", claims_by_id)
        supporting = [item["claim_id"] for item in evidence if item["effect"] == "supports"]
        if not supporting:
            raise AssessmentValidationError(f"{path} needs supporting evidence")
        if not any(
            claims_by_id[claim_id]["claim_kind"] == "capacity"
            and actor_id in claims_by_id[claim_id]["subject_actor_ids"]
            and claims_by_id[claim_id]["representation"] == "verbatim"
            and text_mentions_actor(actor_id, claims_by_id[claim_id]["actual"])
            for claim_id in supporting
        ):
            raise AssessmentValidationError(
                f"{path} needs a verbatim capacity claim visibly naming its actor"
            )
        for related_actor_id in power_scope["related_actor_ids"]:
            if not any(
                claims_by_id[claim_id]["claim_kind"] == "capacity"
                and actor_id in claims_by_id[claim_id]["subject_actor_ids"]
                and related_actor_id in claims_by_id[claim_id]["subject_actor_ids"]
                and claims_by_id[claim_id]["representation"] == "verbatim"
                and text_mentions_actor(actor_id, claims_by_id[claim_id]["actual"])
                and text_mentions_actor(
                    related_actor_id, claims_by_id[claim_id]["actual"]
                )
                for claim_id in supporting
            ):
                raise AssessmentValidationError(
                    f"{path}.scope related actor needs joint verbatim capacity evidence"
                )
        power_bases.append({
            "power_basis_id": power_basis_id,
            "actor_id": actor_id,
            "domain": _choice(basis["domain"], f"{path}.domain", POWER_DOMAINS),
            "description": _text(basis["description"], f"{path}.description"),
            "scope": power_scope,
            "time_scope": _time_scope(basis["time_scope"], f"{path}.time_scope"),
            "workflow_state": _choice(
                basis["workflow_state"], f"{path}.workflow_state", WORKFLOW_STATES
            ),
            "evidence": evidence,
            "limitations": _unique_texts(
                basis["limitations"], f"{path}.limitations", 20, minimum=1
            ),
            "falsifiers": _unique_texts(
                basis["falsifiers"], f"{path}.falsifiers", 20, minimum=1
            ),
        })

    hypotheses = []
    hypothesis_ids = set()
    for index, raw_hypothesis in enumerate(
        _array(root["intent_hypotheses"], "intent_hypotheses", 50)
    ):
        path = f"intent_hypotheses[{index}]"
        hypothesis = _object(
            raw_hypothesis,
            path,
            {
                "hypothesis_id", "actor_id", "topic", "hypothesis", "rationale",
                "time_scope", "workflow_state", "evidence",
                "alternatives", "falsifiers",
            },
            {
                "hypothesis_id", "actor_id", "topic", "hypothesis", "rationale",
                "time_scope", "workflow_state", "evidence",
                "alternatives", "falsifiers",
            },
        )
        hypothesis_id = _identifier(
            hypothesis["hypothesis_id"], f"{path}.hypothesis_id"
        )
        if hypothesis_id in hypothesis_ids:
            raise AssessmentValidationError(f"duplicate hypothesis_id {hypothesis_id!r}")
        hypothesis_ids.add(hypothesis_id)
        evidence = _evidence_links(
            hypothesis["evidence"], f"{path}.evidence", claims_by_id
        )
        supporting = [item["claim_id"] for item in evidence if item["effect"] == "supports"]
        if not supporting:
            raise AssessmentValidationError(f"{path} needs supporting evidence")
        actor_id = _known_actor(hypothesis["actor_id"], f"{path}.actor_id")
        if not any(
            actor_id in claims_by_id[claim_id]["subject_actor_ids"]
            and claims_by_id[claim_id]["claim_kind"] in INTENT_EVIDENCE_CLAIM_KINDS
            and claims_by_id[claim_id]["representation"] == "verbatim"
            and text_mentions_actor(actor_id, claims_by_id[claim_id]["actual"])
            for claim_id in supporting
        ):
            raise AssessmentValidationError(
                f"{path} needs verbatim action or public-position evidence visibly naming its actor"
            )
        hypotheses.append({
            "hypothesis_id": hypothesis_id,
            "actor_id": actor_id,
            "topic": _text(hypothesis["topic"], f"{path}.topic", 500),
            "hypothesis": _text(hypothesis["hypothesis"], f"{path}.hypothesis"),
            "rationale": _text(hypothesis["rationale"], f"{path}.rationale"),
            "time_scope": _time_scope(
                hypothesis["time_scope"], f"{path}.time_scope"
            ),
            "workflow_state": _choice(
                hypothesis["workflow_state"], f"{path}.workflow_state", WORKFLOW_STATES
            ),
            "evidence": evidence,
            "alternatives": _unique_texts(
                hypothesis["alternatives"], f"{path}.alternatives", 20, minimum=1
            ),
            "falsifiers": _unique_texts(
                hypothesis["falsifiers"], f"{path}.falsifiers", 20, minimum=1
            ),
        })

    contradictions = []
    contradiction_pairs = set()
    for index, raw_relation in enumerate(
        _array(root["contradictions"], "contradictions", 100)
    ):
        path = f"contradictions[{index}]"
        relation = _object(
            raw_relation, path,
            {"left_claim_id", "right_claim_id", "explanation"},
            {"left_claim_id", "right_claim_id", "explanation"},
        )
        left = _identifier(relation["left_claim_id"], f"{path}.left_claim_id")
        right = _identifier(relation["right_claim_id"], f"{path}.right_claim_id")
        if left == right or left not in claim_ids or right not in claim_ids:
            raise AssessmentValidationError(f"{path} must reference two different claims")
        pair = frozenset((left, right))
        if pair in contradiction_pairs:
            raise AssessmentValidationError(f"{path} duplicates a contradiction pair")
        contradiction_pairs.add(pair)
        contradictions.append({
            "left_claim_id": left,
            "right_claim_id": right,
            "explanation": _text(relation["explanation"], f"{path}.explanation"),
        })

    return {
        "contract": ASSESSMENT_CONTRACT,
        "casefile_fingerprint_sha256": fingerprint,
        "observation_ids": observation_ids,
        "producer": _producer(root["producer"]),
        "claims": claims,
        "power_bases": power_bases,
        "intent_hypotheses": hypotheses,
        "contradictions": contradictions,
        "unknowns": _unique_texts(root["unknowns"], "unknowns", 100),
        "scope": ASSESSMENT_SCOPE,
    }


__all__ = [
    "ASSESSMENT_CONTRACT", "ASSESSMENT_SCOPE", "EVIDENCE_EFFECTS",
    "INTENT_EVIDENCE_CLAIM_KINDS", "POWER_DOMAINS", "AssessmentValidationError",
    "validate_assessment",
]
