"""Tests proving IdeaScout's persistence architecture is actually safe:

- production defaults resolve outside the repo (PROJECT_ROOT);
- the autouse conftest.py fixture actually redirects ordinary tests away
  from the real production state directory;
- load_config() cannot cause a test to open the real production database.

These exist because of a real incident: earlier development repeatedly
used the exact same path as the production database for manual
verification, and a "clean up my test files" habit ended up deleting real
production data. See CLAUDE.md for the resulting rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ideascout import config


@pytest.mark.real_config_defaults
def test_default_state_dir_is_outside_project_root():
    assert config.DEFAULT_STATE_DIR != config.PROJECT_ROOT
    assert config.PROJECT_ROOT not in config.DEFAULT_STATE_DIR.parents


@pytest.mark.real_config_defaults
def test_default_state_dir_matches_the_agreed_location():
    assert config.DEFAULT_STATE_DIR == Path.home() / "IdeaScoutLocal"


@pytest.mark.real_config_defaults
def test_default_database_log_and_env_paths_live_under_state_dir_not_repo():
    assert config.DEFAULT_DATABASE_PATH.parent == config.DEFAULT_STATE_DIR
    assert config.DEFAULT_LOG_PATH.parent == config.DEFAULT_STATE_DIR
    assert config.ENV_PATH.parent == config.DEFAULT_STATE_DIR

    for path in (config.DEFAULT_DATABASE_PATH, config.DEFAULT_LOG_PATH, config.ENV_PATH):
        assert config.PROJECT_ROOT not in path.parents


def test_autouse_fixture_redirects_ordinary_tests_away_from_real_state_dir(tmp_path):
    """No special marker here -- this is what every ordinary test gets.
    Proves the safety net is actually active by default, not just present
    in the source.
    """
    real_state_dir = Path.home() / "IdeaScoutLocal"
    assert config.DEFAULT_STATE_DIR != real_state_dir
    assert config.DEFAULT_STATE_DIR.is_relative_to(tmp_path)
    assert config.DEFAULT_DATABASE_PATH.is_relative_to(tmp_path)
    assert config.ENV_PATH.is_relative_to(tmp_path)


def test_load_config_cannot_resolve_to_the_real_production_database(monkeypatch):
    """With no DATABASE_PATH override in the environment, load_config()
    must fall back to the (patched-by-conftest) sandbox default -- never
    to the real repo or the real IdeaScoutLocal.
    """
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    monkeypatch.delenv("LOG_PATH", raising=False)

    resolved = config.load_config()

    real_state_dir = Path.home() / "IdeaScoutLocal"
    assert resolved.database_path != real_state_dir / "ideas.db"
    assert config.PROJECT_ROOT not in resolved.database_path.parents
    assert real_state_dir not in resolved.database_path.parents


def test_find_legacy_repo_env_never_reads_or_modifies_the_file(tmp_path, monkeypatch):
    """find_legacy_repo_env is only ever supposed to check existence for a
    warning -- it must never read, write, or otherwise touch the file's
    content.
    """
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    legacy_env = tmp_path / ".env"
    legacy_env.write_text("ANTHROPIC_API_KEY=super-secret\n", encoding="utf-8")
    original_content = legacy_env.read_text(encoding="utf-8")
    original_mtime = legacy_env.stat().st_mtime

    found = config.find_legacy_repo_env()

    assert found == legacy_env
    assert legacy_env.read_text(encoding="utf-8") == original_content
    assert legacy_env.stat().st_mtime == original_mtime


def test_find_legacy_repo_env_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    assert config.find_legacy_repo_env() is None
