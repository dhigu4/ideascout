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


@pytest.mark.real_config_defaults
def test_default_screen_rules_path_matches_the_agreed_location():
    """Stage 4's canonical IDEA_SCREEN_RULES.md lives in a separate real
    repo (InvestmentBrain), never under IdeaScoutLocal and never in this
    repo -- this is Brad's real path, so this test (like the
    DEFAULT_STATE_DIR ones above) is exempt from the autouse sandboxing
    fixture specifically to check the real, unpatched value.
    """
    assert config.DEFAULT_SCREEN_RULES_PATH == Path.home() / "Repos" / "InvestmentBrain" / "IDEA_SCREEN_RULES.md"
    assert config.PROJECT_ROOT not in config.DEFAULT_SCREEN_RULES_PATH.parents


@pytest.mark.real_config_defaults
def test_default_browser_profiles_and_raw_storage_dirs_live_under_state_dir_not_repo():
    """Stage 5's browser-profiles (Chrome's own persistent session data)
    and raw (permanently captured source documents) must both live under
    IdeaScoutLocal, never under this repo.
    """
    assert config.DEFAULT_BROWSER_PROFILES_DIR == config.DEFAULT_STATE_DIR / "browser-profiles"
    assert config.DEFAULT_RAW_STORAGE_DIR == config.DEFAULT_STATE_DIR / "raw"
    assert config.PROJECT_ROOT not in config.DEFAULT_BROWSER_PROFILES_DIR.parents
    assert config.PROJECT_ROOT not in config.DEFAULT_RAW_STORAGE_DIR.parents


def test_alerts_enabled_defaults_false():
    assert config.DEFAULT_ALERTS_ENABLED is False
    resolved = config.load_config()
    assert resolved.alerts_enabled is False


def test_autouse_fixture_redirects_ordinary_tests_away_from_real_state_dir(tmp_path):
    """No special marker here -- this is what every ordinary test gets.
    Proves the safety net is actually active by default, not just present
    in the source.
    """
    real_state_dir = Path.home() / "IdeaScoutLocal"
    real_screen_rules_path = Path.home() / "Repos" / "InvestmentBrain" / "IDEA_SCREEN_RULES.md"
    assert config.DEFAULT_STATE_DIR != real_state_dir
    assert config.DEFAULT_STATE_DIR.is_relative_to(tmp_path)
    assert config.DEFAULT_DATABASE_PATH.is_relative_to(tmp_path)
    assert config.ENV_PATH.is_relative_to(tmp_path)
    assert config.DEFAULT_SCREEN_RULES_PATH != real_screen_rules_path
    assert config.DEFAULT_SCREEN_RULES_PATH.is_relative_to(tmp_path)

    real_browser_profiles_dir = Path.home() / "IdeaScoutLocal" / "browser-profiles"
    real_raw_storage_dir = Path.home() / "IdeaScoutLocal" / "raw"
    assert config.DEFAULT_BROWSER_PROFILES_DIR != real_browser_profiles_dir
    assert config.DEFAULT_RAW_STORAGE_DIR != real_raw_storage_dir
    assert config.DEFAULT_BROWSER_PROFILES_DIR.is_relative_to(tmp_path)
    assert config.DEFAULT_RAW_STORAGE_DIR.is_relative_to(tmp_path)


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


def test_load_config_cannot_resolve_screen_rules_path_to_the_real_investment_brain_repo(monkeypatch):
    """Same guarantee as the database one above, for Stage 4's
    screen_rules_path: with no SCREEN_RULES_PATH override in the
    environment, load_config() must fall back to the (patched-by-conftest)
    sandbox default -- never to Brad's real InvestmentBrain repo.
    """
    monkeypatch.delenv("SCREEN_RULES_PATH", raising=False)

    resolved = config.load_config()

    real_screen_rules_path = Path.home() / "Repos" / "InvestmentBrain" / "IDEA_SCREEN_RULES.md"
    assert resolved.screen_rules_path != real_screen_rules_path
    assert config.PROJECT_ROOT not in resolved.screen_rules_path.parents


def test_load_config_cannot_resolve_browser_or_raw_dirs_to_real_idea_scout_local(monkeypatch):
    monkeypatch.delenv("BROWSER_PROFILES_DIR", raising=False)
    monkeypatch.delenv("RAW_STORAGE_DIR", raising=False)

    resolved = config.load_config()

    real_state_dir = Path.home() / "IdeaScoutLocal"
    assert resolved.browser_profiles_dir != real_state_dir / "browser-profiles"
    assert resolved.raw_storage_dir != real_state_dir / "raw"
    assert real_state_dir not in resolved.browser_profiles_dir.parents
    assert real_state_dir not in resolved.raw_storage_dir.parents


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
