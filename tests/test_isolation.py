import json
import os
import socket
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

import conftest
from analysis.llm_processor import LLMResult
from core.pipeline import STREAM_PATH, ingest_leaf


# The denial never reads its arguments, so every probe below stays on loopback:
# deleting the guard has to fail these tests without emitting a request that
# leaves the machine.
def _refusal(call: Callable[[], object]) -> str:
    """Return the guard's refusal for ``call``, or how it failed to refuse."""
    try:
        call()
    except AssertionError as exc:
        return str(exc)
    except Exception as exc:
        return f"not refused: {exc!r}"
    return "not refused"


# Evaluated while this module is imported, which is before any fixture of any
# scope has run.
_IMPORT_TIME_REFUSAL = _refusal(lambda: socket.getaddrinfo("localhost", 80))


def test_pipeline_writes_only_to_isolated_stream(test_storage_root):
    stream_path = Path(STREAM_PATH)
    assert stream_path.resolve() == Path(os.environ["WW3_STREAM_PATH"]).resolve()
    assert stream_path.resolve().is_relative_to(test_storage_root.resolve())

    result = LLMResult(
        event_title="Isolated test event",
        event_category="other",
        classification="speculation",
        significance=1,
        summary="Synthetic fixture data.",
    )
    leaf = {
        "source": "test",
        "type": "test",
        "raw_text": "synthetic fixture",
    }

    with patch("core.pipeline.claude_available", return_value=True), \
         patch("core.pipeline.process_leaf", return_value=result), \
         patch("core.pipeline.save_all_branches"):
        event = ingest_leaf(leaf)

    assert event is not None
    assert stream_path.exists()
    records = [json.loads(line) for line in stream_path.read_text().splitlines()]
    assert [record["event_id"] for record in records] == [event.id]


def test_external_network_and_real_claude_are_denied(monkeypatch):
    with pytest.raises(AssertionError, match="network access is disabled"):
        socket.getaddrinfo("localhost", 443)

    from analysis.claude_cli import claude_available
    monkeypatch.setenv("WW3_DISABLE_LLM", "0")
    assert claude_available() is False


def test_disposable_storage_and_disabled_model_match_the_stated_boundary(
    test_storage_root,
):
    from core.db import database

    repository_root = Path(__file__).resolve().parent.parent
    storage_root = test_storage_root.resolve()
    db_path = Path(os.environ["DB_PATH"]).resolve()
    stream_path = Path(os.environ["WW3_STREAM_PATH"]).resolve()

    assert os.environ["WW3_DISABLE_LLM"] == "1"
    assert not storage_root.is_relative_to(repository_root)
    assert db_path.is_relative_to(storage_root)
    assert stream_path.is_relative_to(storage_root)
    assert Path(database.database).resolve() == db_path


def test_denial_is_installed_before_any_fixture_runs():
    assert _IMPORT_TIME_REFUSAL.startswith("network access is disabled")
    assert "socket.getaddrinfo" in _IMPORT_TIME_REFUSAL


def test_denial_survives_a_test_undoing_its_own_monkeypatch(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [])
    monkeypatch.undo()

    with pytest.raises(AssertionError, match="socket.getaddrinfo"):
        socket.getaddrinfo("localhost", 443)
    with pytest.raises(AssertionError, match="socket.create_connection"):
        socket.create_connection(("127.0.0.1", 9))
    assert conftest.isolation_guard_gaps() == []


def test_replaced_claude_executable_survives_monkeypatch_undo(monkeypatch):
    from analysis import claude_cli

    monkeypatch.setattr(claude_cli, "_CLAUDE", "/bin/sh")
    monkeypatch.undo()

    assert claude_cli._CLAUDE == str(conftest.TEST_CLAUDE_PATH)
    assert not Path(claude_cli._CLAUDE).exists()
    with patch.dict(os.environ, {"WW3_DISABLE_LLM": "0"}):
        assert claude_cli.claude_available() is False


@pytest.mark.parametrize("attempt", [
    lambda: socket.create_connection(("127.0.0.1", 9)),
    lambda: socket.create_server(("127.0.0.1", 0)),
    lambda: socket.getaddrinfo("localhost", 443),
    lambda: socket.gethostbyname("localhost"),
    lambda: socket.gethostbyname_ex("localhost"),
    lambda: socket.gethostbyaddr("127.0.0.1"),
    lambda: socket.getnameinfo(("127.0.0.1", 443), 0),
    lambda: socket.getfqdn("localhost"),
])
def test_name_resolution_and_dialling_helpers_are_denied(attempt):
    with pytest.raises(AssertionError, match="network access is disabled"):
        attempt()


@pytest.mark.parametrize("family", [socket.AF_INET, socket.AF_INET6])
def test_external_socket_construction_is_denied(family):
    with pytest.raises(AssertionError, match=r"socket\.socket\(family=AF_INET"):
        socket.socket(family, socket.SOCK_STREAM)
    with pytest.raises(AssertionError, match=r"socket\.socket\(family=AF_INET"):
        socket.socket(family, socket.SOCK_DGRAM)


def test_default_socket_construction_is_denied():
    # CPython resolves the -1 default family to AF_INET, so an argument-free
    # constructor is still an external socket.
    with pytest.raises(AssertionError, match=r"socket\.socket\(family=AF_INET\)"):
        socket.socket()


def test_connect_and_udp_send_are_denied_on_a_permitted_socket():
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        with pytest.raises(AssertionError, match="socket.socket.sendto"):
            sock.sendto(b"payload", "/nowhere/ww3-test.sock")
        with pytest.raises(AssertionError, match="socket.socket.connect"):
            sock.connect("/nowhere/ww3-test.sock")
        with pytest.raises(AssertionError, match="socket.socket.connect_ex"):
            sock.connect_ex("/nowhere/ww3-test.sock")


def test_local_socket_pairs_stay_available():
    # The guard's scope is external families; local IPC that cannot leave the
    # machine keeps working, including descriptors adopted from socketpair.
    left, right = socket.socketpair()
    with left, right:
        left.send(b"ping")
        assert right.recv(4) == b"ping"


def test_guard_gap_report_names_a_denial_that_was_removed(monkeypatch):
    monkeypatch.setattr(socket, "gethostbyname", lambda host: "127.0.0.1")
    monkeypatch.setattr(socket.socket, "sendto", lambda self, *args: 0)

    assert conftest.isolation_guard_gaps() == [
        "socket.gethostbyname",
        "socket.socket.sendto",
    ]
