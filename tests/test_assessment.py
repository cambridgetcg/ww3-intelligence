import json
from pathlib import Path

import pytest

from core.observations import get_or_create_observation
from interpretation.assessment import AssessmentValidationError, validate_assessment
from interpretation.casefile import build_casefile


def _fixture(
    *,
    source_record_id="statement-1",
    canonical_url="https://www.whitehouse.gov/releases/statement-1/",
    statement="The United States will seek a negotiated agreement.",
):
    observation = get_or_create_observation({
        "source": "white_house",
        "source_record_id": source_record_id,
        "type": "statement",
        "canonical_url": canonical_url,
        "raw_text": json.dumps({
            "title": "United States statement",
            "summary": statement,
        }),
    })[0]
    casefile = build_casefile([observation])
    passage = next(
        item for item in casefile["observations"][0]["passages"]
        if item["field"] == "summary"
    )
    actual = statement
    payload = {
        "contract": "ww3.assessment/v1",
        "casefile_fingerprint_sha256": casefile["fingerprint_sha256"],
        "observation_ids": [observation.id],
        "producer": {"kind": "agent", "name": "test-agent", "version": "1"},
        "claims": [{
            "claim_id": "claim:declared-position-1",
            "observation_id": observation.id,
            "passage_id": passage["passage_id"],
            "span": {
                "start": passage["text"].index(actual),
                "end": passage["text"].index(actual) + len(actual),
                "offset_encoding": "unicode-code-point",
            },
            "actual": actual,
            "proposition": actual,
            "representation": "verbatim",
            "claim_kind": "public-position",
            "subject_actor_ids": ["state:us"],
            "attribution": {"actor_id": "state:us", "mode": "explicit-speaker"},
        }],
        "power_bases": [],
        "intent_hypotheses": [{
            "hypothesis_id": "intent:negotiations-1",
            "actor_id": "state:us",
            "topic": "negotiations",
            "hypothesis": "The actor may be seeking room for a negotiated agreement.",
            "rationale": "The public position is consistent with that intent but does not prove follow-through.",
            "time_scope": {"start": "2026-07-17T12:00:00Z", "end": None},
            "workflow_state": "draft",
            "evidence": [{
                "claim_id": "claim:declared-position-1",
                "effect": "supports",
                "reason": "The actor publicly described negotiation as an intended course.",
            }],
            "alternatives": ["The statement may be positioning without planned follow-through."],
            "falsifiers": ["Subsequent documented action rejects every negotiation channel."],
        }],
        "contradictions": [],
        "unknowns": ["No independent evidence of follow-through is included."],
    }
    return payload


def test_valid_assessment_links_exact_claim_and_falsifiable_intent_hypothesis():
    result = validate_assessment(_fixture())

    assert result["claims"][0]["actual"].startswith("The United States")
    assert result["intent_hypotheses"][0]["evidence"][0]["claim_id"] == \
        "claim:declared-position-1"
    assert "confidence" not in json.dumps(result)
    assert "not mind-reading" in result["scope"]


def test_assessment_rejects_fingerprint_or_exact_span_mismatch():
    bad_fingerprint = _fixture()
    bad_fingerprint["casefile_fingerprint_sha256"] = "0" * 64
    with pytest.raises(AssessmentValidationError, match="fingerprint"):
        validate_assessment(bad_fingerprint)

    bad_span = _fixture()
    bad_span["claims"][0]["actual"] = "A different sentence"
    with pytest.raises(AssessmentValidationError, match="does not match"):
        validate_assessment(bad_span)

    false_verbatim = _fixture()
    false_verbatim["claims"][0]["proposition"] = "An invented proposition."
    with pytest.raises(AssessmentValidationError, match="must equal actual"):
        validate_assessment(false_verbatim)

    paraphrase_support = _fixture()
    paraphrase_support["claims"][0]["representation"] = "paraphrase"
    paraphrase_support["claims"][0]["proposition"] = (
        "The actor described negotiation as a possible course."
    )
    with pytest.raises(AssessmentValidationError, match="verbatim action"):
        validate_assessment(paraphrase_support)
    paraphrase_support["intent_hypotheses"] = []
    assert validate_assessment(paraphrase_support)["claims"][0]["representation"] == \
        "paraphrase"


