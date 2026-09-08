"""Shared pytest fixtures for the whole test suite.

The single most important thing in this file: an autouse fixture that
makes it structurally impossible for a test to resolve to the real
production state directory (C:\\Users\\<you>\\IdeaScoutLocal on Windows),
even by accident (e.g. a future test that forgets to pass an explicit
database_path). Every existing test already passes explicit tmp_path-based
paths, so this fixture changes nothing about how they behave -- it exists
purely as a safety net for tests that don't (or won't, someday).

See CLAUDE.md for the full rule this enforces: production state under
IdeaScoutLocal must never be touched by tests or by ad-hoc developer
smoke-testing.
"""

from __future__ import annotations

import pytest

from ideascout import config as ideascout_config


def pytest_configure(config):
    # NOTE: pytest requires this hook's parameter to be named exactly
    # "config" (it's the pytest Config object, matched by name) -- hence
    # the import alias above instead of shadowing it here.
    config.addinivalue_line(
        "markers",
        "real_config_defaults: exempt this test from the production-state-patching "
        "autouse fixture, for tests that specifically verify the real default paths",
    )


@pytest.fixture(autouse=True)
def _never_touch_production_state(request, tmp_path, monkeypatch):
    if "real_config_defaults" in request.keywords:
        # This test's whole job is to check the *real* unpatched defaults
        # (e.g. "DEFAULT_STATE_DIR really does point at IdeaScoutLocal") --
        # patching them here would make that impossible to verify.
        yield
        return

    sandbox = tmp_path / "IdeaScoutLocal_TEST_SANDBOX"
    monkeypatch.setattr(ideascout_config, "DEFAULT_STATE_DIR", sandbox)
    monkeypatch.setattr(ideascout_config, "ENV_PATH", sandbox / ".env")
    monkeypatch.setattr(ideascout_config, "DEFAULT_DATABASE_PATH", sandbox / "ideas.db")
    monkeypatch.setattr(ideascout_config, "DEFAULT_LOG_PATH", sandbox / "app.log")
    yield
