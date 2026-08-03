import hashlib
import json
import unicodedata
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli.commands import cli
from core.registry import (
    ACTOR_CONTRACT,
    ALIAS_FORM_AS_REGISTERED,
    ALIAS_FORM_NFC,
    ALIAS_FORM_NFD,
    SOURCE_CONTRACT,
    actor_profiles,
    alias_identity_key,
    expand_alias_forms,
    registry_payload,
    rss_source_profiles,
    source_profiles,
)
from core.text_safety import contains_unsafe_controls


# No endpoint below has been fetched from this machine, so each entry stays out
# of collection until a human confirms the feed.
_UNVERIFIED_SOURCE_IDS = frozenset({
    "china_mfa", "iran_mfa", "israel_mfa", "india_mea", "ukraine_mfa",
    "japan_mofa", "turkey_mfa", "saudi_mofa", "france_diplomatie",
    "germany_federal_foreign_office", "kcna", "aljazeera_arabic", "lemonde",
    "xinhua", "nhk_news",
})
_NATIVE_SCRIPT_ALIASES = {
    "state:china": ("中国", "中华人民共和国", "中華人民共和國"),
    "state:russia": ("Россия", "Российская Федерация"),
    "state:japan": ("日本", "日本国"),
    "state:iran": ("ایران", "جمهوری اسلامی ایران"),
    "state:israel": ("ישראל", "מדינת ישראל"),
    "state:ukraine": ("Україна",),
    "state:north-korea": ("조선민주주의인민공화국", "북한"),
    "state:india": ("भारत",),
    "state:germany": ("Deutschland",),
    "state:turkey": ("Türkiye Cumhuriyeti",),
}


def _has_case(text: str) -> bool:
    return text.lower() != text.upper()


def test_registries_are_versioned_identity_metadata_without_scores():
    payload = registry_payload()

    assert payload["source_contract"] == SOURCE_CONTRACT
    assert payload["actor_contract"] == ACTOR_CONTRACT
    assert len(payload["sources"]) >= 10
    assert len(payload["actors"]) >= 20
    encoded = repr(payload).lower()
    for forbidden in (
        "credibility_score", "reliability_score", "truth_score", "power_score",
        "motivation",
    ):
        assert forbidden not in encoded


def test_on_disk_registries_load_under_the_shipped_validator():
    from core import registry

    registry._registry_document.cache_clear()
    registry.actor_profiles.cache_clear()
    registry.source_profiles.cache_clear()
    actors = registry.actor_profiles()
    sources = registry.source_profiles()

    # Entry and alias totals are a canary: an accidental drop or a duplicated
    # identity changes them before it changes any visible casefile.
    assert len(actors) == 39
    assert len(sources) == 27
    assert sum(len(item.aliases) for item in actors) == 261
    # Only an alias whose characters decompose gains a second spelling, so the
    # searchable surface grows by the 44 that do and by nothing else.
    assert sum(len(item.alias_forms) for item in actors) == 305


def test_registered_rss_sources_replace_dead_endpoints_and_diversify_roles():
    profiles = rss_source_profiles()
    ids = {item.source_id for item in profiles}
    roles = {item.content_role for item in profiles}

    assert "reuters_world" not in ids
    assert "ap_world" not in ids
    assert {"un_press", "iaea_topnews", "eu_council", "white_house", "russia_mfa"} <= ids
    assert {"newsroom-report", "first-party-statement"} <= roles
    assert all(item.endpoint.startswith("https://") for item in profiles)
    assert all(item.canonical_hosts for item in profiles)


def test_unconfirmed_endpoints_are_registered_but_never_collected():
    sources = {item.source_id: item for item in source_profiles()}
    collected = {item.source_id for item in rss_source_profiles()}

    assert _UNVERIFIED_SOURCE_IDS <= set(sources)
    for source_id in _UNVERIFIED_SOURCE_IDS:
        profile = sources[source_id]
        assert profile.collection_enabled is False
        assert profile.canonical_hosts
        assert "unverified" in profile.caveat
        assert "a human must confirm this feed" in profile.caveat
    assert not _UNVERIFIED_SOURCE_IDS & collected


def test_first_party_statements_reach_past_the_anglosphere():
    sources = source_profiles()
    first_party = {
        item.editorial_control_group for item in sources
        if item.content_role == "first-party-statement"
    }
    locales = {item.locale for item in sources}

    assert {
        "group:china-mfa", "group:iran-mfa", "group:israel-mfa", "group:india-mea",
        "group:japan-mofa", "group:kcna", "group:saudi-mofa", "group:turkey-mfa",
        "group:ukraine-mfa",
    } <= first_party
    assert locales == {
        "ar", "de", "en", "fa", "fr", "he", "ja", "ko", "tr", "uk", "zh-Hans",
    }
    assert sum(1 for item in sources if item.locale != "en") == 14


