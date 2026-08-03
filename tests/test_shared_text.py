import hashlib
import json

import pytest

from interpretation.rhetorlint import Passage
from interpretation.shared_text import (
    CAP_OBSERVATIONS,
    CAP_TOKENS,
    MAX_COMPARED_OBSERVATIONS,
    MAX_SHARED_RUN_TOKENS,
    REASON_BELOW_MINIMUM,
    REPORT_THRESHOLD,
    SHARED_TEXT_VERSION,
    build_stream,
    compare_streams,
    comparison_tokens,
    shared_visible_text,
)


_WIRE = (
    "The Security Council met on Tuesday to discuss the ceasefire proposal, "
    "diplomats said, after three days of talks in Geneva that produced no "
    "agreement on monitoring arrangements."
)
_RERUN = (
    "Editors note. The Security Council met on Tuesday to discuss the ceasefire "
    "proposal, diplomats said, after three days of talks in Geneva that produced "
    "no agreement on monitoring arrangements. The council reconvenes on Friday."
)
_SAME_EVENT_OTHER_WORDS = (
    "Talks about halting the fighting ended without result. Envoys in "
    "Switzerland could not settle who would watch the front line, and the "
    "fifteen-member body will try again later this week."
)
_TRANSLATION = (
    "Le Conseil de securite s'est reuni mardi pour examiner la proposition de "
    "cessez-le-feu, ont declare des diplomates, apres trois jours de "
    "pourparlers a Geneve sans accord sur les modalites de surveillance."
)


def _passage(text, passage_id="p1", field="summary"):
    encoded = text.encode("utf-8")
    return Passage(
        passage_id=passage_id,
        field=field,
        text=text,
        truncated=False,
        utf8_bytes=len(encoded),
        input_sha256=hashlib.sha256(encoded).hexdigest(),
    )


def _records(*texts, first_id=1):
    return [
        (first_id + index, (_passage(text, passage_id=f"p{first_id + index}"),))
        for index, text in enumerate(texts)
    ]


def _compare(first, second):
    return compare_streams(
        build_stream(1, (_passage(first, passage_id="a"),)),
        build_stream(2, (_passage(second, passage_id="b"),)),
    )


def test_near_identical_records_report_shared_text_with_a_visible_measure():
    pair = _compare(_WIRE, _RERUN)

    assert pair["reaches_threshold"] is True
    assert pair["containment"] == 1.0
    assert pair["shared_shingles"] == pair["distinct_shingles"][0]
    assert pair["longest_shared_run"]["normalised_text"].startswith(
        "the security council met on tuesday"
    )
    assert pair["longest_shared_run"]["passage_id"] == "a"


def test_same_event_in_different_words_is_not_reported_as_shared_text():
    pair = _compare(_WIRE, _SAME_EVENT_OTHER_WORDS)

    assert pair["comparable"] is True
    assert pair["shared_shingles"] == 0
    assert pair["reaches_threshold"] is False
    assert pair["longest_shared_run"] is None


def test_translation_of_one_text_is_not_reported_and_is_not_called_independent():
    section = shared_visible_text(_records(_WIRE, _TRANSLATION))

    assert section["pairs"] == []
    assert section["pairs_below_threshold"] == 1
    assert section["highest_containment_below_threshold"] == 0.0
    # The one place a reader meets this result must say what it cannot mean, or
    # a measured zero between a story and its translation reads as two
    # independent reports of the same event.
    assert "not evidence of independence" in section["scope"]
    assert "translation" in section["scope"]


def test_measures_are_recomputable_from_the_emitted_integers():
    pair = _compare(_WIRE, _RERUN)
    first, second = pair["distinct_shingles"]
    shared = pair["shared_shingles"]

    assert pair["containment"] == round(shared / min(first, second), 4)
    assert pair["jaccard"] == round(shared / (first + second - shared), 4)


@pytest.mark.parametrize("other", [_RERUN, _SAME_EVENT_OTHER_WORDS, _TRANSLATION])
def test_threshold_verdict_is_the_one_a_reader_recomputes(other):
    pair = _compare(_WIRE, other)

    assert pair["reaches_threshold"] == (pair["containment"] >= REPORT_THRESHOLD)


def test_result_is_stable_across_runs_and_independent_of_record_order():
    forward = _records(_WIRE, _SAME_EVENT_OTHER_WORDS, _RERUN)
    reversed_order = list(reversed(forward))

    first = json.dumps(shared_visible_text(forward), sort_keys=True)
    again = json.dumps(shared_visible_text(forward), sort_keys=True)
    other = json.dumps(shared_visible_text(reversed_order), sort_keys=True)

    assert first == again == other


def test_shingles_never_span_two_passages():
    stream = build_stream(1, (
        _passage("alpha bravo charlie delta echo", passage_id="title"),
        _passage("foxtrot golf hotel india juliett", passage_id="summary"),
    ))

    assert stream.distinct == {
        ("alpha", "bravo", "charlie", "delta", "echo"),
        ("foxtrot", "golf", "hotel", "india", "juliett"),
    }
    assert stream.passage_ids == ("title", "summary")


