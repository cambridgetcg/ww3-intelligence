import hashlib

import pytest

from core.registry import ActorProfile
from interpretation.actors import (
    ACTOR_MENTION_VERSION,
    MATCH_MODE_BOUNDARY_GUARDED,
    MATCH_MODE_LEADING_GUARDED,
    MATCH_MODE_UNGUARDED,
    MATCH_MODES,
    MAX_MENTION_CANDIDATES_PER_PASSAGE,
    MAX_MENTIONS_PER_OBSERVATION,
    MAX_MENTIONS_PER_PASSAGE,
    _alias_pattern,
    _scan_passage,
    scan_actor_mentions,
    text_mentions_actor,
)
from interpretation.rhetorlint import Passage


def _passage(text, passage_id="p1"):
    encoded = text.encode("utf-8")
    return Passage(
        passage_id=passage_id,
        field="summary",
        text=text,
        truncated=False,
        utf8_bytes=len(encoded),
        input_sha256=hashlib.sha256(encoded).hexdigest(),
    )


def _profile(actor_id, *aliases, case_sensitive=False):
    """Build one in-test identity so the shipped registry stays out of scope."""
    return ActorProfile(
        actor_id=actor_id,
        canonical_name=actor_id.split(":")[-1],
        kind="state",
        aliases=tuple((alias, case_sensitive) for alias in aliases),
    )


def _mentions(text, *profiles):
    accepted, _, _ = _scan_passage(_passage(text), profiles)
    return accepted


def _adjacency(mention):
    return mention.word_character_before, mention.word_character_after


@pytest.mark.parametrize("alias, text, start, mode", [
    ("中国", "中国政府表示", 0, MATCH_MODE_UNGUARDED),
    ("中国", "关于中国的声明", 2, MATCH_MODE_UNGUARDED),
    ("日本", "日本政府は発表した", 0, MATCH_MODE_UNGUARDED),
    ("ロシア", "ロシアは発表した", 0, MATCH_MODE_UNGUARDED),
    ("러시아", "러시아는 말했다", 0, MATCH_MODE_LEADING_GUARDED),
    ("ไทย", "รัฐบาลไทยแถลง", 6, MATCH_MODE_UNGUARDED),
    ("China", "China said today", 0, MATCH_MODE_BOUNDARY_GUARDED),
    ("Россия", "Россия заявила", 0, MATCH_MODE_BOUNDARY_GUARDED),
])
def test_alias_visibility_holds_across_spaced_and_unspaced_scripts(
    alias, text, start, mode
):
    mentions = _mentions(text, _profile("state:x", alias))

    assert [
        (item.actual, item.start, item.end, item.match_mode) for item in mentions
    ] == [(alias, start, start + len(alias), mode)]


@pytest.mark.parametrize("alias, text", [
    ("US", "USA said today"),
    ("Iran", "Iranian officials spoke"),
    ("China", "Chinatown reopened"),
    ("Иран", "Иранский представитель"),
])
def test_spaced_script_aliases_keep_the_guard_that_protects_them(alias, text):
    assert _mentions(text, _profile("state:x", alias)) == []
    assert len(_mentions(f"the {alias} spoke", _profile("state:x", alias))) == 1


def test_hangul_guards_only_the_edge_a_particle_cannot_reach():
    profile = _profile("state:russia", "러시아")

    for text in ("러시아는 말했다", "러시아에서 열린 회담"):
        mention = _mentions(text, profile)[0]
        assert mention.match_mode == MATCH_MODE_LEADING_GUARDED
        assert _adjacency(mention) == (False, True)

    # Named limit: a Korean prefix attaches with no space either, and the
    # leading guard this alias keeps cannot see past it.
    assert _mentions("주러시아 대사관", profile) == []


def test_mixed_script_alias_takes_the_weakest_guard_it_contains():
    profile = _profile("org:huawei", "华为 Technologies")

    embedded = _mentions("华为 Technologies的声明", profile)[0]
    spaced = _mentions("华为 Technologies said today", profile)[0]

    assert embedded.match_mode == MATCH_MODE_UNGUARDED
    assert embedded.word_character_after is True
    assert spaced.match_mode == MATCH_MODE_UNGUARDED
    assert spaced.word_character_after is False
    # The Latin sibling alias of the same actor is unaffected by the mixed one.
    assert _mentions("Huaweiism", _profile("org:huawei", "Huawei")) == []


def test_unguarded_substring_mentions_are_labelled_rather_than_suppressed():
    profile = _profile("state:china", "中国")

    subject = _mentions("中国政府表示", profile)[0]
    compound = _mentions("中国人的看法", profile)[0]
    seam = _mentions("美中国际关系", profile)[0]

    assert all(
        item.match_mode == MATCH_MODE_UNGUARDED
        for item in (subject, compound, seam)
    )
    assert _adjacency(subject) == (False, True)
    assert _adjacency(compound) == (False, True)
    assert _adjacency(seam) == (True, True)
    # A consumer wanting the stricter reading can reconstruct it exactly: the
    # adjacency flags are the test the dropped guard would have applied.
    assert seam.start == 1


def test_mention_dict_records_the_guards_the_span_and_the_encoding():
    han = _mentions("关于中国的声明", _profile("state:china", "中国"))[0]
    latin = _mentions("China said today", _profile("state:china", "China"))[0]

    assert han.as_dict() == {
        "actor_id": "state:china",
        "canonical_name": "china",
        "passage_id": "p1",
        "field": "summary",
        "actual": "中国",
        "span": {"start": 2, "end": 4, "offset_encoding": "unicode-code-point"},
        "match": {
            "mode": MATCH_MODE_UNGUARDED,
            "leading_guard": False,
            "trailing_guard": False,
            "adjacent_word_character": {"before": True, "after": True},
        },
    }
    assert latin.as_dict()["match"] == {
        "mode": MATCH_MODE_BOUNDARY_GUARDED,
        "leading_guard": True,
        "trailing_guard": True,
        "adjacent_word_character": {"before": False, "after": False},
    }


