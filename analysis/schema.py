"""Strict validation for model-produced intelligence records.

The LLM is an untrusted parser.  Nothing it returns reaches persistence until
it passes the small, explicit contract in this module.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping
from typing import Any


class LLMValidationError(ValueError):
    """Raised when model output does not match the intelligence contract."""


EVENT_CATEGORIES = {"seismic", "military", "political", "cyber", "economic", "other"}
CLASSIFICATIONS = {"speculation", "reported", "confirmed"}
ACTOR_TYPES = {"state", "non-state", "individual", "organization"}
ACTOR_ROLES = {"perpetrator", "victim", "observer", "beneficiary", "mediator"}
CONFIDENCE_LEVELS = {"low", "medium", "high"}
NARRATIVE_TYPES = {
    "official_state", "opposition", "western_intel", "eastern_intel",
    "social_media", "independent",
}
DISINFO_FLAGS = {"fabricated", "misleading", "unverified", "credible"}
IMPACT_FIELDS = {
    "military_regional", "military_global", "economic_regional", "economic_global",
    "humanitarian_regional", "humanitarian_global", "diplomatic_regional",
    "diplomatic_global",
}
TECHNICAL_CATEGORIES = EVENT_CATEGORIES | {"chemical", "nuclear"}


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LLMValidationError(f"{path} must be an object")
    return value


def _items(value: Any, path: str, *, maximum: int) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LLMValidationError(f"{path} must be an array")
    if len(value) > maximum:
        raise LLMValidationError(f"{path} exceeds the {maximum}-item limit")
    return value


def _text(
    value: Any,
    path: str,
    *,
    required: bool = True,
    maximum: int = 5_000,
) -> str | None:
    if value is None:
        if required:
            raise LLMValidationError(f"{path} is required")
        return None
    if not isinstance(value, str):
        raise LLMValidationError(f"{path} must be text")
    if any(
        unicodedata.category(character) == "Cc" and character not in "\n\r\t"
        for character in value
    ):
        raise LLMValidationError(f"{path} contains unsupported control characters")
    value = value.strip()
    if required and not value:
        raise LLMValidationError(f"{path} cannot be empty")
    if len(value) > maximum:
        raise LLMValidationError(f"{path} exceeds the {maximum}-character limit")
    return value or None


def _integer(value: Any, path: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise LLMValidationError(f"{path} must be an integer")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise LLMValidationError(f"{path} must be an integer") from None
    if not number.is_integer():
        raise LLMValidationError(f"{path} must be an integer")
    number = int(number)
    if not minimum <= number <= maximum:
        raise LLMValidationError(f"{path} must be between {minimum} and {maximum}")
    return number


def _choice(value: Any, path: str, choices: set[str], *, default: str | None = None) -> str:
    if value is None and default is not None:
        value = default
    value = _text(value, path, maximum=100)
    if value not in choices:
        raise LLMValidationError(f"{path} has unsupported value {value!r}")
    return value


def _validate_scenario(item: Any, path: str, depth: int, budget: list[int]) -> dict[str, Any]:
    if depth > 4:
        raise LLMValidationError(f"{path} exceeds the maximum scenario depth")
    budget[0] += 1
    if budget[0] > 100:
        raise LLMValidationError("scenarios exceed the 100-node limit")
    data = _mapping(item, path)
    children = [
        _validate_scenario(child, f"{path}.children[{index}]", depth + 1, budget)
        for index, child in enumerate(_items(data.get("children"), f"{path}.children", maximum=20))
    ]
    return {
        "text": _text(data.get("text"), f"{path}.text", maximum=2_000),
        "probability": _integer(
            data.get("probability", 50), f"{path}.probability", minimum=0, maximum=100
        ),
        "timeframe": _text(
            data.get("timeframe"), f"{path}.timeframe", required=False, maximum=200
        ),
        "trigger": _text(data.get("trigger"), f"{path}.trigger", required=False, maximum=2_000),
        "children": children,
    }


def validate_llm_payload(payload: Any) -> dict[str, Any]:
    """Validate and normalize one leaf-analysis response.

    This deliberately performs no permissive object coercion: malformed or
    out-of-range analysis is retriable failure, not intelligence.
    """
    data = _mapping(payload, "response")

    actors = []
    for index, raw_actor in enumerate(_items(data.get("actors"), "actors", maximum=50)):
        path = f"actors[{index}]"
        actor = _mapping(raw_actor, path)
        actors.append({
            "name": _text(actor.get("name"), f"{path}.name", maximum=200),
            "type": _choice(actor.get("type"), f"{path}.type", ACTOR_TYPES, default="state"),
            "country": _text(actor.get("country"), f"{path}.country", required=False, maximum=100),
            "role": _choice(actor.get("role"), f"{path}.role", ACTOR_ROLES, default="observer"),
            "capability_tier": _integer(
                actor.get("capability_tier", 3), f"{path}.capability_tier", minimum=1, maximum=5
            ),
            "motivation": _text(
                actor.get("motivation"), f"{path}.motivation", required=False, maximum=5_000
            ),
        })

    evidence = []
    for index, raw_evidence in enumerate(_items(data.get("evidence"), "evidence", maximum=100)):
        path = f"evidence[{index}]"
        item = _mapping(raw_evidence, path)
        evidence.append({
            "type": _text(item.get("type"), f"{path}.type", maximum=100),
            "description": _text(item.get("description"), f"{path}.description", maximum=5_000),
            "confidence": _choice(
                item.get("confidence"), f"{path}.confidence", CONFIDENCE_LEVELS, default="medium"
            ),
            "source_url": _text(
                item.get("source_url"), f"{path}.source_url", required=False, maximum=2_000
            ),
        })

    narratives = []
    for index, raw_narrative in enumerate(
        _items(data.get("narratives"), "narratives", maximum=50)
    ):
        path = f"narratives[{index}]"
        item = _mapping(raw_narrative, path)
        disinfo_flag = item.get("disinfo_flag")
        narratives.append({
            "type": _choice(
                item.get("type"), f"{path}.type", NARRATIVE_TYPES, default="independent"
            ),
            "content": _text(item.get("content"), f"{path}.content", maximum=5_000),
            "credibility_score": _integer(
                item.get("credibility_score", 5),
                f"{path}.credibility_score",
                minimum=1,
                maximum=10,
            ),
            "disinfo_flag": (
                _choice(disinfo_flag, f"{path}.disinfo_flag", DISINFO_FLAGS)
                if disinfo_flag is not None else None
            ),
        })

    technical_raw = data.get("technical") or {}
    technical_map = _mapping(technical_raw, "technical")
    technical: dict[str, Any] = {}
    if technical_map:
        fields = _mapping(technical_map.get("fields") or {}, "technical.fields")
        try:
            encoded_fields = json.dumps(fields, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise LLMValidationError(f"technical.fields is not JSON-serializable: {exc}") from None
        if len(encoded_fields) > 50_000:
            raise LLMValidationError("technical.fields exceeds the 50,000-character limit")
        technical = {
            "category_type": _choice(
                technical_map.get("category_type"),
                "technical.category_type",
                TECHNICAL_CATEGORIES,
                default="other",
            ),
            "fields": dict(fields),
        }

    impact_raw = data.get("impact") or {}
    impact_map = _mapping(impact_raw, "impact")
    unknown_impact = set(impact_map) - IMPACT_FIELDS
    if unknown_impact:
        raise LLMValidationError(f"impact has unsupported fields: {', '.join(sorted(unknown_impact))}")
    impact = {
        key: _integer(value, f"impact.{key}", minimum=1, maximum=10)
        for key, value in impact_map.items()
    }

    budget = [0]
    scenarios = [
        _validate_scenario(item, f"scenarios[{index}]", 1, budget)
        for index, item in enumerate(_items(data.get("scenarios"), "scenarios", maximum=20))
    ]
    if scenarios and sum(item["probability"] for item in scenarios) != 100:
        raise LLMValidationError("root scenario probabilities must sum to 100")

    escalation_region = _text(
        data.get("escalation_region"), "escalation_region", required=False, maximum=200
    )
    escalation_level = _integer(
        data.get("escalation_level", 1), "escalation_level", minimum=1, maximum=10
    )

    return {
        "event_title": _text(data.get("event_title"), "event_title", maximum=240),
        "event_category": _choice(
            data.get("event_category"), "event_category", EVENT_CATEGORIES
        ),
        "classification": _choice(
            data.get("classification"), "classification", CLASSIFICATIONS
        ),
        "significance": _integer(
            data.get("significance"), "significance", minimum=1, maximum=10
        ),
        "summary": _text(data.get("summary"), "summary", maximum=5_000),
        "actors": actors,
        "evidence": evidence,
        "narratives": narratives,
        "escalation_region": escalation_region or "",
        "escalation_level": escalation_level,
        "technical": technical,
        "impact": impact,
        "scenarios": scenarios,
    }


def validate_horizon_payload(payload: Any, expected_horizons: list[str]) -> dict[str, Any]:
    """Validate a complete set of distinct, horizon-specific forecasts."""
    data = _mapping(payload, "response")
    expected = list(expected_horizons)
    expected_set = set(expected)
    items = _items(data.get("horizons"), "horizons", maximum=len(expected))
    normalized = []
    seen: set[str] = set()
    for index, raw_item in enumerate(items):
        path = f"horizons[{index}]"
        item = _mapping(raw_item, path)
        code = _choice(item.get("horizon"), f"{path}.horizon", expected_set)
        if code in seen:
            raise LLMValidationError(f"duplicate forecast horizon {code!r}")
        seen.add(code)
        normalized.append({
            "horizon": code,
            "outcome": _text(item.get("outcome"), f"{path}.outcome", maximum=2_000),
            "probability": _integer(
                item.get("probability"), f"{path}.probability", minimum=0, maximum=100
            ),
            "confidence": _choice(
                item.get("confidence"),
                f"{path}.confidence",
                CONFIDENCE_LEVELS,
                default="medium",
            ),
            "reasoning": _text(item.get("reasoning"), f"{path}.reasoning", maximum=5_000),
        })

    missing = [code for code in expected if code not in seen]
    if missing:
        raise LLMValidationError(f"missing forecast horizons: {', '.join(missing)}")
    order = {code: index for index, code in enumerate(expected)}
    normalized.sort(key=lambda item: order[item["horizon"]])
    return {
        "event_assessment": _text(
            data.get("event_assessment"), "event_assessment", required=False, maximum=2_000
        ) or "",
        "horizons": normalized,
    }
