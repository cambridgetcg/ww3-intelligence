"""Offline contract tests for the local Claude CLI boundary."""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from analysis import claude_cli
from core import settings


@pytest.fixture(autouse=True)
def _explicitly_allow_mocked_llm_calls():
    """The device defaults LLMs off; individual tests still never launch one."""

    with patch.dict(os.environ, {"WW3_DISABLE_LLM": "off"}):
        yield


def _completed(stdout: str, *, stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=[claude_cli._CLAUDE],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _envelope(*, result=None, structured_output=None, **extra) -> str:
    value = {"type": "result", "is_error": False, **extra}
    if result is not None:
        value["result"] = result
    if structured_output is not None:
        value["structured_output"] = structured_output
    return json.dumps(value)


def _call_with_mock(completed, **kwargs):
    with patch.object(claude_cli, "_claude_executable_exists", return_value=True), \
         patch.object(claude_cli.subprocess, "run", return_value=completed) as run:
        value = claude_cli.call_claude_json("analyse this", **kwargs)
    return value, run


def test_invocation_has_closed_agent_surface_and_minimal_environment():
    response = _completed(_envelope(structured_output={"status": "ok"}))
    seen_cwd = None

    def fake_run(command, **kwargs):
        nonlocal seen_cwd
        seen_cwd = kwargs["cwd"]
        assert Path(seen_cwd).is_dir()
        assert list(Path(seen_cwd).iterdir()) == []
        return response

    inherited = {
        "HOME": "/safe-home",
        "PATH": "/safe-bin",
        "LANG": "en_GB.UTF-8",
        "ANTHROPIC_API_KEY": "must-not-pass",
        "ANTHROPIC_AUTH_TOKEN": "must-not-pass",
        "CLAUDE_CODE_OAUTH_TOKEN": "must-not-pass",
        "AWS_SECRET_ACCESS_KEY": "must-not-pass",
        "DATABASE_PASSWORD": "must-not-pass",
        "WW3_DISABLE_LLM": "off",
        "NODE_OPTIONS": "--require=must-not-pass",
    }
    with patch.dict(os.environ, inherited, clear=True), \
         patch.object(claude_cli, "_claude_executable_exists", return_value=True), \
         patch.object(claude_cli.subprocess, "run", side_effect=fake_run) as run:
        assert claude_cli.call_claude_json("untrusted feed", model="sonnet", timeout=17) == {
            "status": "ok"
        }

    assert seen_cwd is not None and not Path(seen_cwd).exists()
    run.assert_called_once()
    command = run.call_args.args[0]
    assert command == [
        claude_cli._CLAUDE,
        "--print",
        "--model",
        "sonnet",
        "--output-format",
        "json",
        "--json-schema",
        '{"type":"object"}',
        "--no-session-persistence",
        "--setting-sources",
        "",
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--disable-slash-commands",
        "--no-chrome",
        "--permission-mode",
        "dontAsk",
        "--system-prompt",
        claude_cli._SYSTEM_PROMPT,
    ]
    assert "untrusted feed" not in command
    assert "untrusted" in claude_cli._SYSTEM_PROMPT.lower()

    call = run.call_args.kwargs
    assert call["input"] == "untrusted feed"
    assert call["timeout"] == 17
    assert call["env"] == {
        "HOME": "/safe-home",
        "PATH": "/safe-bin",
        "LANG": "en_GB.UTF-8",
    }
    assert call["capture_output"] is True
    assert call["check"] is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "", "unexpected"])
def test_disable_llm_is_fail_closed(value):
    with patch.dict(os.environ, {"WW3_DISABLE_LLM": value}, clear=True), \
         patch.object(claude_cli.subprocess, "run") as run:
        assert claude_cli.claude_available() is False
        with pytest.raises(RuntimeError, match="WW3_DISABLE_LLM"):
            claude_cli.call_claude_json("do not run")
    run.assert_not_called()