def test_offsets_stay_unicode_code_points_past_an_astral_character():
    mention = _mentions("🌏中国政府表示", _profile("state:china", "中国"))[0]

    assert (mention.start, mention.end) == (1, 3)
    assert "🌏中国政府表示"[mention.start:mention.end] == mention.actual


def test_overlap_resolution_prefers_the_longest_identity_across_scripts():
    china = _profile("state:china", "中国", "China")
    bank = _profile("org:pboc", "中国人民银行")
    republic = _profile("state:prc", "中华人民共和国")

    nested = _mentions("中国人民银行发表声明", china, bank)
    long_form = _mentions("中华人民共和国政府", republic, _profile("state:x", "中华"))
    mixed = _mentions("China met 中国人民银行", china, bank)

    assert [(item.actor_id, item.start, item.end) for item in nested] == [
        ("org:pboc", 0, 6),
    ]
    assert [(item.actor_id, item.start, item.end) for item in long_form] == [
        ("state:prc", 0, 7),
    ]
    assert [(item.actor_id, item.actual) for item in mixed] == [
        ("state:china", "China"), ("org:pboc", "中国人民银行"),
    ]


def test_scan_metadata_names_every_match_mode_and_the_contract(monkeypatch):
    monkeypatch.setattr(
        "interpretation.actors.actor_profiles",
        lambda: (_profile("state:china", "中国"), _profile("state:us", "United States")),
    )

    scan = scan_actor_mentions((
        _passage("中国政府表示", "p1"),
        _passage("The United States replied.", "p2"),
    ))

    assert scan.metadata()["contract"] == ACTOR_MENTION_VERSION
    assert scan.metadata()["match_modes"] == {
        MATCH_MODE_BOUNDARY_GUARDED: 1,
        MATCH_MODE_LEADING_GUARDED: 0,
        MATCH_MODE_UNGUARDED: 1,
    }
    assert tuple(scan.metadata()["match_modes"]) == MATCH_MODES
    assert scan.metadata()["limits"] == {
        "candidates_per_passage": MAX_MENTION_CANDIDATES_PER_PASSAGE,
        "mentions_per_passage": MAX_MENTIONS_PER_PASSAGE,
        "mentions_per_observation": MAX_MENTIONS_PER_OBSERVATION,
    }
    assert scan.truncated is False


def test_bounds_still_bite_on_unspaced_text(monkeypatch):
    profile = _profile("state:china", "中国")
    accepted, candidates, truncated = _scan_passage(_passage("中国" * 5_000), (profile,))

    assert candidates == MAX_MENTION_CANDIDATES_PER_PASSAGE
    assert len(accepted) == MAX_MENTIONS_PER_PASSAGE
    assert truncated is True

    monkeypatch.setattr("interpretation.actors.actor_profiles", lambda: (profile,))
    scan = scan_actor_mentions(tuple(
        _passage("中国" * 300, f"p{index}") for index in range(3)
    ))

    assert len(scan.mentions) == MAX_MENTIONS_PER_OBSERVATION
    assert scan.passages_scanned == 2
    assert scan.truncated is True
    assert scan.metadata()["match_modes"][MATCH_MODE_UNGUARDED] == (
        MAX_MENTIONS_PER_OBSERVATION
    )


def test_alias_patterns_are_cached_on_the_alias_not_the_passage():
    _alias_pattern.cache_clear()
    profile = _profile("state:china", "中国")

    _mentions("中国政府表示", profile)
    _mentions("关于中国的声明", profile)

    assert _alias_pattern("中国", False)[0] is _alias_pattern("中国", False)[0]
    assert _alias_pattern.cache_info().misses == 1
    assert _alias_pattern.cache_info().hits >= 2


def test_text_mentions_actor_sees_unspaced_scripts_and_keeps_latin_guards(monkeypatch):
    monkeypatch.setattr(
        "interpretation.actors.actor_profiles",
        lambda: (_profile("state:china", "中国", "China"), _profile("state:us", "US")),
    )

    assert text_mentions_actor("state:china", "中国政府表示新的立场。") is True
    assert text_mentions_actor("state:china", "关于中国的声明") is True
    assert text_mentions_actor("state:us", "USA issued a statement.") is False
    assert text_mentions_actor("state:us", "The US issued a statement.") is True
    assert text_mentions_actor("state:china", "") is False


@pytest.mark.parametrize("alias, spaced, prefixed", [
    ("الصين", "قالت الصين اليوم", "والصين اليوم"),
    ("רוסיה", "אמרה רוסיה היום", "הרוסיה אמרה"),
])
def test_attached_prefix_scripts_stay_guarded_as_a_named_limit(alias, spaced, prefixed):
    profile = _profile("state:x", alias)

    assert _mentions(spaced, profile)[0].match_mode == MATCH_MODE_BOUNDARY_GUARDED
    # Arabic and Hebrew space their words, so the bare alias is still visible;
    # an attached prefix hides that occurrence and is not handled here.
    assert _mentions(prefixed, profile) == []


def test_zero_width_alias_cannot_spray_mentions_between_characters():
    assert _mentions("中国政府表示", _profile("state:x", "")) == []
