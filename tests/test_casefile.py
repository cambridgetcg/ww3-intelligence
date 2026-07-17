import json

import pytest

from core.observations import get_or_create_observation
from core.db import Observation
from interpretation.actors import MAX_MENTIONS_PER_OBSERVATION, text_mentions_actor
from interpretation.casefile import CASEFILE_VERSION, build_casefile, casefile_fingerprint


_SOURCE_URLS = {
    "un_press": "https://press.un.org/en/example",
    "bbc_world": "https://www.bbc.co.uk/news/example",
}


def _observation(source, source_record_id, title, summary):
    return get_or_create_observation({
        "source": source,
        "source_record_id": source_record_id,
        "type": "news",
        "canonical_url": _SOURCE_URLS.get(source, "https://example.test/report"),
        "raw_text": json.dumps({"title": title, "summary": summary}),
    })[0]


def test_casefile_separates_source_context_mentions_and_epistemic_limits():
    first = _observation(
        "un_press", "un-1", "United Nations meeting",
        "The United States and China addressed the Council.",
    )
    second = _observation(
        "bbc_world", "bbc-1", "China responds",
        "China responded to the United States statement.",
    )

    value = build_casefile([first, second])

    assert value["casefile_version"] == CASEFILE_VERSION
    assert value["epistemic_status"] == {
        "truth": "not-adjudicated",
        "claim_corroboration": "not-assessed",
        "intent": "not-inferred",
        "power": "not-scored",
        "next_layer": value["epistemic_status"]["next_layer"],
    }
    assert value["source_diversity"]["known_editorial_control_groups"] == [
        "group:bbc", "group:un",
    ]
    assert value["source_diversity"]["host_consistent_source_ids"] == [
        "bbc_world", "un_press",
    ]
    visibility = {item["actor"]["actor_id"]: item for item in value["actor_visibility"]}
    assert visibility["state:us"]["observation_ids"] == [first.id, second.id]
    assert visibility["state:china"]["mention_count"] == 3
    assert "power_score" not in json.dumps(value)
    assert value["observations"][0]["rhetoric"] == {"status": "not-requested"}
    assert value["observations"][0]["passages"][0]["text"] == "United Nations meeting"


def test_casefile_labels_unknown_source_lineage():
    observation = _observation("unregistered", "x", "Unknown publisher", "Russia replied.")

    value = build_casefile([observation])

    assert value["source_diversity"]["unknown_lineage_source_ids"] == ["unregistered"]
    assert value["observations"][0]["source_context"]["content_role"] == "unknown"


def test_casefile_does_not_promote_spoofed_registered_source_identity():
    observation = get_or_create_observation({
        "source": "white_house",
        "source_record_id": "spoofed",
        "type": "statement",
        "canonical_url": "https://attacker.example/not-the-white-house",
        "raw_text": json.dumps({"title": "Claimed release"}),
    })[0]

    value = build_casefile([observation])
    record = value["observations"][0]

    assert record["source_identity"]["status"] == "canonical-host-mismatch"
    assert record["publisher_actor"] is None
    assert value["source_diversity"]["known_editorial_control_groups"] == []
    assert value["source_diversity"]["unknown_lineage_source_ids"] == ["white_house"]


def test_casefile_rhetoric_is_explicit_and_failure_is_stable():
    observation = _observation("un_press", "r", "Statement", "Mistakes were made.")

    completed = build_casefile(
        [observation], include_rhetoric=True,
        rhetoric_analyzer=lambda item: {"status": "completed", "summary": {}},
    )

    assert completed["observations"][0]["rhetoric"]["status"] == "completed"


def test_casefile_rhetoric_has_observation_and_aggregate_caps(monkeypatch):
    observations = [
        _observation("un_press", f"rhetoric-cap-{index}", "Statement", "Text")
        for index in range(9)
    ]
    with pytest.raises(ValueError, match="exceeds 8 observations"):
        build_casefile(observations, include_rhetoric=True)

    monkeypatch.setattr("interpretation.casefile.MAX_RHETORIC_CASEFILE_BYTES", 1)
    bounded = build_casefile(
        observations[:1],
        include_rhetoric=True,
        rhetoric_analyzer=lambda item: {
            "status": "completed", "summary": {"matched_tells": 0},
        },
    )
    assert bounded["observations"][0]["rhetoric"] == {
        "status": "skipped", "code": "rhetorlint_casefile_limit",
    }


def test_casefile_rejects_duplicate_or_unstored_observations():
    observation = _observation("un_press", "d", "Statement", "Text")
    with pytest.raises(ValueError, match="unique"):
        build_casefile([observation, observation])
    with pytest.raises(TypeError, match="stored"):
        build_casefile([{"id": 1}])
    with pytest.raises(TypeError, match="stored"):
        build_casefile([Observation(id=999_999)])


def test_casefile_fingerprint_binds_immutable_evidence_metadata():
    first = _observation("un_press", "fingerprint-a", "Statement", "Text")
    second = get_or_create_observation({
        "source": "un_press",
        "source_record_id": "fingerprint-b",
        "type": "news",
        "canonical_url": "https://press.un.org/en/different-metadata",
        "raw_text": json.dumps({"title": "Statement", "summary": "Text"}),
    })[0]

    assert first.content_hash == second.content_hash
    assert casefile_fingerprint([first]) != casefile_fingerprint([second])


def test_actor_mentions_prefer_longest_identity_and_are_bounded():
    nested = _observation(
        "un_press", "nested", "Actors",
        "People's Republic of China met the United States Geological Survey.",
    )
    nested_value = build_casefile([nested])
    actor_ids = {
        item["actor_id"] for item in nested_value["observations"][0]["actor_mentions"]
    }
    assert actor_ids == {"state:china", "org:usgs"}
    assert text_mentions_actor(
        "org:usgs", "United States Geological Survey recorded an earthquake."
    ) is True
    assert text_mentions_actor(
        "state:us", "United States Geological Survey recorded an earthquake."
    ) is False

    repeated = _observation(
        "un_press", "bounded", "Actors", "China " * 5_000,
    )
    repeated_value = build_casefile([repeated])
    scan = repeated_value["observations"][0]["actor_mention_scan"]
    assert scan["mentions_emitted"] <= MAX_MENTIONS_PER_OBSERVATION
    assert scan["truncated"] is True
    assert repeated_value["actor_visibility_status"] == {
        "complete": False,
        "truncated_observation_ids": [repeated.id],
        "scope": repeated_value["actor_visibility_status"]["scope"],
    }
