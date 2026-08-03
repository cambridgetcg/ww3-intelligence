import hashlib
import unicodedata

import pytest

from core.registry import (
    ALIAS_FORM_AS_REGISTERED,
    ALIAS_FORM_NFC,
    ALIAS_FORM_NFD,
    ALIAS_FORMS,
    ActorProfile,
    expand_alias_forms,
)
from interpretation.actors import (
    ACTOR_MENTION_VERSION,
    MATCH_MODE_BOUNDARY_GUARDED,
    MATCH_MODE_LEADING_GUARDED,
    MATCH_MODE_UNGUARDED,
    MATCH_MODES,
    MAX_MENTION_CANDIDATES_PER_PASSAGE,
    MAX_MENTIONS_PER_OBSERVATION,
    MAX_MENTIONS_PER_PASSAGE,
    _abuts_word_character,
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
    declared = tuple((alias, case_sensitive) for alias in aliases)
    return ActorProfile(
        actor_id=actor_id,
        canonical_name=actor_id.split(":")[-1],
        kind="state",
        aliases=declared,
        alias_forms=expand_alias_forms(declared),
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
            "alias_form": ALIAS_FORM_AS_REGISTERED,
            "leading_guard": False,
            "trailing_guard": False,
            "adjacent_word_character": {"before": True, "after": True},
        },
    }
    assert latin.as_dict()["match"] == {
        "mode": MATCH_MODE_BOUNDARY_GUARDED,
        "alias_form": ALIAS_FORM_AS_REGISTERED,
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
    assert scan.metadata()["alias_forms"] == {
        ALIAS_FORM_AS_REGISTERED: 2, ALIAS_FORM_NFC: 0, ALIAS_FORM_NFD: 0,
    }
    assert tuple(scan.metadata()["alias_forms"]) == ALIAS_FORMS
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


@pytest.mark.parametrize("alias_form, text_form, matched_form", [
    ("NFC", "NFC", ALIAS_FORM_AS_REGISTERED),
    ("NFC", "NFD", ALIAS_FORM_NFD),
    ("NFD", "NFD", ALIAS_FORM_AS_REGISTERED),
    ("NFD", "NFC", ALIAS_FORM_NFC),
])
@pytest.mark.parametrize("alias, sentence, mode", [
    ("러시아", "러시아는 말했다", MATCH_MODE_LEADING_GUARDED),
    ("Türkiye", "the Türkiye statement", MATCH_MODE_BOUNDARY_GUARDED),
    ("Việt Nam", "the Việt Nam statement", MATCH_MODE_BOUNDARY_GUARDED),
])
def test_an_alias_is_seen_in_either_normalization_the_publisher_serves(
    alias, sentence, mode, alias_form, text_form, matched_form
):
    profile = _profile("state:x", unicodedata.normalize(alias_form, alias))
    text = unicodedata.normalize(text_form, sentence)

    mention = _mentions(text, profile)[0]

    assert mention.match_mode == mode
    assert mention.alias_form == matched_form
    # The span indexes the text exactly as it arrived, not a normalized copy:
    # NFC and NFD of the same sentence are different lengths.
    assert text[mention.start:mention.end] == mention.actual
    assert mention.actual == unicodedata.normalize(text_form, alias)


def test_the_reproduced_decomposed_korean_case_answers_with_true_spans():
    profile = _profile("state:russia", "러시아")
    composed = unicodedata.normalize("NFC", "러시아는 말했다")
    decomposed = unicodedata.normalize("NFD", "러시아는 말했다")

    composed_mention = _mentions(composed, profile)[0]
    decomposed_mention = _mentions(decomposed, profile)[0]

    assert (len(composed), len(decomposed)) == (8, 18)
    assert (composed_mention.start, composed_mention.end) == (0, 3)
    # Six Jamo carry the same three syllables, so an offset taken from the
    # composed copy would end mid-word in the record actually stored.
    assert (decomposed_mention.start, decomposed_mention.end) == (0, 6)
    assert decomposed[0:6] == unicodedata.normalize("NFD", "러시아")
    assert decomposed_mention.alias_form == ALIAS_FORM_NFD
    assert decomposed_mention.match_mode == MATCH_MODE_LEADING_GUARDED
    assert _adjacency(decomposed_mention) == (False, True)


def test_ascii_aliases_gain_no_extra_spellings_to_search():
    latin = _profile("state:us", "United States")
    accented = _profile("state:turkey", "Türkiye")

    assert [form.form for form in latin.alias_forms] == [ALIAS_FORM_AS_REGISTERED]
    assert [form.form for form in accented.alias_forms] == [
        ALIAS_FORM_AS_REGISTERED, ALIAS_FORM_NFD,
    ]
    assert [form.text for form in accented.alias_forms] == [
        "Türkiye", unicodedata.normalize("NFD", "Türkiye"),
    ]


def test_guards_and_overlap_resolution_survive_a_decomposed_passage():
    profile = _profile("state:turkey", "Türkiye")
    republic = _profile("state:turkey-republic", "Türkiye Cumhuriyeti")
    text = unicodedata.normalize("NFD", "Türkiye Cumhuriyeti spoke")

    long_form = unicodedata.normalize("NFD", "Türkiye Cumhuriyeti")

    assert _mentions(unicodedata.normalize("NFD", "Türkiyeli yazar"), profile) == []
    assert [
        (item.actor_id, item.actual) for item in _mentions(text, profile, republic)
    ] == [("state:turkey-republic", long_form)]


def test_scan_metadata_counts_each_spelling_that_matched(monkeypatch):
    monkeypatch.setattr(
        "interpretation.actors.actor_profiles",
        lambda: (_profile("state:russia", "러시아"), _profile("state:china", "中国")),
    )

    scan = scan_actor_mentions((
        _passage(unicodedata.normalize("NFD", "러시아는 말했다"), "p1"),
        _passage("러시아는 말했다", "p2"),
        _passage("中国政府表示", "p3"),
    ))

    assert scan.metadata()["alias_forms"] == {
        ALIAS_FORM_AS_REGISTERED: 2, ALIAS_FORM_NFC: 0, ALIAS_FORM_NFD: 1,
    }
    assert scan.metadata()["mentions_emitted"] == 3


def test_alias_spellings_share_the_pattern_cache_without_recompiling():
    _alias_pattern.cache_clear()
    profile = _profile("state:russia", "러시아")
    decomposed = unicodedata.normalize("NFD", "러시아")

    _mentions("러시아는 말했다", profile)
    _mentions(unicodedata.normalize("NFD", "러시아에서 열린 회담"), profile)

    # One entry per distinct spelling, however many passages are scanned.
    assert _alias_pattern.cache_info().misses == 2
    assert _alias_pattern.cache_info().hits >= 2
    assert _alias_pattern(decomposed, False)[0] is _alias_pattern(decomposed, False)[0]


def test_text_mentions_actor_sees_a_decomposed_capacity_claim(monkeypatch):
    monkeypatch.setattr(
        "interpretation.actors.actor_profiles",
        lambda: (_profile("state:russia", "러시아"), _profile("state:us", "US")),
    )

    assert text_mentions_actor(
        "state:russia", unicodedata.normalize("NFD", "러시아는 말했다")
    ) is True
    assert text_mentions_actor("state:russia", "러시아는 말했다") is True
    assert text_mentions_actor("state:us", "USA issued a statement.") is False


def test_a_guard_is_not_fooled_by_the_marks_decomposed_text_carries():
    profile = _profile("state:x", "Ha")
    decomposed = unicodedata.normalize("NFD", "Hà Nội said")

    # The compiled guard alone answers here, because a combining acute is not
    # a word character and the alias appears to stand delimited.
    assert _alias_pattern("Ha", False)[0].search(decomposed) is not None
    assert _mentions(decomposed, profile) == []
    assert len(_mentions("Ha said", profile)) == 1
    # The same predicate is what a mention without that guard reports, so the
    # stricter reading stays reconstructable rather than silently asserted.
    assert _abuts_word_character(decomposed, 2) is True
    assert _abuts_word_character("Ha said", 2) is False
    assert _abuts_word_character(unicodedata.normalize("NFD", "भारता"), 4) is True


def test_shipped_registry_sees_decomposed_native_script_text():
    from core.registry import actor_profiles

    for actor_id, sentence in (
        ("state:russia", "Российская Федерация заявила"),
        ("state:turkey", "Türkiye Cumhuriyeti açıklama yaptı"),
    ):
        profile = next(item for item in actor_profiles() if item.actor_id == actor_id)
        text = unicodedata.normalize("NFD", sentence)

        mention = _mentions(text, profile)[0]

        assert mention.alias_form == ALIAS_FORM_NFD
        assert text[mention.start:mention.end] == mention.actual