def test_intent_requires_claim_support_alternative_and_falsifier():
    payload = _fixture()
    payload["intent_hypotheses"][0]["evidence"] = []
    with pytest.raises(AssessmentValidationError, match="between 1 and 100"):
        validate_assessment(payload)

    payload = _fixture()
    payload["intent_hypotheses"][0]["alternatives"] = []
    with pytest.raises(AssessmentValidationError, match="between 1 and 20"):
        validate_assessment(payload)

    payload = _fixture()
    payload["intent_hypotheses"][0]["falsifiers"] = []
    with pytest.raises(AssessmentValidationError, match="between 1 and 20"):
        validate_assessment(payload)


def test_assessment_rejects_truthy_score_fields_and_unknown_actor():
    payload = _fixture()
    payload["intent_hypotheses"][0]["confidence"] = 0.99
    with pytest.raises(AssessmentValidationError, match="unsupported fields"):
        validate_assessment(payload)

    payload = _fixture()
    payload["intent_hypotheses"][0]["actor_id"] = "person:unknown"
    with pytest.raises(AssessmentValidationError, match="actor identity registry"):
        validate_assessment(payload)


def test_intent_evidence_must_be_unique_and_about_the_actor():
    payload = _fixture()
    payload["intent_hypotheses"][0]["evidence"].append(
        dict(payload["intent_hypotheses"][0]["evidence"][0])
    )
    with pytest.raises(AssessmentValidationError, match="duplicate claim IDs"):
        validate_assessment(payload)

    payload = _fixture()
    payload["claims"][0]["subject_actor_ids"] = ["state:china"]
    with pytest.raises(AssessmentValidationError, match="action or public-position"):
        validate_assessment(payload)

    payload = _fixture()
    payload["claims"][0]["claim_kind"] = "capacity"
    with pytest.raises(AssessmentValidationError, match="action or public-position"):
        validate_assessment(payload)


def test_power_basis_requires_capacity_evidence_about_actor_and_no_score():
    payload = _fixture()
    payload["intent_hypotheses"] = []
    payload["power_bases"] = [{
        "power_basis_id": "power:us-diplomatic-1",
        "actor_id": "state:us",
        "domain": "diplomatic",
        "description": "The actor has documented negotiation capacity.",
        "scope": {
            "relationship": "self-capacity",
            "geographies": ["global"],
            "operations": ["formal diplomatic negotiation"],
            "related_actor_ids": [],
        },
        "time_scope": {"start": "2026-07-17T12:00:00Z", "end": None},
        "workflow_state": "draft",
        "evidence": [{
            "claim_id": "claim:declared-position-1",
            "effect": "supports",
            "reason": "The cited passage describes use of a negotiation channel.",
        }],
        "limitations": ["A stated position does not measure implementation capacity."],
        "falsifiers": ["Evidence establishes that no negotiation channel exists."],
    }]

    with pytest.raises(AssessmentValidationError, match="capacity claim"):
        validate_assessment(payload)

    payload["claims"][0]["claim_kind"] = "capacity"
    result = validate_assessment(payload)
    assert result["power_bases"][0]["actor_id"] == "state:us"
    assert "power_score" not in json.dumps(result)


