import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from core.observations import get_or_create_observation
from interpretation import rhetorlint
from interpretation.rhetorlint import (
    PASSAGE_TRANSFORM_VERSION,
    SCRIPT_MODE_SPACED,
    SCRIPT_MODE_UNSPACED,
    UNSPACED_BASIS_RATIO,
    Passage,
    RhetorLintConfigInvalid,
    RhetorLintInvalidOutput,
    RhetorLintOutputLimit,
    RhetorLintPackUnrecognized,
    RhetorLintRuntime,
    RhetorLintRuntimeChanged,
    RhetorLintTimeout,
    RhetorLintUnavailable,
    RulesPack,
    _apply_exclusions,
    _engine_identity,
    _rules_pack,
    _run,
    _summary_density,
    _utf16_units,
    _validate_result,
    extract_passages,
    script_profile,
)

# A passage in each of the three shapes the denominator has to survive: words
# written apart, words written together, and one of each in the same string.
ENGLISH = "Mistakes were made."
CHINESE = "有关部门表示，据报道，该事件的责任方尚未确定，错误已经发生。"
JAPANESE = "関係当局によると、報道によれば、その事件の責任者はまだ特定されていない。"
UNSPACED_GENRE = "SECURITY COUNCIL：有关部门表示错误已经发生。"


def _observation(raw_text):
    return get_or_create_observation({
        "source": "un_press",
        "source_record_id": "rhetoric-1",
        "type": "statement",
        "raw_text": raw_text,
    })[0]


def _passage(text=ENGLISH):
    encoded = text.encode("utf-8")
    return Passage(
        passage_id="sha256:" + "a" * 64,
        field="summary",
        text=text,
        truncated=False,
        utf8_bytes=len(encoded),
        input_sha256=hashlib.sha256(encoded).hexdigest(),
    )


def _pack(version="0.1.0"):
    return RulesPack(
        pack_id="@rhetorlint/rules-en",
        version=version,
        declared_rule_ids=("agency-hiding.deleted-subject", "shouting.caps"),
    )


def _runtime(tmp_path):
    root = tmp_path / "root"
    core_path = tmp_path / "core.mjs"
    rules_path = tmp_path / "rules.json"
    core_bytes = b"export function analyze() {}\n"
    rules_bytes = b"{}\n"
    core_path.write_bytes(core_bytes)
    rules_path.write_bytes(rules_bytes)
    return RhetorLintRuntime(
        root=root,
        node=tmp_path / "node",
        core_path=core_path,
        rules_path=rules_path,
        core_sha256=hashlib.sha256(core_bytes).hexdigest(),
        rules_sha256=hashlib.sha256(rules_bytes).hexdigest(),
        pack=_pack(),
    )


def _result(passage, *, start=None, actual="were made", rewrite=None):
    if start is None:
        py_start = passage.text.index(actual)
        start = _utf16_units(passage.text[:py_start])
    end = start + _utf16_units(actual)
    return {
        "rhetorlint": "0.1",
        "source": {
            "chars": _utf16_units(passage.text),
            "words": len(passage.text.split()),
            "locale": "en",
        },
        "density": {"tells": 1, "per100Words": 25.0},
        "marks": [{
            "ruleId": "agency-hiding.deleted-subject",
            "family": "agency-hiding",
            "technique": "Obfuscation",
            "actual": actual,
            "position": {
                "start": {"offset": start, "line": 1, "column": start + 1},
                "end": {"offset": end, "line": 1, "column": end + 1},
            },
            "note": "visible grammar pattern",
            "expected": ["name the actor"],
            "confidence": 0.7,
            "level": "warning",
        }],
        "strip": "display-only",
        "rewrite": rewrite,
        "engine": {
            "name": "@rhetorlint/core",
            "version": "0.1.0",
            "rules": "@rhetorlint/rules-en@0.1.0",
        },
    }


def _unspaced_result(passage, actual="据报道"):
    """Engine output for text whitespace cannot divide: one 'word' per passage."""
    # The engine's own arithmetic, reproduced exactly: a whole passage counts
    # as a single word, so one mark reads as a hundred marks per hundred words.
    value = _result(passage, actual=actual)
    value["density"] = {"tells": 1, "per100Words": 100.0}
    return value


