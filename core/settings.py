"""Project-local settings without exporting secrets into the process.

Environment variables take precedence.  Values from ``.env`` are read on
demand and returned only to the caller; they are never copied into
``os.environ``.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


@lru_cache(maxsize=1)
def _dotenv_settings() -> dict[str, str | None]:
    if not ENV_PATH.is_file():
        return {}
    return dict(dotenv_values(ENV_PATH))


def get_setting(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is not None:
        return value
    value = _dotenv_settings().get(name)
    return value if value is not None else default


def project_path_setting(name: str, default: str) -> str:
    value = get_setting(name, default) or default
    if value == ":memory:":
        return value
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return str(path.resolve())


def database_path() -> str:
    return project_path_setting("DB_PATH", "core/db/ww3.db")


def stream_path() -> str:
    return project_path_setting("WW3_STREAM_PATH", "data/stream/events.jsonl")
