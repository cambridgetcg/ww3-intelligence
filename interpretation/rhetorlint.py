"""Strict, bounded bridge to the local RhetorLint 0.1 reference engine.

RhetorLint marks visible language patterns.  This module deliberately omits
its de-spun ``strip`` from WW3 output because removing qualifiers such as
``reportedly`` can destroy evidential meaning.  Nothing here reads a person,
rates truth, infers intent, or enters the model-derived Event pipeline.

The engine is general-purpose and this bridge is the only layer that knows
the corpus: wire slugs, alert strings, and institutional headers.  A rule
whose tell is a typographic convention of that corpus is withheld here by
rule id, never by rewriting the engine's note.  Every withheld rule stays in
the output with its reason and its withheld-mark count, because a silent
filter would let a reader read "this rule was not applied" as "this rule
found nothing" — a worse claim than the false mark it removes.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from core.db import Observation
from core.registry import source_identity_context
from core.settings import PROJECT_ROOT, get_setting


# 0.2 withholds excluded rules from the marks, and therefore from the tell
# counts.  The same passage answers with a smaller number than 0.1 gave it, so
# a reader comparing two runs needs the contract to say which one it is.
CONTRACT_VERSION = "ww3-rhetorlint/0.2"
PASSAGE_TRANSFORM_VERSION = "visible-text-v1"
MAX_PASSAGES = 8
MAX_PASSAGE_BYTES = 16 * 1024
MAX_TOTAL_INPUT_BYTES = 64 * 1024
MAX_MARKS = 4_096
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_PACK_RULES = 512
RUN_TIMEOUT_SECONDS = 5

# `shouting.caps` (rules-en 0.1.2) marks any run of ALL-CAPS words as "volume
# standing in for argument".  In this corpus that pattern is `SECURITY
# COUNCIL`, `FACT SHEET`, a GDACS alert string, or a wire slug, so the mark
# presents house typography as a manipulation tell.  `RHETORLINT_EXCLUDED_RULES`
# is a comma-separated override; set it empty to apply every packed rule.
DEFAULT_EXCLUDED_RULES = ("shouting.caps",)
_EXCLUSION_REASONS = {
    "shouting.caps": (
        "Withheld for this corpus: wire and institutional copy set headers, "
        "slugs, and alert strings in ALL-CAPS as typographic convention, so "
        "the rule's manipulation note does not describe the visible language."
    ),
}
_CONFIGURED_EXCLUSION_REASON = (
    "Withheld by local RHETORLINT_EXCLUDED_RULES configuration; this bridge "
    "records no reviewed reason for it."
)

FAMILIES = {
    "manipulative-wording", "simplification", "distraction",
    "attack-on-reputation", "justification", "call", "agency-hiding",
}
LEVELS = {"info", "note", "warning"}
_RULE_ID = re.compile(r"^[a-z0-9-]+\.[a-z0-9-]+$")
_COMPATIBLE_VERSION = re.compile(r"^0\.1(?:\.\d+)?$")
_PACK_ID = "@rhetorlint/rules-en"
_COMPATIBLE_PACK_VERSION = re.compile(r"^0\.1\.\d+$")
_RAW_FIELDS = (
    ("title", "headline"),
    ("summary", "description"),
    ("statement",),
    ("text",),
    ("content",),
    ("transcript",),
)


class RhetorLintError(RuntimeError):
    """Stable, non-source-bearing bridge failure."""

    code = "rhetorlint_error"

    def __init__(self, message: str | None = None):
        super().__init__(message or self.code)


class RhetorLintUnavailable(RhetorLintError):
    code = "rhetorlint_unavailable"


class RhetorLintTimeout(RhetorLintError):
    code = "rhetorlint_timeout"


class RhetorLintOutputLimit(RhetorLintError):
    code = "rhetorlint_output_limit"


class RhetorLintRuntimeChanged(RhetorLintError):
    code = "rhetorlint_runtime_changed"


class RhetorLintInvalidOutput(RhetorLintError):
    code = "rhetorlint_invalid_output"


# A pack that adds a family, level, rule-id shape, or version this bridge has
# not reviewed fails the whole run rather than losing the unknown mark: an
# annotation the bridge cannot describe must not reach a casefile, and must
# not look like a clean zero-match run either.  It stays a subclass so callers
# that already handle invalid output keep working with a narrower code.
class RhetorLintPackUnrecognized(RhetorLintInvalidOutput):
    code = "rhetorlint_pack_unrecognized"


class RhetorLintConfigInvalid(RhetorLintError):
    code = "rhetorlint_config_invalid"


class NoAnalyzableText(RhetorLintError):
    code = "no_analyzable_text"


@dataclass(frozen=True, slots=True)
class RulesPack:
    pack_id: str
    version: str
    declared_rule_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RhetorLintRuntime:
    root: Path
    node: Path
    core_path: Path
    rules_path: Path
    core_sha256: str
    rules_sha256: str
    pack: RulesPack


@dataclass(frozen=True, slots=True)
class Passage:
    passage_id: str
    field: str
    text: str
    truncated: bool
    utf8_bytes: int
    input_sha256: str
    transform_version: str = PASSAGE_TRANSFORM_VERSION


class _VisibleTextParser(HTMLParser):
    _BREAKS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self.hidden_depth += 1
        elif tag in self._BREAKS and self.hidden_depth == 0:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self.hidden_depth = max(0, self.hidden_depth - 1)
        elif tag in self._BREAKS and self.hidden_depth == 0:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.hidden_depth == 0:
            self.parts.append(data)


def _visible_text(value: str) -> str:
    parser = _VisibleTextParser()
    try:
        parser.feed(value)
        parser.close()
    except (ValueError, RecursionError):
        return " ".join(value.split())
    return " ".join("".join(parser.parts).split())


def _bounded_utf8(value: str, maximum: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value, False
    return encoded[:maximum].decode("utf-8", errors="ignore"), True


def _passage(field: str, value: str, budget: int, dedupe_key: str) -> Passage | None:
    visible = _visible_text(value)
    if not visible:
        return None
    text, truncated = _bounded_utf8(visible, min(MAX_PASSAGE_BYTES, budget))
    if not text:
        return None
    encoded = text.encode("utf-8")
    input_sha256 = hashlib.sha256(encoded).hexdigest()
    passage_id = hashlib.sha256(json.dumps(
        [dedupe_key, field, PASSAGE_TRANSFORM_VERSION, input_sha256],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return Passage(
        passage_id=f"sha256:{passage_id}",
        field=field,
        text=text,
        truncated=truncated or len(encoded) < len(visible.encode("utf-8")),
        utf8_bytes=len(encoded),
        input_sha256=input_sha256,
    )


def extract_passages(observation: Observation) -> tuple[Passage, ...]:
    """Extract bounded visible-text fields with explicit transform provenance."""
    try:
        raw = json.loads(observation.raw_text)
    except (TypeError, ValueError, RecursionError):
        raw = None

    candidates: list[tuple[str, str]] = []
    if isinstance(raw, dict):
        for aliases in _RAW_FIELDS:
            for field in aliases:
                value = raw.get(field)
                if isinstance(value, str) and value.strip():
                    candidates.append((field, value))
                    break
    elif isinstance(observation.raw_text, str) and observation.raw_text.strip():
        candidates.append(("raw_text", observation.raw_text))

    passages: list[Passage] = []
    seen: set[str] = set()
    remaining = MAX_TOTAL_INPUT_BYTES
    for field, value in candidates:
        if len(passages) >= MAX_PASSAGES or remaining <= 0:
            break
        item = _passage(field, value, remaining, observation.dedupe_key)
        if item is None or item.input_sha256 in seen:
            continue
        passages.append(item)
        seen.add(item.input_sha256)
        remaining -= item.utf8_bytes
    return tuple(passages)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def excluded_rule_ids() -> tuple[str, ...]:
    """Return the rule ids withheld from marks, in stable order."""
    configured = get_setting("RHETORLINT_EXCLUDED_RULES")
    if configured is None:
        return DEFAULT_EXCLUDED_RULES
    rule_ids = {item.strip() for item in configured.split(",")} - {""}
    if any(not _RULE_ID.fullmatch(item) for item in rule_ids):
        raise RhetorLintConfigInvalid()
    return tuple(sorted(rule_ids))


def _rules_pack(path: Path) -> RulesPack:
    """Read the local rule pack's declared identity and rule ids."""
    try:
        declared = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        raise RhetorLintPackUnrecognized() from None
    if not isinstance(declared, dict):
        raise RhetorLintPackUnrecognized()
    version = declared.get("version")
    rules = declared.get("rules")
    if (
        declared.get("id") != _PACK_ID
        or not isinstance(version, str)
        or not _COMPATIBLE_PACK_VERSION.fullmatch(version)
        or not isinstance(rules, list)
        or len(rules) > MAX_PACK_RULES
    ):
        raise RhetorLintPackUnrecognized()
    rule_ids = set()
    for rule in rules:
        rule_id = rule.get("ruleId") if isinstance(rule, dict) else None
        if not isinstance(rule_id, str) or not _RULE_ID.fullmatch(rule_id):
            raise RhetorLintPackUnrecognized()
        rule_ids.add(rule_id)
    return RulesPack(
        pack_id=_PACK_ID, version=version, declared_rule_ids=tuple(sorted(rule_ids))
    )