def test_power_basis_requires_contextual_scope_and_bilateral_counterparty():
    payload = _fixture()
    payload["intent_hypotheses"] = []
    payload["claims"][0]["claim_kind"] = "capacity"
    payload["power_bases"] = [{
        "power_basis_id": "power:us-diplomatic-2",
        "actor_id": "state:us",
        "domain": "diplomatic",
        "description": "The actor has documented negotiation capacity.",
        "scope": {
            "relationship": "bilateral-leverage",
            "geographies": ["global"],
            "operations": ["formal diplomatic negotiation"],
            "related_actor_ids": [],
        },
        "time_scope": {"start": "2026-07-17T12:00:00Z", "end": None},
        "workflow_state": "draft",
        "evidence": [{
            "claim_id": "claim:declared-position-1",
            "effect": "supports",
            "reason": "The passage describes use of a negotiation channel.",
        }],
        "limitations": ["A stated position does not measure implementation capacity."],
        "falsifiers": ["Evidence establishes that no negotiation channel exists."],
    }]

    with pytest.raises(AssessmentValidationError, match="counterparty"):
        validate_assessment(payload)

    payload["power_bases"][0]["scope"]["related_actor_ids"] = ["state:china"]
    with pytest.raises(AssessmentValidationError, match="joint verbatim"):
        validate_assessment(payload)

    payload["power_bases"][0]["scope"]["related_actor_ids"].append("state:russia")
    with pytest.raises(AssessmentValidationError, match="exactly one"):
        validate_assessment(payload)

    payload = _fixture(
        source_record_id="bilateral-capacity",
        statement=(
            "The United States and China maintain a formal diplomatic negotiation channel."
        ),
    )
    payload["intent_hypotheses"] = []
    payload["claims"][0]["claim_kind"] = "capacity"
    payload["claims"][0]["subject_actor_ids"] = ["state:us", "state:china"]
    payload["power_bases"] = [{
        "power_basis_id": "power:us-china-diplomatic",
        "actor_id": "state:us",
        "domain": "diplomatic",
        "description": "The actors have a documented bilateral negotiation channel.",
        "scope": {
            "relationship": "bilateral-leverage",
            "geographies": ["global"],
            "operations": ["formal diplomatic negotiation"],
            "related_actor_ids": ["state:china"],
        },
        "time_scope": {"start": "2026-07-17T12:00:00Z", "end": None},
        "workflow_state": "draft",
        "evidence": [{
            "claim_id": "claim:declared-position-1",
            "effect": "supports",
            "reason": "The cited passage names both actors and the channel.",
        }],
        "limitations": ["The passage does not measure use or effectiveness."],
        "falsifiers": ["Evidence establishes that the named channel does not exist."],
    }]
    assert validate_assessment(payload)["power_bases"][0]["scope"] == \
        payload["power_bases"][0]["scope"]


def test_assessment_enforces_publisher_identity_and_unresolved_attribution():
    payload = _fixture()
    payload["claims"][0]["attribution"] = {
        "actor_id": "state:us", "mode": "publisher",
    }
    with pytest.raises(AssessmentValidationError, match="lacks registry and host"):
        validate_assessment(payload)

    payload["claims"][0]["attribution"] = {
        "actor_id": "org:white-house", "mode": "publisher",
    }
    assert validate_assessment(payload)["claims"][0]["attribution"]["actor_id"] == \
        "org:white-house"

    payload = _fixture(
        source_record_id="statement-spoof",
        canonical_url="https://attacker.example/spoof",
    )
    payload["claims"][0]["attribution"] = {
        "actor_id": "org:white-house", "mode": "publisher",
    }
    with pytest.raises(AssessmentValidationError, match="lacks registry and host"):
        validate_assessment(payload)

    payload = _fixture()
    payload["claims"][0]["attribution"]["mode"] = "unresolved"
    with pytest.raises(AssessmentValidationError, match="requires a null actor"):
        validate_assessment(payload)


def test_assessment_time_scope_and_normalized_output_are_stable():
    payload = _fixture()
    payload["intent_hypotheses"][0]["time_scope"] = {"start": None, "end": None}
    with pytest.raises(AssessmentValidationError, match="temporal bound"):
        validate_assessment(payload)

    payload = _fixture()
    payload["intent_hypotheses"][0]["time_scope"]["start"] = \
        "2026-07-17 12:00:00+00:00"
    with pytest.raises(AssessmentValidationError, match="RFC 3339"):
        validate_assessment(payload)

    normalized = validate_assessment(_fixture())
    assert validate_assessment(normalized) == normalized


def test_portable_schema_tracks_power_subject_and_evidence_contract():
    schema = json.loads(
        (Path(__file__).parents[1] / "spec" / "assessment.schema.json").read_text()
    )

    assert "power_bases" in schema["required"]
    assert "subject_actor_ids" in schema["$defs"]["claim"]["required"]
    assert "scope" in schema["$defs"]["powerBasis"]["required"]
    assert schema["$defs"]["powerScope"]["additionalProperties"] is False
    assert "evidence" in schema["$defs"]["intent"]["required"]
    assert schema["$defs"]["evidenceLink"]["additionalProperties"] is False
