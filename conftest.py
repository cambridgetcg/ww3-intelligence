"""Session isolation: disposable storage, denied network, no real Claude."""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import pytest


# Configure isolation before any project module is imported.  core.db captures
# DB_PATH when its module is loaded, so setting these later is too late.
_TEST_SESSION = tempfile.TemporaryDirectory(prefix="ww3-pytest-")
TEST_STORAGE_ROOT = Path(_TEST_SESSION.name)
TEST_DB_PATH = TEST_STORAGE_ROOT / "ww3-test.db"
TEST_STREAM_PATH = TEST_STORAGE_ROOT / "stream" / "events.jsonl"
TEST_CLAUDE_PATH = TEST_STORAGE_ROOT / "disabled-claude-executable"

os.environ["DB_PATH"] = str(TEST_DB_PATH)
os.environ["WW3_STREAM_PATH"] = str(TEST_STREAM_PATH)
os.environ["WW3_DISABLE_LLM"] = "1"
os.environ["BRAVE_SEARCH_API_KEY"] = ""
os.environ["BRAVE_API_KEY"] = ""

DENIAL_MESSAGE = "network access is disabled in the WW3 test suite"

# This is a guard against ACCIDENT, not against an adversary.  It covers the
# shapes an adapter reaching the network takes (resolve a name, ask a helper to
# dial, dial or send from a socket of its own) inside this interpreter, and
# nothing more.  Deliberately outside its scope: child processes
# (tests/test_cli.py starts one on purpose), descriptors already open when this
# module is imported, local families such as AF_UNIX, and any path that
# bypasses the socket module.
DENIED_SOCKET_FUNCTIONS = (
    "create_connection",
    "create_server",
    "getaddrinfo",
    "gethostbyname",
    "gethostbyname_ex",
    "gethostbyaddr",
    "getnameinfo",
    "getfqdn",
)
DENIED_SOCKET_METHODS = ("connect", "connect_ex", "sendto")
DENIED_SOCKET_FAMILIES = frozenset({socket.AF_INET, socket.AF_INET6})

# Marks this module's stand-ins, so a gap report can tell an installed denial
# apart from whatever else may now hold the attribute.
_GUARD_MARK = "_ww3_denies_external_io"

# The suite owns this patcher rather than borrowing a test's ``monkeypatch``.
# A function-scoped ``monkeypatch.undo()`` is legitimate and common, and it
# would otherwise restore the real socket stack and the real Claude executable
# for the remainder of that test, silently, inside a green suite.
_GUARD = pytest.MonkeyPatch()
_REAL_SOCKET_INIT = socket.socket.__init__


def _deny(api: str) -> Callable[..., NoReturn]:
    """Build a stand-in that refuses loudly and names the attempted call."""

    def blocked(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError(f"{DENIAL_MESSAGE}: {api}")

    setattr(blocked, _GUARD_MARK, api)
    return blocked


def _blocked_socket_init(
    self,
    family: int = -1,
    type: int = -1,
    proto: int = -1,
    fileno: int | None = None,
) -> None:
    """Refuse new external-family sockets, allow local and adopted ones."""

    # CPython resolves family -1 to AF_INET for a new socket; a caller passing
    # fileno is adopting a descriptor (accept, socketpair, dup) that some other
    # permitted call already produced.
    if fileno is None:
        resolved = socket.AF_INET if family == -1 else family
        if resolved in DENIED_SOCKET_FAMILIES:
            name = socket.AddressFamily(resolved).name
            raise AssertionError(f"{DENIAL_MESSAGE}: socket.socket(family={name})")
    _REAL_SOCKET_INIT(self, family, type, proto, fileno)


setattr(_blocked_socket_init, _GUARD_MARK, "socket.socket.__init__")


def _install_isolation_guard() -> None:
    """Deny external I/O for the whole session, before any fixture can run."""

    for name in DENIED_SOCKET_FUNCTIONS:
        _GUARD.setattr(socket, name, _deny(f"socket.{name}"))
    for name in DENIED_SOCKET_METHODS:
        _GUARD.setattr(socket.socket, name, _deny(f"socket.socket.{name}"))
    _GUARD.setattr(socket.socket, "__init__", _blocked_socket_init)

    # Imported only after the socket denial is in place, so that an import-time
    # fetch anywhere under it is refused rather than performed.
    from analysis import claude_cli

    _GUARD.setattr(claude_cli, "_CLAUDE", str(TEST_CLAUDE_PATH))


def _is_guarded(value: object) -> bool:
    """Report whether an attribute is one of this module's refusals."""
    return getattr(value, _GUARD_MARK, None) is not None


def isolation_guard_gaps() -> list[str]:
    """Name every denied API that is not currently guarded."""
    from analysis import claude_cli

    gaps = [
        f"socket.{name}"
        for name in DENIED_SOCKET_FUNCTIONS
        if not _is_guarded(getattr(socket, name, None))
    ]
    gaps += [
        f"socket.socket.{name}"
        for name in DENIED_SOCKET_METHODS
        if not _is_guarded(getattr(socket.socket, name, None))
    ]
    if not _is_guarded(socket.socket.__init__):
        gaps.append("socket.socket.__init__")
    if claude_cli._CLAUDE != str(TEST_CLAUDE_PATH) or TEST_CLAUDE_PATH.exists():
        gaps.append("analysis.claude_cli._CLAUDE")
    return gaps


_install_isolation_guard()

from core.db import (  # noqa: E402
    ALL_MODELS,
    database,
    install_observation_guards,
    migrate_schema,
    remove_observation_guards,
)


def _clear_test_storage() -> None:
    """Remove files produced by a test without touching repository data."""
    for child in TEST_STORAGE_ROOT.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Restore the patched globals once no further test can run."""
    _GUARD.undo()


@pytest.fixture(scope="session", autouse=True)
def isolated_test_session():
    yield
    if not database.is_closed():
        database.close()
    _TEST_SESSION.cleanup()


@pytest.fixture
def test_storage_root() -> Path:
    return TEST_STORAGE_ROOT


@pytest.fixture(autouse=True)
def deny_external_io():
    """Fail a test whose predecessor left the session guard incomplete."""
    gaps = isolation_guard_gaps()
    assert not gaps, f"external I/O denial is no longer installed for: {gaps}"
    assert os.environ["WW3_DISABLE_LLM"] == "1"


@pytest.fixture(autouse=True)
def use_test_db(isolated_test_session):
    """Run every test against core.db.database in disposable storage."""
    if not database.is_closed():
        database.close()
    _clear_test_storage()

    assert Path(database.database).resolve() == TEST_DB_PATH.resolve()
    assert all(model._meta.database is database for model in ALL_MODELS)

    database.connect()
    database.create_tables(ALL_MODELS)
    migrate_schema()
    install_observation_guards()
    try:
        yield
    finally:
        if database.is_closed():
            database.connect()
        remove_observation_guards()
        database.drop_tables(ALL_MODELS, safe=True)
        database.close()
        _clear_test_storage()