def test_every_source_states_its_caveat_and_publisher_hosts():
    for source in source_profiles():
        assert source.caveat and not contains_unsafe_controls(source.caveat)
        assert not contains_unsafe_controls(source.label)
        # A named publisher is only claimable against a canonical host list.
        assert bool(source.canonical_hosts) == bool(source.publisher_actor_id)


def test_registry_ids_and_aliases_are_unique():
    sources = source_profiles()
    actors = actor_profiles()

    assert len({item.source_id for item in sources}) == len(sources)
    assert len({item.actor_id for item in actors}) == len(actors)
    aliases = []
    for actor in actors:
        aliases.extend(
            text if case_sensitive else text.casefold()
            for text, case_sensitive in actor.aliases
        )
    assert len(set(aliases)) == len(aliases)


def test_actors_carry_the_names_their_own_scripts_use():
    actors = {item.actor_id: item for item in actor_profiles()}

    for actor_id, expected in _NATIVE_SCRIPT_ALIASES.items():
        texts = {text for text, _ in actors[actor_id].aliases}
        assert set(expected) <= texts, actor_id
    for actor in actors.values():
        if not actor.actor_id.startswith("state:"):
            continue
        non_ascii = [text for text, _ in actor.aliases if not text.isascii()]
        assert len(actor.aliases) >= 8, actor.actor_id
        assert len(non_ascii) >= 4, actor.actor_id


def test_native_script_aliases_survive_the_ascii_json_round_trip():
    payload = registry_payload()

    restored = json.loads(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    actors = {item["actor_id"]: item for item in restored["actors"]}
    for actor_id, expected in _NATIVE_SCRIPT_ALIASES.items():
        texts = {alias["text"] for alias in actors[actor_id]["aliases"]}
        assert set(expected) <= texts, actor_id


def test_alias_case_sensitivity_and_text_stay_meaningful():
    for actor in actor_profiles():
        for text, case_sensitive in actor.aliases:
            assert not contains_unsafe_controls(text)
            # A script without case cannot be disambiguated by casing, so the
            # flag would only record an intention the matcher cannot honour,
            # and one character of such a script is too ordinary to identify
            # an actor at all.
            if not _has_case(text):
                assert case_sensitive is False, text
                assert len(text) >= 2, text
    cased = {
        text: case_sensitive
        for actor in actor_profiles()
        for text, case_sensitive in actor.aliases
    }

    assert cased["США"] is True
    assert cased["РФ"] is True
    assert cased["НАТО"] is True
    assert cased["Le Monde"] is True
    assert cased["中国"] is False


def test_registry_rejects_a_native_script_alias_shared_by_two_actors(monkeypatch):
    from core import registry

    actors = {
        "contract": ACTOR_CONTRACT,
        "actors": [
            {
                "actor_id": "state:one", "canonical_name": "One", "kind": "state",
                "aliases": [{"text": "中国", "case_sensitive": False}],
            },
            {
                "actor_id": "org:two", "canonical_name": "Two", "kind": "org",
                "aliases": [{"text": "中国", "case_sensitive": True}],
            },
        ],
    }
    monkeypatch.setattr(registry, "_load_json", lambda name: actors)
    registry.actor_profiles.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="ambiguous actor alias"):
            registry.actor_profiles()
    finally:
        registry.actor_profiles.cache_clear()


def test_sources_command_does_not_initialize_database():
    with patch("cli.commands.init_db") as init:
        result = CliRunner().invoke(cli, ["sources", "--json"])

    assert result.exit_code == 0, result.output
    assert SOURCE_CONTRACT in result.output
    init.assert_not_called()


def test_registry_loader_rejects_analytical_fields(monkeypatch):
    from core import registry

    actors = {
        "contract": ACTOR_CONTRACT,
        "actors": [{
            "actor_id": "state:test",
            "canonical_name": "Test",
            "kind": "state",
            "aliases": [],
            "power_score": 10,
        }],
    }
    monkeypatch.setattr(registry, "_load_json", lambda name: actors)
    registry.actor_profiles.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="forbidden"):
            registry.actor_profiles()
    finally:
        registry.actor_profiles.cache_clear()


def test_registry_rejects_casefold_ambiguous_or_extended_aliases(monkeypatch):
    from core import registry

    actors = {
        "contract": ACTOR_CONTRACT,
        "actors": [
            {
                "actor_id": "org:one", "canonical_name": "One", "kind": "org",
                "aliases": [{"text": "UN", "case_sensitive": True}],
            },
            {
                "actor_id": "org:two", "canonical_name": "Two", "kind": "org",
                "aliases": [{"text": "un", "case_sensitive": False}],
            },
        ],
    }
    monkeypatch.setattr(registry, "_load_json", lambda name: actors)
    registry.actor_profiles.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="ambiguous actor alias"):
            registry.actor_profiles()
    finally:
        registry.actor_profiles.cache_clear()