def _genre_result(passage, caps="SECURITY COUNCIL", actual="were made"):
    """Engine output where a wire-copy caps header sits beside a real tell."""
    value = _result(passage, actual=actual)
    start = _utf16_units(passage.text[:passage.text.index(caps)])
    end = start + _utf16_units(caps)
    value["marks"].insert(0, {
        "ruleId": "shouting.caps",
        "family": "manipulative-wording",
        "technique": "Loaded Language",
        "actual": caps,
        "position": {
            "start": {"offset": start, "line": 1, "column": start + 1},
            "end": {"offset": end, "line": 1, "column": end + 1},
        },
        "note": "sustained ALL-CAPS — volume standing in for argument",
        "expected": ["(make the point in sentence case)"],
        "confidence": 0.6,
        "level": "info",
    })
    value["density"] = {"tells": 2, "per100Words": 40.0}
    return value


def _genre_observation():
    return _observation(json.dumps({
        "summary": "SECURITY COUNCIL statement: Mistakes were made."
    }))


def test_passage_extraction_is_field_specific_visible_text_and_deterministic():
    observation = _observation(json.dumps({
        "title": "  Public <b>statement</b>  ",
        "summary": "<p>Mistakes were made.</p><script>ignore me</script>",
        "content": "Mistakes were made.",
    }))

    first = extract_passages(observation)
    second = extract_passages(observation)

    assert first == second
    assert [(item.field, item.text) for item in first] == [
        ("title", "Public statement"),
        ("summary", "Mistakes were made."),
    ]
    assert all(item.transform_version == PASSAGE_TRANSFORM_VERSION for item in first)
    assert all(item.passage_id.startswith("sha256:") for item in first)


def test_script_profile_measures_what_no_whitespace_split_can_divide():
    assert script_profile(ENGLISH)["mode"] == SCRIPT_MODE_SPACED
    assert script_profile(CHINESE)["mode"] == SCRIPT_MODE_UNSPACED
    assert script_profile(JAPANESE)["mode"] == SCRIPT_MODE_UNSPACED
    assert script_profile("ประเทศไทยกล่าวว่า")["mode"] == SCRIPT_MODE_UNSPACED
    assert script_profile(UNSPACED_GENRE)["mode"] == SCRIPT_MODE_UNSPACED

    # Korean writes its words apart, so a whitespace count still describes it.
    assert script_profile("러시아는 성명을 발표했다")["mode"] == SCRIPT_MODE_SPACED
    # No letters at all: nothing here is hidden from a whitespace split.
    assert script_profile("2026-07-24 12:00 UTC")["mode"] == SCRIPT_MODE_SPACED

    quoting = script_profile("Sanctions on 中国 announced by the ministry today")
    assert quoting == {
        "mode": SCRIPT_MODE_SPACED,
        "script_characters": 40,
        "unspaced_characters": 2,
        "unspaced_ratio_threshold": UNSPACED_BASIS_RATIO,
    }


def test_english_density_is_word_based_and_passes_the_engine_figure_through():
    passage = _passage()

    value = _validate_result(_result(passage), passage)

    assert value["source"]["whitespace_words"] == 3
    assert value["source"]["script"]["mode"] == SCRIPT_MODE_SPACED
    assert value["source"]["rules_locale"] == "en"
    assert value["density"] == {
        "tells": 1,
        "basis": "words",
        "per_100_words": 25.0,
        "per_100_characters": None,
    }


def test_unspaced_passage_reports_characters_and_withholds_the_word_figure():
    passage = _passage(CHINESE)

    value = _validate_result(_unspaced_result(passage), passage)

    assert value["source"]["whitespace_words"] == 1
    assert value["source"]["script"] == {
        "mode": SCRIPT_MODE_UNSPACED,
        "script_characters": 26,
        "unspaced_characters": 26,
        "unspaced_ratio_threshold": UNSPACED_BASIS_RATIO,
    }
    assert value["density"] == {
        "tells": 1,
        "basis": "characters",
        "per_100_words": None,
        "per_100_characters": round(1 / _utf16_units(CHINESE) * 100, 1),
    }
    assert value["density"]["per_100_characters"] != 100.0


def test_no_marks_found_and_an_inapplicable_denominator_read_differently():
    english = _passage()
    chinese = _passage(CHINESE)
    unmarked = {"marks": [], "density": {"tells": 0, "per100Words": 0}}

    spaced = _validate_result({**_result(english), **unmarked}, english)
    unspaced = _validate_result({**_unspaced_result(chinese), **unmarked}, chinese)

    assert spaced["density"]["per_100_words"] == 0
    assert spaced["density"]["per_100_characters"] is None
    assert unspaced["density"]["per_100_words"] is None
    assert unspaced["density"]["per_100_characters"] == 0.0