def _setting_path(name: str) -> Path | None:
    value = get_setting(name)
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def discover_runtime() -> RhetorLintRuntime:
    root = _setting_path("RHETORLINT_ROOT")
    if root is None:
        sibling = PROJECT_ROOT.parent / "rhetorlint-spec"
        root = sibling.resolve() if sibling.is_dir() else None
    node_setting = get_setting("RHETORLINT_NODE")
    node = None
    if node_setting:
        if "/" in node_setting or os.sep in node_setting:
            node = _setting_path("RHETORLINT_NODE")
        else:
            resolved_setting = shutil.which(node_setting)
            node = Path(resolved_setting).resolve() if resolved_setting else None
    if node is None:
        resolved = shutil.which("node")
        node = Path(resolved).resolve() if resolved else None
    if root is None or node is None or not node.is_file():
        raise RhetorLintUnavailable()

    core_path = root / "packages" / "core" / "index.mjs"
    rules_path = root / "packages" / "rules-en" / "rules.json"
    if not core_path.is_file() or not rules_path.is_file():
        raise RhetorLintUnavailable()
    return RhetorLintRuntime(
        root=root,
        node=node,
        core_path=core_path,
        rules_path=rules_path,
        core_sha256=_sha256_file(core_path),
        rules_sha256=_sha256_file(rules_path),
        pack=_rules_pack(rules_path),
    )


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _utf16_slice(value: str, start: int, end: int) -> str:
    if start < 0 or end < start:
        raise RhetorLintInvalidOutput()
    encoded = value.encode("utf-16-le")
    if end * 2 > len(encoded):
        raise RhetorLintInvalidOutput()
    try:
        return encoded[start * 2:end * 2].decode("utf-16-le")
    except UnicodeDecodeError:
        raise RhetorLintInvalidOutput() from None


