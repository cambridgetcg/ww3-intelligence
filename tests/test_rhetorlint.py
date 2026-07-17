import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from core.observations import get_or_create_observation
from interpretation import rhetorlint
from interpretation.rhetorlint import (
    PASSAGE_TRANSFORM_VERSION,
    Passage,
    RhetorLintInvalidOutput,
    RhetorLintOutputLimit,
    RhetorLintRuntime,
    RhetorLintRuntimeChanged,
    RhetorLintTimeout,
    RhetorLintUnavailable,
    _run,
    _utf16_units,
    _validate_result,
    extract_passages,
)


def _observation(raw_text):
    return get_or_create_observation({
        "source": "un_press",
        "source_record_id": "rhetoric-1",
        "type": "statement",
        "raw_text": raw_text,
    })[0]


def _passage(text="Mistakes were made."):
    encoded = text.encode("utf-8")
    return Passage(
        passage_id="sha256:" + "a" * 64,
        field="summary",
        text=text,
        truncated=False,
        utf8_bytes=len(encoded),
        input_sha256=hashlib.sha256(encoded).hexdigest(),
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
        rules_sha256="d" * 64,
    )
    with patch("interpretation.rhetorlint.discover_runtime", return_value=runtime), \
         patch("interpretation.rhetorlint._run", return_value=[
             _validate_result(_result(passage), passage)
         ]):
        value = rhetorlint.analyze_observation_rhetoric(observation)

    assert value["runtime"]["core_sha256"] == "c" * 64
    assert all("strip" not in item["result"] for item in value["passages"])
    assert value["passages"][0]["passage_id"] == passage.passage_id
