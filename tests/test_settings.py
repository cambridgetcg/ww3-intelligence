import os
from pathlib import Path
from unittest.mock import patch

from core import settings


def test_dotenv_values_are_scoped_and_not_exported(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("SCOPED_TEST_SECRET=fixture-value\n")

    with patch.object(settings, "ENV_PATH", env_file), \
         patch.dict(os.environ, {}, clear=False):
        os.environ.pop("SCOPED_TEST_SECRET", None)
        settings._dotenv_settings.cache_clear()
        try:
            assert settings.get_setting("SCOPED_TEST_SECRET") == "fixture-value"
            assert "SCOPED_TEST_SECRET" not in os.environ
        finally:
            settings._dotenv_settings.cache_clear()


def test_environment_takes_precedence_over_dotenv(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TEST_SETTING=from-file\n")

    with patch.object(settings, "ENV_PATH", env_file), \
         patch.dict(os.environ, {"TEST_SETTING": "from-environment"}):
        settings._dotenv_settings.cache_clear()
        try:
            assert settings.get_setting("TEST_SETTING") == "from-environment"
        finally:
            settings._dotenv_settings.cache_clear()


def test_relative_project_paths_resolve_from_project_root():
    with patch.dict(os.environ, {"TEST_PATH_SETTING": "data/example.db"}):
        path = Path(settings.project_path_setting("TEST_PATH_SETTING", "unused"))

    assert path == (settings.PROJECT_ROOT / "data/example.db").resolve()
