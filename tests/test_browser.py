"""Unit tests for ideascout/sources/browser.py -- path construction only.
Never actually launches Playwright/Chrome (that would violate "no test
accesses live Yellowbrick" / no real browser in automated tests).
"""

from __future__ import annotations

from pathlib import Path

from ideascout.sources import browser


def test_profile_dir_is_a_subdirectory_of_browser_profiles_dir(tmp_path):
    profiles_root = tmp_path / "browser-profiles"
    result = browser.profile_dir("yellowbrick", profiles_root)
    assert result == profiles_root / "yellowbrick"


def test_profile_dir_always_outside_the_repo(tmp_path):
    from ideascout import config as ideascout_config

    result = browser.profile_dir("yellowbrick", tmp_path / "browser-profiles")
    assert ideascout_config.PROJECT_ROOT not in result.parents


def test_profile_dir_different_sources_get_different_directories(tmp_path):
    root = tmp_path / "browser-profiles"
    yb = browser.profile_dir("yellowbrick", root)
    other = browser.profile_dir("vic", root)
    assert yb != other