def _finite_number(value: Any, *, minimum: float, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RhetorLintInvalidOutput()
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise RhetorLintInvalidOutput()
    if maximum is not None and number > maximum:
        raise RhetorLintInvalidOutput()
    return number


def _bounded_string(value: Any, maximum: int = 10_000) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise RhetorLintInvalidOutput()
    return value


def _point(value: Any) -> dict[str, int]:
    if (
        not isinstance(value, dict)
        or isinstance(value.get("offset"), bool)
        or not isinstance(value.get("offset"), int)
        or value["offset"] < 0
    ):
        raise RhetorLintInvalidOutput()
    point = {"offset": value["offset"]}
    for field in ("line", "column"):
        if field in value:
            item = value[field]
            if isinstance(item, bool) or not isinstance(item, int) or item < 1:
                raise RhetorLintInvalidOutput()
            point[field] = item
    return point


def _validate_result(value: Any, passage: Passage) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("rhetorlint") != "0.1":
        raise RhetorLintInvalidOutput()
    source = value.get("source")
    density = value.get("density")
    engine = value.get("engine")
    marks = value.get("marks")
    if not all(isinstance(item, dict) for item in (source, density, engine)):
        raise RhetorLintInvalidOutput()
    if not isinstance(marks, list) or len(marks) > MAX_MARKS:
        raise RhetorLintInvalidOutput()
    if value.get("rewrite") is not None:
        raise RhetorLintInvalidOutput()
    if (
        isinstance(source.get("chars"), bool)
        or not isinstance(source.get("chars"), int)
        or source.get("chars") != _utf16_units(passage.text)
    ):
        raise RhetorLintInvalidOutput()
    if (
        isinstance(source.get("words"), bool)
        or not isinstance(source.get("words"), int)
        or source["words"] < 0
    ):
        raise RhetorLintInvalidOutput()
    locale = _bounded_string(source.get("locale"), 50)
    tells = density.get("tells")
    if isinstance(tells, bool) or not isinstance(tells, int) or tells != len(marks):
        raise RhetorLintInvalidOutput()
    per100 = _finite_number(density.get("per100Words"), minimum=0)
    if engine.get("name") != "@rhetorlint/core":
        raise RhetorLintInvalidOutput()
    engine_version = _bounded_string(engine.get("version"), 50)
    rules_version = _bounded_string(engine.get("rules"), 200)
    if not _COMPATIBLE_VERSION.fullmatch(engine_version):
        raise RhetorLintPackUnrecognized()
    if not rules_version.startswith(f"{_PACK_ID}@0.1."):
        raise RhetorLintPackUnrecognized()

    normalized_marks = []
    for mark in marks:
        if not isinstance(mark, dict):
            raise RhetorLintInvalidOutput()
        rule_id = _bounded_string(mark.get("ruleId"), 200)
        family = _bounded_string(mark.get("family"), 100)
        level = _bounded_string(mark.get("level"), 20)
        actual = _bounded_string(mark.get("actual"), MAX_PASSAGE_BYTES)
        if not _RULE_ID.fullmatch(rule_id) or family not in FAMILIES or level not in LEVELS:
            raise RhetorLintPackUnrecognized()
        position = mark.get("position")
        if not isinstance(position, dict):
            raise RhetorLintInvalidOutput()
        start = _point(position.get("start"))
        end = _point(position.get("end"))
        if _utf16_slice(passage.text, start["offset"], end["offset"]) != actual:
            raise RhetorLintInvalidOutput()
        expected = mark.get("expected", [])
        if not isinstance(expected, list) or len(expected) > 20:
            raise RhetorLintInvalidOutput()
        normalized_marks.append({
            "rule_id": rule_id,
            "family": family,
            "technique": _bounded_string(mark.get("technique", ""), 500),
            "actual": actual,
            "position_utf16": {"start": start, "end": end},
            "note": _bounded_string(mark.get("note", ""), 2_000),
            "expected": [_bounded_string(item, 1_000) for item in expected],
            "confidence": _finite_number(mark.get("confidence"), minimum=0, maximum=1),
            "level": level,
        })
    return {
        "rhetorlint": "0.1",
        "source": {"chars_utf16": source["chars"], "words": source["words"], "locale": locale},
        "density": {"tells": tells, "per_100_words": per100},
        "marks": normalized_marks,
        "engine": {
            "name": "@rhetorlint/core",
            "version": engine_version,
            "rules": rules_version,
        },
    }


def _apply_exclusions(
    result: dict[str, Any], excluded: tuple[str, ...]
) -> dict[str, Any]:
    """Withhold excluded rule ids from one validated result and count them."""
    withheld: dict[str, int] = {}
    kept = []
    for mark in result["marks"]:
        if mark["rule_id"] in excluded:
            withheld[mark["rule_id"]] = withheld.get(mark["rule_id"], 0) + 1
        else:
            kept.append(mark)
    if not withheld:
        return {**result, "excluded_marks": {}}
    # Density describes the marks this bridge reports, so a reader can still
    # count them; the withheld tally beside it restores the engine's total.
    words = result["source"]["words"]
    return {
        **result,
        "density": {
            "tells": len(kept),
            "per_100_words": round(len(kept) / words * 100, 1) if words else 0,
        },
        "marks": kept,
        "excluded_marks": dict(sorted(withheld.items())),
    }


def _engine_identity(
    runtime: RhetorLintRuntime, results: list[dict[str, Any]]
) -> dict[str, Any]:
    """Fold one engine identity from every passage of a single run."""
    # The engine echoes back the pack it was handed, so disagreement between
    # passages, or with the pack parsed here, means the marks cannot be tied
    # to the artifact hashes recorded beside them.
    identity = results[0]["engine"]
    expected_rules = f"{runtime.pack.pack_id}@{runtime.pack.version}"
    if any(item["engine"] != identity for item in results):
        raise RhetorLintPackUnrecognized()
    if identity["rules"] != expected_rules:
        raise RhetorLintPackUnrecognized()
    return dict(identity)


def _run(runtime: RhetorLintRuntime, passages: tuple[Passage, ...]) -> list[dict[str, Any]]:
    runner = Path(__file__).with_name("rhetorlint_runner.mjs")
    payload = json.dumps({
        "passages": [{"field": item.field, "text": item.text} for item in passages]
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with tempfile.TemporaryDirectory(prefix="ww3-rhetorlint-") as home:
        snapshot_root = Path(home) / "runtime"
        core_snapshot = snapshot_root / "packages" / "core" / "index.mjs"
        rules_snapshot = snapshot_root / "packages" / "rules-en" / "rules.json"
        try:
            core_bytes = runtime.core_path.read_bytes()
            rules_bytes = runtime.rules_path.read_bytes()
        except OSError:
            raise RhetorLintUnavailable() from None
        if (
            hashlib.sha256(core_bytes).hexdigest() != runtime.core_sha256
            or hashlib.sha256(rules_bytes).hexdigest() != runtime.rules_sha256
        ):
            raise RhetorLintRuntimeChanged()
        try:
            core_snapshot.parent.mkdir(parents=True)
            rules_snapshot.parent.mkdir(parents=True)
            core_snapshot.write_bytes(core_bytes)
            rules_snapshot.write_bytes(rules_bytes)
        except OSError:
            raise RhetorLintUnavailable() from None
        with tempfile.TemporaryFile() as output_file:
            try:
                completed = subprocess.run(
                    [str(runtime.node), str(runner), str(snapshot_root)],
                    input=payload,
                    stdout=output_file,
                    stderr=subprocess.DEVNULL,
                    cwd=home,
                    env={
                        "HOME": home, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                        "NO_COLOR": "1",
                    },
                    timeout=RUN_TIMEOUT_SECONDS,
                    check=False,
                    shell=False,
                )
            except subprocess.TimeoutExpired:
                raise RhetorLintTimeout() from None
            except OSError:
                raise RhetorLintUnavailable() from None
            output_file.seek(0, 2)
            if output_file.tell() > MAX_OUTPUT_BYTES:
                raise RhetorLintOutputLimit()
            output_file.seek(0)
            stdout = output_file.read(MAX_OUTPUT_BYTES + 1)
    if completed.returncode != 0:
        raise RhetorLintInvalidOutput()
    try:
        decoded = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise RhetorLintInvalidOutput() from None
    results = decoded.get("results") if isinstance(decoded, dict) else None
    if not isinstance(results, list) or len(results) != len(passages):
        raise RhetorLintInvalidOutput()
    output = []
    for expected, item in zip(passages, results):
        if not isinstance(item, dict) or item.get("field") != expected.field:
            raise RhetorLintInvalidOutput()
        output.append(_validate_result(item.get("result"), expected))
    return output


def analyze_observation_rhetoric(observation: Observation) -> dict[str, Any]:
    """Return ephemeral, field-specific RhetorLint annotations for one Observation."""
    if not isinstance(observation, Observation) or observation.id is None:
        raise TypeError("observation must be a stored Observation")
    stored = Observation.get_or_none(Observation.id == observation.id)
    if stored is None:
        raise TypeError("observation must be a stored Observation")
    observation = stored
    passages = extract_passages(observation)
    if not passages:
        raise NoAnalyzableText()
    excluded = excluded_rule_ids()
    runtime = discover_runtime()
    results = _run(runtime, passages)
    engine = _engine_identity(runtime, results)
    records = []
    total_words = 0
    total_marks = 0
    withheld_totals: dict[str, int] = {}
    for passage, result in zip(passages, results):
        result = _apply_exclusions(result, excluded)
        total_words += result["source"]["words"]
        total_marks += result["density"]["tells"]
        for rule_id, count in result["excluded_marks"].items():
            withheld_totals[rule_id] = withheld_totals.get(rule_id, 0) + count
        records.append({
            "passage_id": passage.passage_id,
            "field": passage.field,
            "transform_version": passage.transform_version,
            "truncated": passage.truncated,
            "utf8_bytes": passage.utf8_bytes,
            "input_sha256": passage.input_sha256,
            "result": result,
        })
    return {
        "status": "completed",
        "contract_version": CONTRACT_VERSION,
        "observation": {
            "id": observation.id,
            "dedupe_key": observation.dedupe_key,
            "content_sha256": observation.content_hash,
            "source": observation.source,
            "canonical_url": observation.canonical_url,
            "synthetic": bool(observation.synthetic),
            "source_identity": source_identity_context(
                observation.source,
                observation.canonical_url,
                synthetic=bool(observation.synthetic),
            ),
        },
        "runtime": {
            "core_sha256": runtime.core_sha256,
            "rules_sha256": runtime.rules_sha256,
            "engine": engine,
            # The pack declares these ids; the bridge cannot observe which of
            # them the engine evaluated, so the applied set is exactly the
            # declared set minus `excluded_rules`.
            "rules_pack": {
                "id": runtime.pack.pack_id,
                "version": runtime.pack.version,
                "declared_rule_ids": list(runtime.pack.declared_rule_ids),
            },
            "excluded_rules": [
                {
                    "rule_id": rule_id,
                    "reason": _EXCLUSION_REASONS.get(
                        rule_id, _CONFIGURED_EXCLUSION_REASON
                    ),
                    "declared_by_pack": rule_id in runtime.pack.declared_rule_ids,
                    "marks_withheld": withheld_totals.get(rule_id, 0),
                }
                for rule_id in excluded
            ],
        },
        "summary": {
            "passages": len(records),
            "words": total_words,
            "matched_tells": total_marks,
            "per_100_words": round(total_marks / total_words * 100, 1) if total_words else 0,
            "excluded_marks": sum(withheld_totals.values()),
        },
        "passages": records,
        "scope": (
            "Visible language-pattern annotations only; not intent, deception, truth, "
            "credibility, corroboration, or actor attribution. Zero matches means only "
            "that the applied English rules matched nothing; rules named in "
            "runtime.excluded_rules were withheld at this bridge and their marks are "
            "counted, not shown. RhetorLint strip is omitted to preserve evidential "
            "qualifiers."
        ),
    }


__all__ = [
    "CONTRACT_VERSION", "DEFAULT_EXCLUDED_RULES", "NoAnalyzableText", "Passage",
    "RhetorLintConfigInvalid", "RhetorLintError", "RhetorLintInvalidOutput",
    "RhetorLintOutputLimit", "RhetorLintPackUnrecognized", "RhetorLintTimeout",
    "RhetorLintRuntimeChanged", "RhetorLintUnavailable", "RulesPack",
    "analyze_observation_rhetoric", "discover_runtime", "excluded_rule_ids",
    "extract_passages",
]