def test_summary_density_lists_every_basis_and_averages_inside_one_only():
    english = _passage()
    chinese = _passage(CHINESE)
    spaced = _validate_result(_result(english), english)
    unspaced = _validate_result(_unspaced_result(chinese), chinese)

    single = _summary_density([spaced, spaced])

    assert single["comparable_across_passages"] is True
    assert single["words"] == {
        "passages": 2, "tells": 2, "words": 6, "per_100_words": 33.3,
    }
    # A basis no passage reached carries no figure; a basis whose passages
    # carried no marks carries 0.0.
    assert single["characters"] == {
        "passages": 0, "tells": 0, "characters": 0, "per_100_characters": None,
    }

    mixed = _summary_density([spaced, unspaced])

    assert mixed["comparable_across_passages"] is False
    assert mixed["words"] == {
        "passages": 1, "tells": 1, "words": 3, "per_100_words": 33.3,
    }
    assert mixed["characters"] == {
        "passages": 1,
        "tells": 1,
        "characters": _utf16_units(CHINESE),
        "per_100_characters": round(1 / _utf16_units(CHINESE) * 100, 1),
    }


def test_mixed_script_observation_reports_no_single_blended_density(tmp_path):
    observation = _observation(json.dumps({"title": ENGLISH, "summary": CHINESE}))
    title, summary = extract_passages(observation)
    with patch("interpretation.rhetorlint.discover_runtime",
               return_value=_runtime(tmp_path)), \
         patch("interpretation.rhetorlint._run", return_value=[
             _validate_result(_result(title), title),
             _validate_result(_unspaced_result(summary), summary),
         ]):
        value = rhetorlint.analyze_observation_rhetoric(observation)

    density = value["summary"]["density"]
    assert value["summary"]["matched_tells"] == 2
    assert value["summary"].keys() == {
        "passages", "matched_tells", "excluded_marks", "density",
    }
    assert density["comparable_across_passages"] is False
    assert density["words"]["passages"] == 1
    assert density["characters"]["passages"] == 1
    assert [item["result"]["density"]["basis"] for item in value["passages"]] == [
        "words", "characters",
    ]
    assert value["contract_version"] == "ww3-rhetorlint/0.3"
    assert "comparable only within one basis" in value["scope"]


def test_withheld_rule_recounts_a_passage_under_its_own_denominator():
    spaced = _passage("SECURITY COUNCIL statement: Mistakes were made.")
    unspaced = _passage(UNSPACED_GENRE)

    words = _apply_exclusions(
        _validate_result(_genre_result(spaced), spaced), ("shouting.caps",)
    )
    characters = _apply_exclusions(
        _validate_result(
            _genre_result(unspaced, actual="有关部门"), unspaced
        ),
        ("shouting.caps",),
    )

    assert words["excluded_marks"] == {"shouting.caps": 1}
    assert words["density"] == {
        "tells": 1,
        "basis": "words",
        "per_100_words": round(1 / 6 * 100, 1),
        "per_100_characters": None,
    }
    assert characters["excluded_marks"] == {"shouting.caps": 1}
    assert characters["density"] == {
        "tells": 1,
        "basis": "characters",
        "per_100_words": None,
        "per_100_characters": round(1 / _utf16_units(UNSPACED_GENRE) * 100, 1),
    }


def test_utf16_offsets_validate_visible_span_after_emoji():
    passage = _passage("🌞 Mistakes were made.")
    value = _validate_result(_result(passage), passage)

    assert value["marks"][0]["actual"] == "were made"
    assert value["marks"][0]["position_utf16"]["start"]["offset"] == 12

    with pytest.raises(RhetorLintInvalidOutput):
        _validate_result(_result(passage, start=passage.text.index("were made")), passage)


def test_validator_rejects_rewrite_and_incompatible_result():
    passage = _passage()
    with pytest.raises(RhetorLintInvalidOutput):
        _validate_result(_result(passage, rewrite="invented plain truth"), passage)

    incompatible = _result(passage)
    incompatible["engine"]["version"] = "0.2.0"
    with pytest.raises(RhetorLintInvalidOutput):
        _validate_result(incompatible, passage)


