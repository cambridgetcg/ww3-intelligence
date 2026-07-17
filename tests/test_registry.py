import hashlib
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli.commands import cli
from core.registry import (
    ACTOR_CONTRACT,
    SOURCE_CONTRACT,
    actor_profiles,
    registry_payload,
    rss_source_profiles,
    source_profiles,
)


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