def test_text_too_short_to_measure_is_reported_rather_than_answered_zero():
    section = shared_visible_text(_records("Ceasefire talks", "Ceasefire talks"))

    assert section["pairs"] == []
    assert section["pairs_compared"] == 1
    assert section["pairs_not_comparable"] == [{
        "observation_ids": [1, 2],
        "distinct_shingles": [0, 0],
        "comparable": False,
        "reason": REASON_BELOW_MINIMUM,
    }]
    assert section["records"][0]["comparable"] is False


def test_unspaced_script_records_are_measured_rather_than_skipped():
    han = "安全理事会星期二开会讨论停火提议外交官说日内瓦会谈没有达成监督安排协议"
    pair = _compare(han, "编者按。" + han)

    assert pair["comparable"] is True
    assert pair["containment"] == 1.0
    assert comparison_tokens("中国政府 said HELLO-world")[:4] == [
        "中", "国", "政", "府",
    ]


def test_single_record_yields_no_pair_and_stays_complete():
    section = shared_visible_text(_records(_WIRE))

    assert section["contract"] == SHARED_TEXT_VERSION
    assert section["pairs_compared"] == 0
    assert section["pairs"] == []
    assert section["highest_containment_below_threshold"] is None
    assert section["status"] == "complete"
    assert len(section["records"]) == 1


def test_observation_cap_bites_by_observation_id_and_is_reported():
    many = _records(*[_WIRE] * (MAX_COMPARED_OBSERVATIONS + 2))
    section = shared_visible_text(many)
    shuffled = shared_visible_text(list(reversed(many)))

    compared = [record["observation_id"] for record in section["records"]]
    assert compared == list(range(1, MAX_COMPARED_OBSERVATIONS + 1))
    assert section["observations_not_compared"] == [
        MAX_COMPARED_OBSERVATIONS + 1, MAX_COMPARED_OBSERVATIONS + 2,
    ]
    assert section["pairs_compared"] == 28
    assert section["status"] == "bounded"
    assert section["bounds"]["caps_hit"] == [CAP_OBSERVATIONS]
    assert shuffled["observations_not_compared"] == section["observations_not_compared"]


def test_token_cap_bites_and_travels_with_the_pair(monkeypatch):
    monkeypatch.setattr("interpretation.shared_text.MAX_TOKENS_PER_OBSERVATION", 12)
    section = shared_visible_text(_records(_WIRE, _RERUN))

    assert section["records"][0]["token_count"] == 12
    assert section["records"][0]["token_limit_hit"] is True
    assert section["status"] == "bounded"
    assert section["bounds"]["caps_hit"] == [CAP_TOKENS]
    assert section["bounds"]["max_tokens_per_observation"] == 12
    assert section["pairs"][0]["token_limit_hit"] is True


def test_shared_run_sample_is_bounded_and_marked_when_cut():
    pair = _compare(_WIRE, _RERUN)
    run = pair["longest_shared_run"]

    assert run["truncated"] is True
    assert run["token_count"] > MAX_SHARED_RUN_TOKENS
    assert len(run["normalised_text"].split(" ")) == MAX_SHARED_RUN_TOKENS


def test_reporting_threshold_is_emitted_with_the_parameters_that_produced_it():
    method = shared_visible_text(_records(_WIRE, _RERUN))["method"]

    assert method["report_threshold"] == REPORT_THRESHOLD
    assert method["decision_measure"] == "containment"
    assert method["shingle_tokens"] == 5
    assert method["minimum_comparable_shingles"] == 8


@pytest.mark.parametrize("word", [
    "syndicat", "wire", "copied", "origin", "plagiar", "corroborat", "credib",
    "independen",
])
def test_no_structured_field_states_a_finding_beyond_the_shared_characters(word):
    section = shared_visible_text(_records(_WIRE, _RERUN, _SAME_EVENT_OTHER_WORDS))
    for pair in section["pairs"]:
        # The sample is the records' own words, not this framework's wording.
        if pair["longest_shared_run"]:
            pair["longest_shared_run"]["normalised_text"] = ""
    # The three prose fields name what the measure cannot mean, so they carry
    # those words in the negative; every other field is a name or a number.
    section["scope"] = ""
    section["method"]["normalisation"] = ""
    section["method"]["arithmetic"] = ""

    assert word not in json.dumps(section, ensure_ascii=False)


def test_scope_refuses_the_inferences_a_high_measure_invites():
    scope = shared_visible_text(_records(_WIRE, _RERUN))["scope"]

    assert "not a finding that either record copied the other" in scope
    assert "share an origin, a wire, or an author" in scope
    assert "which was published first" in scope
    assert "syndicat" not in scope.lower()