def test_runner_uses_exact_argv_minimal_env_and_no_shell(tmp_path):
    passage = _passage()
    runtime = _runtime(tmp_path)
    stdout = json.dumps({
        "results": [{"field": passage.field, "result": _result(passage)}]
    }).encode()
    def completed(*args, **kwargs):
        snapshot_root = Path(args[0][-1])
        assert snapshot_root != runtime.root
        assert (snapshot_root / "packages/core/index.mjs").read_bytes() == \
            runtime.core_path.read_bytes()
        assert (snapshot_root / "packages/rules-en/rules.json").read_bytes() == \
            runtime.rules_path.read_bytes()
        kwargs["stdout"].write(stdout)
        return subprocess.CompletedProcess(args[0], 0)

    with patch("interpretation.rhetorlint.subprocess.run", side_effect=completed) as run:
        results = _run(runtime, (passage,))

    assert results[0]["density"]["tells"] == 1
    kwargs = run.call_args.kwargs
    assert kwargs["shell"] is False
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["stdout"] is not subprocess.PIPE
    assert kwargs["env"].keys() == {"HOME", "LANG", "LC_ALL", "NO_COLOR"}
    assert "API_KEY" not in kwargs["env"]
    assert run.call_args.args[0][0] == str(runtime.node)
    assert Path(run.call_args.args[0][-1]).name == "runtime"


def test_runner_timeout_and_discovery_failure_are_stable(monkeypatch, tmp_path):
    passage = _passage()
    runtime = _runtime(tmp_path)
    with patch(
        "interpretation.rhetorlint.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["node"], 5),
    ):
        with pytest.raises(RhetorLintTimeout):
            _run(runtime, (passage,))

    monkeypatch.setattr(rhetorlint, "PROJECT_ROOT", tmp_path / "missing-project")
    monkeypatch.setattr(rhetorlint, "get_setting", lambda name: None)
    monkeypatch.setattr(rhetorlint.shutil, "which", lambda name: None)
    with pytest.raises(RhetorLintUnavailable):
        rhetorlint.discover_runtime()


def test_runner_checks_file_backed_output_limit_before_reading(monkeypatch, tmp_path):
    passage = _passage()
    runtime = _runtime(tmp_path)

    def oversized(*args, **kwargs):
        kwargs["stdout"].write(b"four")
        return subprocess.CompletedProcess(args[0], 0)

    monkeypatch.setattr(rhetorlint, "MAX_OUTPUT_BYTES", 3)
    with patch("interpretation.rhetorlint.subprocess.run", side_effect=oversized):
        with pytest.raises(RhetorLintOutputLimit):
            _run(runtime, (passage,))


