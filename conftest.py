import os
import shutil
import socket
import tempfile
from pathlib import Path

import pytest


# Configure isolation before any project module is imported.  core.db captures
# DB_PATH when its module is loaded, so setting these later is too late.
_TEST_SESSION = tempfile.TemporaryDirectory(prefix="ww3-pytest-")
TEST_STORAGE_ROOT = Path(_TEST_SESSION.name)
TEST_DB_PATH = TEST_STORAGE_ROOT / "ww3-test.db"
TEST_STREAM_PATH = TEST_STORAGE_ROOT / "stream" / "events.jsonl"

os.environ["DB_PATH"] = str(TEST_DB_PATH)
os.environ["WW3_STREAM_PATH"] = str(TEST_STREAM_PATH)
os.environ["WW3_DISABLE_LLM"] = "1"
os.environ["BRAVE_SEARCH_API_KEY"] = ""
os.environ["BRAVE_API_KEY"] = ""

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
def deny_external_io(monkeypatch):
    """Fail tests that accidentally reach a socket or a real Claude binary."""
    def blocked_network(*args, **kwargs):
        raise AssertionError("network access is disabled in the WW3 test suite")

    monkeypatch.setattr(socket, "create_connection", blocked_network)
    monkeypatch.setattr(socket, "getaddrinfo", blocked_network)
    monkeypatch.setattr(socket.socket, "connect", blocked_network)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked_network)

    from analysis import claude_cli
    monkeypatch.setattr(
        claude_cli,
        "_CLAUDE",
        str(TEST_STORAGE_ROOT / "disabled-claude-executable"),
    )


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
