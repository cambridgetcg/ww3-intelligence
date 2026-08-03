"""Versioned source and actor identity registries with no truth scores."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from core.settings import PROJECT_ROOT


SOURCE_CONTRACT = "ww3.sources/v1"
ACTOR_CONTRACT = "ww3.actors/v1"
SOURCE_IDENTITY_CONTRACT = "ww3.source-identity/v1"
MAX_REGISTRY_BYTES = 2 * 1024 * 1024
MAX_ACTORS = 1_000
MAX_SOURCES = 500
CONTENT_ROLES = {
    "primary-measurement", "first-party-statement", "institutional-publication",
    "automated-alert", "newsroom-report", "wire", "aggregator", "archive",
}
DELIVERIES = {"rss", "geojson-api", "json-api", "search-api"}
_FORBIDDEN_FIELDS = {
    "credibility", "credibility_score", "intent", "motivation", "power_score",
    "reliability", "reliability_score", "truth", "truth_score",
}
_SENSITIVE_QUERY_PARTS = {
    "auth", "authorization", "credential", "key", "password", "secret", "token",
}
_ACTOR_FIELDS = {"actor_id", "canonical_name", "kind", "aliases"}
_SOURCE_FIELDS = {
    "source_id", "label", "publisher_actor_id", "delivery", "endpoint",
    "content_role", "locale", "editorial_control_group", "authority_scope",
    "caveat", "canonical_hosts", "domains", "collection_enabled",
}
_HOST = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)

ALIAS_FORM_AS_REGISTERED = "as-registered"
ALIAS_FORM_NFC = "nfc"
ALIAS_FORM_NFD = "nfd"
# Canonical equivalence only, so an expansion is at most three spellings of the
# same characters. NFKC is refused twice over: it folds ﬁ, ｕｓ, and ① onto
# characters the registry never declared, which is a compatibility judgement
# and not an identity; and it is one-way, so folding an alias cannot reach the
# full-width text a publisher served. Only folding the passage could, and a
# passage's offsets are the evidence a casefile quotes.
ALIAS_FORMS = (ALIAS_FORM_AS_REGISTERED, ALIAS_FORM_NFC, ALIAS_FORM_NFD)


@dataclass(frozen=True, slots=True)
class AliasForm:
    text: str
    case_sensitive: bool
    form: str


def alias_identity_key(text: str) -> str:
    """Fold case and canonical spelling into one comparable alias identity."""
    # Canonical caseless matching: a composed alias and its decomposed twin
    # render identically and expand to overlapping spellings, so anything
    # deciding whether two aliases are one identity must not see two.
    return unicodedata.normalize(
        "NFD", unicodedata.normalize("NFD", text).casefold()
    )


def expand_alias_forms(aliases: tuple[tuple[str, bool], ...]) -> tuple[AliasForm, ...]:
    """Expand each alias into the equivalent spellings it can be served in."""
    # The alias moves to meet the text, never the text to meet the alias:
    # captured content is immutable and its offsets index it as stored, so a
    # normalized copy of a passage would report spans into a record that does
    # not exist. Derived per profile rather than per passage.
    forms: list[AliasForm] = []
    for text, case_sensitive in aliases:
        spellings: dict[str, str] = {}
        for name, spelling in (
            (ALIAS_FORM_AS_REGISTERED, text),
            (ALIAS_FORM_NFC, unicodedata.normalize("NFC", text)),
            (ALIAS_FORM_NFD, unicodedata.normalize("NFD", text)),
        ):
            spellings.setdefault(spelling, name)
        forms.extend(
            AliasForm(text=spelling, case_sensitive=case_sensitive, form=name)
            for spelling, name in spellings.items()
        )
    return tuple(forms)


@dataclass(frozen=True, slots=True)
class ActorProfile:
    actor_id: str
    canonical_name: str
    kind: str
    aliases: tuple[tuple[str, bool], ...]
    alias_forms: tuple[AliasForm, ...]

    def as_dict(self) -> dict:
        # Only the registered alias is published. The expansion is a matching
        # surface, not a claim that this registry holds all three spellings.
        return {
            "actor_id": self.actor_id,
            "canonical_name": self.canonical_name,
            "kind": self.kind,
            "aliases": [
                {"text": text, "case_sensitive": case_sensitive}
                for text, case_sensitive in self.aliases
            ],
        }


@dataclass(frozen=True, slots=True)
class SourceProfile:
    source_id: str
    label: str
    publisher_actor_id: str | None
    delivery: str
    endpoint: str
    content_role: str
    locale: str
    editorial_control_group: str
    authority_scope: str | None
    caveat: str
    canonical_hosts: tuple[str, ...]
    domains: tuple[str, ...]
    collection_enabled: bool

    def as_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "label": self.label,
            "publisher_actor_id": self.publisher_actor_id,
            "delivery": self.delivery,
            "endpoint": self.endpoint,
            "content_role": self.content_role,
            "locale": self.locale,
            "editorial_control_group": self.editorial_control_group,
            "authority_scope": self.authority_scope,
            "caveat": self.caveat,
            "canonical_hosts": list(self.canonical_hosts),
            "domains": list(self.domains),
            "collection_enabled": self.collection_enabled,
        }


def _registry_path(name: str) -> Path:
    return PROJECT_ROOT / "registry" / name


def _registry_bytes(name: str) -> bytes:
    source = _registry_path(name).read_bytes()
    if len(source) > MAX_REGISTRY_BYTES:
        raise RuntimeError(f"registry file exceeds byte limit: {name}")
    return source


@lru_cache(maxsize=2)
def _registry_document(name: str) -> tuple[dict, str]:
    """Read, validate, and hash one immutable in-process registry snapshot."""
    try:
        source = _registry_bytes(name)
        value = json.loads(source)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid registry file: {name}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid registry root: {name}")
    return value, hashlib.sha256(source).hexdigest()


def _load_json(name: str) -> dict:
    return _registry_document(name)[0]


def _text(value, path: str, maximum: int = 2_000, *, optional: bool = False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise RuntimeError(f"invalid registry text: {path}")
    return value.strip()


def _check_fields(item: dict, path: str) -> None:
    if set(item) & _FORBIDDEN_FIELDS:
        raise RuntimeError(f"analytical score/intent field is forbidden in {path}")


def _exact_fields(item: dict, path: str, fields: set[str]) -> None:
    unknown = set(item) - fields
    missing = fields - set(item)
    if unknown or missing:
        raise RuntimeError(f"invalid registry fields: {path}")


def _host(value: object, path: str) -> str:
    text = _text(value, path, 253).lower()
    if not _HOST.fullmatch(text):
        raise RuntimeError(f"invalid canonical host: {path}")
    return text


def _endpoint(value: object, path: str) -> str:
    text = _text(value, path)
    parsed = urlsplit(text)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise RuntimeError(f"unsafe registry endpoint: {path}")
    for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        parts = set(key.lower().replace("-", "_").split("_"))
        if parts & _SENSITIVE_QUERY_PARTS:
            raise RuntimeError(f"credential-like query field in registry endpoint: {path}")
    return text


@lru_cache(maxsize=1)
def actor_profiles() -> tuple[ActorProfile, ...]:
    payload = _load_json("actors.json")
    if payload.get("contract") != ACTOR_CONTRACT or not isinstance(payload.get("actors"), list):
        raise RuntimeError("unsupported actor registry contract")
    _exact_fields(payload, "actor registry", {"contract", "actors"})
    if len(payload["actors"]) > MAX_ACTORS:
        raise RuntimeError("actor registry exceeds entry limit")
    output = []
    ids = set()
    aliases: list[tuple[str, str, bool]] = []
    for index, raw in enumerate(payload["actors"]):
        path = f"actors[{index}]"
        if not isinstance(raw, dict):
            raise RuntimeError(f"invalid registry object: {path}")
        _check_fields(raw, path)
        _exact_fields(raw, path, _ACTOR_FIELDS)
        actor_id = _text(raw.get("actor_id"), f"{path}.actor_id", 200)
        if actor_id in ids:
            raise RuntimeError(f"duplicate actor id: {actor_id}")
        ids.add(actor_id)
        raw_aliases = raw.get("aliases")
        if not isinstance(raw_aliases, list) or len(raw_aliases) > 50:
            raise RuntimeError(f"invalid aliases: {path}")
        parsed_aliases = []
        for alias_index, alias in enumerate(raw_aliases):
            if not isinstance(alias, dict) or not isinstance(alias.get("case_sensitive"), bool):
                raise RuntimeError(f"invalid alias: {path}.aliases[{alias_index}]")
            _exact_fields(
                alias, f"{path}.aliases[{alias_index}]", {"text", "case_sensitive"}
            )
            alias_text = _text(alias.get("text"), f"{path}.aliases[{alias_index}].text", 200)
            # Two spellings of one name expand to overlapping patterns, so a
            # collision the raw comparison could not see is still a collision:
            # the check compares identities, and the case-sensitive escape
            # needs a difference in characters rather than in spelling.
            identity = alias_identity_key(alias_text)
            spelling = unicodedata.normalize("NFD", alias_text)
            if any(
                identity == existing_identity
                and not (
                    alias["case_sensitive"]
                    and existing_case
                    and spelling != existing_spelling
                )
                for existing_identity, existing_spelling, existing_case in aliases
            ):
                raise RuntimeError(f"ambiguous actor alias: {alias_text}")
            aliases.append((identity, spelling, alias["case_sensitive"]))
            parsed_aliases.append((alias_text, alias["case_sensitive"]))
        profile_aliases = tuple(parsed_aliases)
        output.append(ActorProfile(
            actor_id=actor_id,
            canonical_name=_text(raw.get("canonical_name"), f"{path}.canonical_name", 200),
            kind=_text(raw.get("kind"), f"{path}.kind", 100),
            aliases=profile_aliases,
            alias_forms=expand_alias_forms(profile_aliases),
        ))
    return tuple(output)


@lru_cache(maxsize=1)
def source_profiles() -> tuple[SourceProfile, ...]:
    payload = _load_json("sources.json")
    if payload.get("contract") != SOURCE_CONTRACT or not isinstance(payload.get("sources"), list):
        raise RuntimeError("unsupported source registry contract")
    _exact_fields(payload, "source registry", {"contract", "sources"})
    if len(payload["sources"]) > MAX_SOURCES:
        raise RuntimeError("source registry exceeds entry limit")
    actors = {item.actor_id for item in actor_profiles()}
    output = []
    ids = set()
    for index, raw in enumerate(payload["sources"]):
        path = f"sources[{index}]"
        if not isinstance(raw, dict):
            raise RuntimeError(f"invalid registry object: {path}")
        _check_fields(raw, path)
        _exact_fields(raw, path, _SOURCE_FIELDS)
        source_id = _text(raw.get("source_id"), f"{path}.source_id", 200)
        if source_id in ids:
            raise RuntimeError(f"duplicate source id: {source_id}")
        ids.add(source_id)
        role = _text(raw.get("content_role"), f"{path}.content_role", 100)
        delivery = _text(raw.get("delivery"), f"{path}.delivery", 100)
        if role not in CONTENT_ROLES or delivery not in DELIVERIES:
            raise RuntimeError(f"unsupported registry role/delivery: {source_id}")
        publisher = raw.get("publisher_actor_id")
        if publisher is not None:
            publisher = _text(publisher, f"{path}.publisher_actor_id", 200)
            if publisher not in actors:
                raise RuntimeError(f"unknown publisher actor: {publisher}")
        domains = raw.get("domains")
        if not isinstance(domains, list) or not domains or len(domains) > 50:
            raise RuntimeError(f"invalid source domains: {source_id}")
        canonical_hosts = raw.get("canonical_hosts")
        if not isinstance(canonical_hosts, list) or len(canonical_hosts) > 50:
            raise RuntimeError(f"invalid canonical hosts: {source_id}")
        parsed_hosts = tuple(
            _host(item, f"{path}.canonical_hosts") for item in canonical_hosts
        )
        if len(set(parsed_hosts)) != len(parsed_hosts):
            raise RuntimeError(f"duplicate canonical host: {source_id}")
        enabled = raw.get("collection_enabled")
        if not isinstance(enabled, bool):
            raise RuntimeError(f"invalid collection flag: {source_id}")
        output.append(SourceProfile(
            source_id=source_id,
            label=_text(raw.get("label"), f"{path}.label", 200),
            publisher_actor_id=publisher,
            delivery=delivery,
            endpoint=_endpoint(raw.get("endpoint"), f"{path}.endpoint"),
            content_role=role,
            locale=_text(raw.get("locale"), f"{path}.locale", 50),
            editorial_control_group=_text(
                raw.get("editorial_control_group"), f"{path}.editorial_control_group", 200
            ),
            authority_scope=_text(
                raw.get("authority_scope"), f"{path}.authority_scope", optional=True
            ),
            caveat=_text(raw.get("caveat"), f"{path}.caveat"),
            canonical_hosts=parsed_hosts,
            domains=tuple(_text(item, f"{path}.domains", 100) for item in domains),
            collection_enabled=enabled,
        ))
    return tuple(output)


def get_actor_profile(actor_id: str) -> ActorProfile | None:
    return next((item for item in actor_profiles() if item.actor_id == actor_id), None)


def get_source_profile(source_id: str) -> SourceProfile | None:
    return next((item for item in source_profiles() if item.source_id == source_id), None)


def source_identity_context(
    source_id: str,
    canonical_url: str | None,
    *,
    synthetic: bool = False,
) -> dict:
    """Check registry/URL consistency without claiming capture authentication."""
    profile = get_source_profile(source_id)
    host = None
    scheme = None
    if canonical_url:
        try:
            parsed = urlsplit(canonical_url)
            host = parsed.hostname.lower() if parsed.hostname else None
            scheme = parsed.scheme.lower()
        except ValueError:
            pass

    if synthetic:
        status = "synthetic-fixture"
        consistent = False
    elif profile is None:
        status = "unregistered-source-id"
        consistent = False
    elif not profile.canonical_hosts:
        status = "canonical-host-unverifiable"
        consistent = None
    elif not host:
        status = "canonical-url-missing-or-invalid"
        consistent = False
    else:
        host_matches = any(
            host == allowed or host.endswith(f".{allowed}")
            for allowed in profile.canonical_hosts
        )
        consistent = bool(host_matches and scheme == "https")
        if consistent:
            status = "canonical-host-consistent"
        elif host_matches:
            status = "canonical-host-match-insecure-url"
        else:
            status = "canonical-host-mismatch"
    return {
        "contract": SOURCE_IDENTITY_CONTRACT,
        "status": status,
        "canonical_url_host": host,
        "host_consistent": consistent,
        "scope": (
            "Compares the claimed source ID and canonical URL host with the local "
            "registry. A match is not cryptographic publisher authentication or proof "
            "that the registered collector fetched this record."
        ),
    }


def rss_source_profiles() -> tuple[SourceProfile, ...]:
    return tuple(
        item for item in source_profiles()
        if item.collection_enabled and item.delivery == "rss"
    )


def registry_payload() -> dict:
    return {
        "source_contract": SOURCE_CONTRACT,
        "actor_contract": ACTOR_CONTRACT,
        "sources": [item.as_dict() for item in source_profiles()],
        "actors": [item.as_dict() for item in actor_profiles()],
        "scope": (
            "Identity, delivery, publisher, and authority-scope metadata only. "
            "No registry entry is a truth, credibility, reliability, power, or intent score."
        ),
    }


def registry_artifact_sha256() -> dict[str, str]:
    """Hash the exact in-process registry snapshots used by a casefile."""
    return {
        "sources": _registry_document("sources.json")[1],
        "actors": _registry_document("actors.json")[1],
    }


__all__ = [
    "ACTOR_CONTRACT", "ALIAS_FORMS", "ALIAS_FORM_AS_REGISTERED", "ALIAS_FORM_NFC",
    "ALIAS_FORM_NFD", "SOURCE_CONTRACT", "SOURCE_IDENTITY_CONTRACT", "MAX_ACTORS",
    "MAX_REGISTRY_BYTES", "MAX_SOURCES", "ActorProfile", "AliasForm", "SourceProfile",
    "actor_profiles", "alias_identity_key", "expand_alias_forms", "get_actor_profile",
    "get_source_profile", "registry_payload", "registry_artifact_sha256",
    "rss_source_profiles", "source_identity_context", "source_profiles",
]