def test_runner_rejects_artifact_change_after_discovery(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.core_path.write_text("changed after discovery\n")

    with patch("interpretation.rhetorlint.subprocess.run") as run:
        with pytest.raises(RhetorLintRuntimeChanged):
            _run(runtime, (_passage(),))

    run.assert_not_called()


def test_rhetoric_result_omits_strip_and_carries_artifact_hashes(tmp_path):
    observation = _observation(json.dumps({"summary": "Mistakes were made."}))
    passage = extract_passages(observation)[0]
    runtime = RhetorLintRuntime(
        root=tmp_path, node=tmp_path / "node", core_path=tmp_path / "core",
        rules_path=tmp_path / "rules", core_sha256="c" * 64,
        rules_sha256="d" * 64, pack=_pack(),
    )
    with patch("interpretation.rhetorlint.discover_runtime", return_value=runtime), \
         patch("interpretation.rhetorlint._run", return_value=[
             _validate_result(_result(passage), passage)
         ]):
        value = rhetorlint.analyze_observation_rhetoric(observation)

    assert value["runtime"]["core_sha256"] == "c" * 64
    assert all("strip" not in item["result"] for item in value["passages"])
    assert value["passages"][0]["passage_id"] == passage.passage_id


def test_excluded_rule_is_withheld_from_marks_and_named_with_its_reason(tmp_path):
    observation = _genre_observation()
    passage = extract_passages(observation)[0]
    runtime = _runtime(tmp_path)
    with patch("interpretation.rhetorlint.discover_runtime", return_value=runtime), \
         patch("interpretation.rhetorlint._run", return_value=[
             _validate_result(_genre_result(passage), passage)
         ]):
        value = rhetorlint.analyze_observation_rhetoric(observation)

    result = value["passages"][0]["result"]
    assert [mark["rule_id"] for mark in result["marks"]] == [
        "agency-hiding.deleted-subject"
    ]
    assert result["density"]["tells"] == 1
    assert result["excluded_marks"] == {"shouting.caps": 1}
    assert value["summary"]["matched_tells"] == 1
    assert value["summary"]["excluded_marks"] == 1
    assert value["runtime"]["excluded_rules"] == [{
        "rule_id": "shouting.caps",
        "reason": rhetorlint._EXCLUSION_REASONS["shouting.caps"],
        "declared_by_pack": True,
        "marks_withheld": 1,
    }]
    assert value["runtime"]["rules_pack"] == {
        "id": "@rhetorlint/rules-en",
        "version": "0.1.0",
        "declared_rule_ids": ["agency-hiding.deleted-subject", "shouting.caps"],
    }
    assert value["runtime"]["engine"]["rules"] == "@rhetorlint/rules-en@0.1.0"
    assert "runtime.excluded_rules" in value["scope"]


def test_rule_exclusion_is_configurable_and_can_be_turned_off(monkeypatch, tmp_path):
    monkeypatch.setattr(rhetorlint, "get_setting", lambda name: None)
    assert rhetorlint.excluded_rule_ids() == ("shouting.caps",)

    monkeypatch.setattr(
        rhetorlint, "get_setting", lambda name: " hedge.softener , shouting.caps ,"
    )
    assert rhetorlint.excluded_rule_ids() == ("hedge.softener", "shouting.caps")

    monkeypatch.setattr(rhetorlint, "get_setting", lambda name: "Shouting.CAPS")
    with pytest.raises(RhetorLintConfigInvalid) as raised:
        rhetorlint.excluded_rule_ids()
    assert raised.value.code == "rhetorlint_config_invalid"

    observation = _genre_observation()
    passage = extract_passages(observation)[0]
    monkeypatch.setattr(rhetorlint, "get_setting", lambda name: "")
    with patch("interpretation.rhetorlint.discover_runtime",
               return_value=_runtime(tmp_path)), \
         patch("interpretation.rhetorlint._run", return_value=[
             _validate_result(_genre_result(passage), passage)
         ]):
        value = rhetorlint.analyze_observation_rhetoric(observation)

    result = value["passages"][0]["result"]
    assert [mark["rule_id"] for mark in result["marks"]] == [
        "shouting.caps", "agency-hiding.deleted-subject"
    ]
    assert result["density"]["tells"] == 2
    assert result["excluded_marks"] == {}
    assert value["runtime"]["excluded_rules"] == []
    assert value["summary"]["excluded_marks"] == 0

    monkeypatch.setattr(rhetorlint, "get_setting", lambda name: "hedge.softener")
    with patch("interpretation.rhetorlint.discover_runtime",
               return_value=_runtime(tmp_path)), \
         patch("interpretation.rhetorlint._run", return_value=[
             _validate_result(_genre_result(passage), passage)
         ]):
        undeclared = rhetorlint.analyze_observation_rhetoric(observation)

    assert undeclared["runtime"]["excluded_rules"] == [{
        "rule_id": "hedge.softener",
        "reason": rhetorlint._CONFIGURED_EXCLUSION_REASON,
        "declared_by_pack": False,
        "marks_withheld": 0,
    }]
    assert undeclared["summary"]["matched_tells"] == 2


def test_excluded_rule_marks_are_validated_before_they_are_withheld():
    passage = _passage("SECURITY COUNCIL statement: Mistakes were made.")
    unanchored = _genre_result(passage)
    unanchored["marks"][0]["actual"] = "SECURITY COUNCILS"

    with pytest.raises(RhetorLintInvalidOutput):
        _validate_result(unanchored, passage)


def test_unreviewed_pack_vocabulary_fails_the_run_rather_than_dropping_a_mark():
    passage = _passage()
    for field, value in (
        ("family", "novel-family"),
        ("level", "critical"),
        ("ruleId", "Not A Rule Id"),
    ):
        payload = _result(passage)
        payload["marks"][0][field] = value
        with pytest.raises(RhetorLintPackUnrecognized) as raised:
            _validate_result(payload, passage)
        assert raised.value.code == "rhetorlint_pack_unrecognized"
        assert isinstance(raised.value, RhetorLintInvalidOutput)

    newer_pack = _result(passage)
    newer_pack["engine"]["rules"] = "@rhetorlint/rules-en@0.2.0"
    with pytest.raises(RhetorLintPackUnrecognized):
        _validate_result(newer_pack, passage)


def test_declared_pack_identity_is_read_and_an_unreviewed_pack_is_rejected(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({
        "id": "@rhetorlint/rules-en",
        "version": "0.1.2",
        "locale": "en",
        "rules": [{"ruleId": "shouting.caps"}, {"ruleId": "hedge.softener"}],
    }))

    pack = _rules_pack(path)

    assert pack.version == "0.1.2"
    assert pack.declared_rule_ids == ("hedge.softener", "shouting.caps")

    for unreviewed in (
        {"id": "@rhetorlint/rules-fr", "version": "0.1.2", "rules": []},
        {"id": "@rhetorlint/rules-en", "version": "0.2.0", "rules": []},
        {"id": "@rhetorlint/rules-en", "version": "0.1.2", "rules": [{"id": "x.y"}]},
        {"id": "@rhetorlint/rules-en", "version": "0.1.2"},
    ):
        path.write_text(json.dumps(unreviewed))
        with pytest.raises(RhetorLintPackUnrecognized):
            _rules_pack(path)

    path.write_text("not json")
    with pytest.raises(RhetorLintPackUnrecognized):
        _rules_pack(path)


def test_engine_identity_must_agree_across_passages_and_with_the_local_pack(tmp_path):
    runtime = _runtime(tmp_path)
    passage = _passage()
    result = _validate_result(_result(passage), passage)

    assert _engine_identity(runtime, [result, result]) == {
        "name": "@rhetorlint/core",
        "version": "0.1.0",
        "rules": "@rhetorlint/rules-en@0.1.0",
    }

    disagreeing = json.loads(json.dumps(result))
    disagreeing["engine"]["version"] = "0.1.1"
    with pytest.raises(RhetorLintPackUnrecognized):
        _engine_identity(runtime, [result, disagreeing])

    with pytest.raises(RhetorLintPackUnrecognized):
        _engine_identity(replace(runtime, pack=_pack("0.1.2")), [result])


def test_real_local_engine_counts_one_word_for_a_whole_chinese_passage():
    """Canary: the engine's denominator, measured rather than assumed."""
    try:
        rhetorlint.discover_runtime()
    except RhetorLintUnavailable:
        pytest.skip("no local RhetorLint checkout or node executable")
    observation = _observation(json.dumps({"summary": CHINESE}))

    value = rhetorlint.analyze_observation_rhetoric(observation)

    result = value["passages"][0]["result"]
    assert result["source"]["chars_utf16"] == 30
    assert result["source"]["whitespace_words"] == 1
    # The pack's locale, echoed by the engine; it read no language here.
    assert result["source"]["rules_locale"] == "en"
    assert result["density"]["basis"] == "characters"
    assert result["density"]["per_100_words"] is None
    assert value["summary"]["density"]["words"]["passages"] == 0


def test_real_local_engine_runs_and_withholds_the_excluded_rule(monkeypatch):
    """Canary: the only test that executes the sibling checkout for real."""
    try:
        runtime = rhetorlint.discover_runtime()
    except RhetorLintUnavailable:
        pytest.skip("no local RhetorLint checkout or node executable")
    monkeypatch.delenv("RHETORLINT_EXCLUDED_RULES", raising=False)
    excluded = rhetorlint.excluded_rule_ids()
    observation = _observation(json.dumps({
        "summary": (
            "SECURITY COUNCIL PRESS RELEASE: Mistakes were made at the "
            "ZAPORIZHZHYA NUCLEAR POWER PLANT."
        ),
    }))

    value = rhetorlint.analyze_observation_rhetoric(observation)

    result = value["passages"][0]["result"]
    marked = {mark["rule_id"] for mark in result["marks"]}
    assert value["status"] == "completed"
    assert value["runtime"]["engine"] == {
        "name": "@rhetorlint/core",
        "version": result["engine"]["version"],
        "rules": f"@rhetorlint/rules-en@{runtime.pack.version}",
    }
    assert "agency-hiding.deleted-subject" in marked
    assert not marked & set(excluded)
    assert result["density"]["tells"] == len(result["marks"])
    declared = runtime.pack.declared_rule_ids
    if "shouting.caps" in excluded and "shouting.caps" in declared:
        assert result["excluded_marks"]["shouting.caps"] >= 1
        assert value["runtime"]["excluded_rules"][0]["marks_withheld"] >= 1