def _actor_document(*aliases):
    return {
        "contract": ACTOR_CONTRACT,
        "actors": [
            {
                "actor_id": f"state:{index}", "canonical_name": f"A{index}",
                "kind": "state", "aliases": [alias],
            }
            for index, alias in enumerate(aliases)
        ],
    }


def _load_actors(monkeypatch, document):
    from core import registry

    monkeypatch.setattr(registry, "_load_json", lambda name: document)
    registry.actor_profiles.cache_clear()
    try:
        return registry.actor_profiles()
    finally:
        registry.actor_profiles.cache_clear()


def test_alias_expansion_adds_equivalent_spellings_and_nothing_else():
    composed = "Türkiye"
    decomposed = unicodedata.normalize("NFD", composed)

    forms = expand_alias_forms((("United States", True), (composed, False)))

    assert [(item.text, item.form, item.case_sensitive) for item in forms] == [
        ("United States", ALIAS_FORM_AS_REGISTERED, True),
        (composed, ALIAS_FORM_AS_REGISTERED, False),
        (decomposed, ALIAS_FORM_NFD, False),
    ]
    # A registry edited on a decomposing filesystem is served the mirror image,
    # so the composed spelling a publisher sends is still reachable.
    assert [item.form for item in expand_alias_forms(((decomposed, False),))] == [
        ALIAS_FORM_AS_REGISTERED, ALIAS_FORM_NFC,
    ]
    # NFKC is not applied: a ligature or full-width spelling is other
    # characters, not another serialization of the same ones, and folding an
    # alias could never have reached text serving them anyway.
    assert [item.text for item in expand_alias_forms((("ﬁrst", False),))] == ["ﬁrst"]
    assert [item.text for item in expand_alias_forms((("ＵＳ", False),))] == ["ＵＳ"]


def test_registry_rejects_two_actors_holding_one_name_in_two_spellings(monkeypatch):
    composed = {"text": "Türkiye", "case_sensitive": False}
    decomposed = {
        "text": unicodedata.normalize("NFD", "Türkiye"), "case_sensitive": False,
    }

    # The raw comparison the check replaced saw two unrelated strings here.
    assert composed["text"].casefold() != decomposed["text"].casefold()
    with pytest.raises(RuntimeError, match="ambiguous actor alias"):
        _load_actors(monkeypatch, _actor_document(composed, decomposed))


def test_case_sensitivity_cannot_excuse_a_spelling_only_difference(monkeypatch):
    composed = {"text": "Türkiye", "case_sensitive": True}
    decomposed = {
        "text": unicodedata.normalize("NFD", "Türkiye"), "case_sensitive": True,
    }
    other = {"text": "TÜRKIYE", "case_sensitive": True}

    with pytest.raises(RuntimeError, match="ambiguous actor alias"):
        _load_actors(monkeypatch, _actor_document(composed, decomposed))
    # Two case-sensitive aliases whose characters actually differ still stand.
    assert len(_load_actors(monkeypatch, _actor_document(composed, other))) == 2


def test_the_ambiguity_check_sees_every_spelling_the_matcher_will_search():
    for actor in actor_profiles():
        for form in actor.alias_forms:
            registered = {
                text for text, _ in actor.aliases
                if alias_identity_key(text) == alias_identity_key(form.text)
            }

            # Nothing compiles into a pattern the collision check did not key
            # on, so two actors cannot quietly answer to one visible name.
            assert registered, (actor.actor_id, form.text)


def test_registry_hashes_bind_the_loaded_snapshot(monkeypatch, tmp_path):
    from core import registry

    target = tmp_path / "registry"
    target.mkdir()
    original_root = registry.PROJECT_ROOT
    for name in ("actors.json", "sources.json"):
        (target / name).write_bytes((original_root / "registry" / name).read_bytes())

    monkeypatch.setattr(registry, "PROJECT_ROOT", tmp_path)
    registry._registry_document.cache_clear()
    registry.actor_profiles.cache_clear()
    registry.source_profiles.cache_clear()
    try:
        registry.source_profiles()
        expected = {
            name.removesuffix(".json"): hashlib.sha256(
                (target / name).read_bytes()
            ).hexdigest()
            for name in ("sources.json", "actors.json")
        }
        (target / "sources.json").write_text("{}")

        assert registry.registry_artifact_sha256() == expected
    finally:
        registry._registry_document.cache_clear()
        registry.actor_profiles.cache_clear()
        registry.source_profiles.cache_clear()