def test_llm_is_disabled_by_default_and_honors_scoped_dotenv(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("WW3_DISABLE_LLM=1\n")

    with patch.dict(os.environ, {}, clear=True), \
         patch.object(settings, "ENV_PATH", env_file), \
         patch.object(claude_cli.subprocess, "run") as run:
        settings._dotenv_settings.cache_clear()
        try:
            assert claude_cli.claude_available() is False
            with pytest.raises(RuntimeError, match="WW3_DISABLE_LLM"):
                claude_cli.call_claude_json("do not run")
        finally:
            settings._dotenv_settings.cache_clear()
    run.assert_not_called()

    with patch.dict(os.environ, {}, clear=True), \
         patch.object(settings, "ENV_PATH", tmp_path / "missing.env"), \
         patch.object(claude_cli.subprocess, "run") as run:
        settings._dotenv_settings.cache_clear()
        try:
            assert claude_cli.claude_available() is False
        finally:
            settings._dotenv_settings.cache_clear()
    run.assert_not_called()


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off"])
def test_explicit_false_disable_values_allow_policy_check(value):
    with patch.dict(os.environ, {"WW3_DISABLE_LLM": value}, clear=True), \
         patch.object(claude_cli, "_claude_executable_exists", return_value=True):
        assert claude_cli.claude_available() is True


def test_parses_schema_structured_output():
    value, _ = _call_with_mock(
        _completed(_envelope(structured_output={"event": "ceasefire", "p": 0.6}))
    )
    assert value == {"event": "ceasefire", "p": 0.6}


def test_parses_json_result_and_optional_markdown_fence():
    result = "```json\n{\"horizons\": [{\"horizon\": \"1h\"}]}\n```"
    value, _ = _call_with_mock(_completed(_envelope(result=result)))
    assert value == {"horizons": [{"horizon": "1h"}]}


def test_accepts_direct_object_for_backward_compatibility():
    value, _ = _call_with_mock(_completed('{"classification":"reported"}'))
    assert value == {"classification": "reported"}


@pytest.mark.parametrize(
    "stdout",
    [
        "not json",
        _envelope(result="[]"),
        json.dumps({"type": "result", "is_error": False}),
    ],
)
def test_rejects_malformed_or_non_object_results(stdout):
    with patch.object(claude_cli, "_claude_executable_exists", return_value=True), \
         patch.object(claude_cli.subprocess, "run", return_value=_completed(stdout)):
        with pytest.raises((json.JSONDecodeError, ValueError)):
            claude_cli.call_claude_json("analyse")


def test_invalid_json_exception_does_not_retain_model_output():
    model_output = "not json but private source text"
    with patch.object(claude_cli, "_claude_executable_exists", return_value=True), \
         patch.object(
             claude_cli.subprocess,
             "run",
             return_value=_completed(model_output),
         ):
        with pytest.raises(json.JSONDecodeError) as error:
            claude_cli.call_claude_json("analyse")
    assert error.value.doc == ""
    assert model_output not in str(error.value)


def test_rejects_cli_error_envelope_without_repeating_model_output():
    stdout = json.dumps(
        {"type": "result", "is_error": True, "result": "sensitive source text"}
    )
    with patch.object(claude_cli, "_claude_executable_exists", return_value=True), \
         patch.object(claude_cli.subprocess, "run", return_value=_completed(stdout)):
        with pytest.raises(RuntimeError) as error:
            claude_cli.call_claude_json("analyse")
    assert "sensitive source text" not in str(error.value)


def test_nonzero_exit_classifies_but_never_copies_stderr():
    diagnostic = "Authentication failed: secret-token-value"
    with patch.object(claude_cli, "_claude_executable_exists", return_value=True), \
         patch.object(
             claude_cli.subprocess,
             "run",
             return_value=_completed("", stderr=diagnostic, returncode=2),
         ):
        with pytest.raises(RuntimeError) as error:
            claude_cli.call_claude_json("analyse")
    message = str(error.value)
    assert "exit 2" in message
    assert "authentication unavailable" in message
    assert "secret-token-value" not in message


def test_timeout_is_bounded_and_does_not_include_prompt():
    prompt = "private source payload"
    timeout = subprocess.TimeoutExpired(cmd=[claude_cli._CLAUDE], timeout=3)
    with patch.object(claude_cli, "_claude_executable_exists", return_value=True), \
         patch.object(claude_cli.subprocess, "run", side_effect=timeout):
        with pytest.raises(RuntimeError) as error:
            claude_cli.call_claude_json(prompt, timeout=3)
    assert "timed out after 3 seconds" in str(error.value)
    assert prompt not in str(error.value)


def test_prompt_and_output_sizes_are_bounded():
    with patch.object(claude_cli, "MAX_PROMPT_BYTES", 3), \
         patch.object(claude_cli.subprocess, "run") as run:
        with pytest.raises(ValueError, match="prompt exceeds"):
            claude_cli.call_claude_json("four")
    run.assert_not_called()

    with patch.object(claude_cli, "MAX_STDOUT_BYTES", 3), \
         patch.object(claude_cli, "_claude_executable_exists", return_value=True), \
         patch.object(
             claude_cli.subprocess,
             "run",
             return_value=_completed('{"too":"large"}'),
         ):
        with pytest.raises(RuntimeError, match="output exceeded"):
            claude_cli.call_claude_json("ok")

    with patch.object(claude_cli, "MAX_STDERR_BYTES", 3), \
         patch.object(claude_cli, "_claude_executable_exists", return_value=True), \
         patch.object(
             claude_cli.subprocess,
             "run",
             return_value=_completed("{}", stderr="four"),
         ):
        with pytest.raises(RuntimeError, match="diagnostics exceeded"):
            claude_cli.call_claude_json("ok")


def test_model_and_timeout_are_validated_before_launch():
    with patch.object(claude_cli, "_claude_executable_exists", return_value=True), \
         patch.object(claude_cli.subprocess, "run") as run:
        with pytest.raises(ValueError, match="model"):
            claude_cli.call_claude_json("x", model="--dangerously-skip-permissions")
        with pytest.raises(ValueError, match="timeout"):
            claude_cli.call_claude_json("x", timeout=0)
    run.assert_not_called()
