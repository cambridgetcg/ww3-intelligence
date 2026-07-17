import pytest

from analysis.schema import (
    LLMValidationError,
    validate_horizon_payload,
    validate_llm_payload,
)


def _payload(**overrides):
    value = {
        "event_title": "Ceasefire proposal reported",
        "event_category": "political",
        "classification": "reported",
        "significance": 5,
        "summary": "One source reports a ceasefire proposal; independent corroboration is absent.",
    }
    value.update(overrides)
    return value


def test_minimal_payload_is_normalized():
    result = validate_llm_payload(_payload())

    assert result["event_category"] == "political"
    assert result["actors"] == []
    assert result["evidence"] == []
    assert result["scenarios"] == []
    assert result["escalation_level"] == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_category", "diplomacy"),
        ("classification", "certain"),
        ("significance", 11),
        ("significance", 2.5),
    ],
)
def test_invalid_core_fields_are_rejected(field, value):
    with pytest.raises(LLMValidationError):
        validate_llm_payload(_payload(**{field: value}))


def test_root_scenario_probabilities_must_sum_to_one_hundred():
    scenarios = [
        {"text": "Talks continue", "probability": 60},
        {"text": "Talks stop", "probability": 30},
    ]

    with pytest.raises(LLMValidationError, match="sum to 100"):
        validate_llm_payload(_payload(scenarios=scenarios))


def test_nested_output_and_source_links_are_validated():
    result = validate_llm_payload(_payload(
        evidence=[{
            "type": "source_claim",
            "description": "A named official made the proposal.",
            "confidence": "medium",
            "source_url": "https://example.test/report",
        }],
        narratives=[{
            "type": "official_state",
            "content": "The proposal is described as temporary.",
            "credibility_score": 5,
            "disinfo_flag": "unverified",
        }],
        scenarios=[
            {"text": "Proposal is accepted", "probability": 40, "children": []},
            {"text": "Proposal is rejected", "probability": 60, "children": []},
        ],
    ))

    assert result["evidence"][0]["source_url"] == "https://example.test/report"
    assert result["narratives"][0]["disinfo_flag"] == "unverified"


def test_unknown_impact_fields_are_rejected():
    with pytest.raises(LLMValidationError, match="unsupported fields"):
        validate_llm_payload(_payload(impact={"vibes_global": 10}))


def test_terminal_control_characters_are_rejected():
    with pytest.raises(LLMValidationError, match="control characters"):
        validate_llm_payload(_payload(event_title="unsafe\u009b31m title"))


def test_horizon_forecasts_must_be_complete_unique_and_ordered():
    expected = ["1h", "1d"]
    payload = {
        "event_assessment": "Uncertain trajectory.",
        "horizons": [
            {
                "horizon": "1d",
                "outcome": "Formal talks begin",
                "probability": 40,
                "confidence": "low",
                "reasoning": "A mediator has offered talks.",
            },
            {
                "horizon": "1h",
                "outcome": "A public response is issued",
                "probability": 70,
                "confidence": "medium",
                "reasoning": "The report is already circulating.",
            },
        ],
    }

    result = validate_horizon_payload(payload, expected)
    assert [item["horizon"] for item in result["horizons"]] == expected

    payload["horizons"] = payload["horizons"][:1]
    with pytest.raises(LLMValidationError, match="missing forecast horizons"):
        validate_horizon_payload(payload, expected)
