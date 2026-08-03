import json

import pytest

from core.observations import get_or_create_observation
from core.db import Observation
from interpretation.actors import MAX_MENTIONS_PER_OBSERVATION, text_mentions_actor
from interpretation.casefile import CASEFILE_VERSION, build_casefile, casefile_fingerprint
from interpretation.shared_text import MAX_COMPARED_OBSERVATIONS, SHARED_TEXT_VERSION


_SOURCE_URLS = {
    "un_press": "https://press.un.org/en/example",
    "bbc_world": "https://www.bbc.co.uk/news/example",
    "aljazeera": "https://www.aljazeera.com/news/example",
    "dw_world": "https://www.dw.com/en/example",
    "lemonde": "https://www.lemonde.fr/international/example",
}

_WIRE_COPY = (
    "The Security Council met on Tuesday to discuss the ceasefire proposal, "
    "diplomats said, after three days of talks in Geneva that produced no "
    "agreement on monitoring arrangements."
)


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


def test_casefile_measures_shared_text_beside_the_publisher_counts():
    outlets = ["bbc_world", "aljazeera", "dw_world", "lemonde"]
    observations = [
        _observation(source, f"wire-{source}", "Council meets over ceasefire", _WIRE_COPY)
        for source in outlets
    ]

    diversity = build_casefile(observations)["source_diversity"]
    shared = diversity["shared_visible_text"]

    assert len(diversity["known_editorial_control_groups"]) == 4
    assert shared["contract"] == SHARED_TEXT_VERSION
    assert shared["pairs_compared"] == 6
    assert shared["pairs_reported"] == 6
    assert {tuple(pair["observation_ids"]) for pair in shared["pairs"]} == {
        (first.id, second.id)
        for index, first in enumerate(observations)
        for second in observations[index + 1:]
    }
    assert all(pair["containment"] == 1.0 for pair in shared["pairs"])
    assert shared["status"] == "complete"
    assert "shared_visible_text measures" in diversity["scope"]


def test_casefile_shared_text_names_only_the_passages_it_compared():
    observation = _observation(
        "un_press", "shared-single", "Council meets over ceasefire", _WIRE_COPY,
    )

    value = build_casefile([observation])
    shared = value["source_diversity"]["shared_visible_text"]
    record = shared["records"][0]

    assert shared["pairs_compared"] == 0
    assert shared["pairs"] == []
    assert record["passage_ids"] == [
        item["passage_id"] for item in value["observations"][0]["passages"]
    ]
    assert value["contracts"]["shared_text"] == SHARED_TEXT_VERSION


def test_casefile_shared_text_reports_the_observations_it_left_out():
    observations = [
        _observation("un_press", f"shared-cap-{index}", "Council meets", _WIRE_COPY)
        for index in range(MAX_COMPARED_OBSERVATIONS + 1)
    ]

    shared = build_casefile(observations)["source_diversity"]["shared_visible_text"]

    assert shared["observations_not_compared"] == [observations[-1].id]
    assert shared["bounds"]["caps_hit"] == ["observation_cap"]
    assert shared["status"] == "bounded"


def test_casefile_fingerprint_ignores_shared_text_parameters(monkeypatch):
    first = _observation("un_press", "fingerprint-shared-a", "Council", _WIRE_COPY)
    second = _observation("bbc_world", "fingerprint-shared-b", "Council", _WIRE_COPY)

    baseline = build_casefile([first, second])
    monkeypatch.setattr("interpretation.shared_text.SHINGLE_TOKENS", 3)
    retuned = build_casefile([first, second])

    # A tuned parameter must move the measure and leave the fingerprint alone,
    # or every assessment already written against these records fails
    # revalidation the moment the threshold is argued with.
    assert (
        retuned["source_diversity"]["shared_visible_text"]
        != baseline["source_diversity"]["shared_visible_text"]
    )
    assert retuned["fingerprint_sha256"] == baseline["fingerprint_sha256"]
    assert baseline["fingerprint_sha256"] == casefile_fingerprint([first, second])


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
