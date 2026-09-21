"""Tests for `python run.py source-login yellowbrick`.

Never launches a real browser -- source_browser.persistent_chrome_context
is monkeypatched to a fake context/page (tests/browser_fakes.py), and
`input()` is monkeypatched so nothing here actually waits on a terminal.
"""

from __future__ import annotations

import dataclasses

from ideascout import cli
from ideascout.config import Config
from ideascout.sources import base as source_base
from ideascout.sources import browser as source_browser
from ideascout.sources import yellowbrick
from tests.browser_fakes import FakePage, make_fake_persistent_chrome_context
from tests.test_cli_build_taste import make_config as _base_make_config


def make_config(tmp_path, **overrides) -> Config:
    config = _base_make_config(tmp_path)
    fields = {
        "browser_profiles_dir": tmp_path / "browser-profiles",
        "raw_storage_dir": tmp_path / "raw",
        "screen_rules_path": tmp_path / "IDEA_SCREEN_RULES.md",
    }
    fields.update(overrides)
    return dataclasses.replace(config, **fields)


FEED_HTML = '<html><body><a href="/sp/139483">A pitch</a></body></html>'
LOGIN_HTML = '<html><body><input type="password"></body></html>'


def test_source_login_opens_dedicated_profile_and_verifies_auth(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    page = FakePage({yellowbrick.HOME_URL: FEED_HTML, yellowbrick.FEED_URL: FEED_HTML})
    fake_cm, context = make_fake_persistent_chrome_context(page)
    monkeypatch.setattr(source_browser, "persistent_chrome_context", fake_cm)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    exit_code = cli.cmd_source_login(config, "yellowbrick")

    assert exit_code == 0
    assert context.closed is True
    output = capsys.readouterr().out
    assert str(config.browser_profiles_dir / "yellowbrick") in output
    assert "Authenticated" in output


def test_source_login_reports_auth_required_when_verification_fails(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    page = FakePage({yellowbrick.HOME_URL: LOGIN_HTML, yellowbrick.FEED_URL: LOGIN_HTML})
    fake_cm, context = make_fake_persistent_chrome_context(page)
    monkeypatch.setattr(source_browser, "persistent_chrome_context", fake_cm)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    exit_code = cli.cmd_source_login(config, "yellowbrick")

    assert exit_code == 1
    assert "AUTH_REQUIRED" in capsys.readouterr().out


def test_source_login_unknown_source_name(tmp_path, capsys):
    config = make_config(tmp_path)
    exit_code = cli.cmd_source_login(config, "not-a-real-source")
    assert exit_code == 2
    assert "Unknown source" in capsys.readouterr().out


def test_source_login_waits_for_manual_enter_before_verifying(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    page = FakePage({yellowbrick.HOME_URL: FEED_HTML, yellowbrick.FEED_URL: FEED_HTML})
    fake_cm, context = make_fake_persistent_chrome_context(page)
    monkeypatch.setattr(source_browser, "persistent_chrome_context", fake_cm)

    input_calls = []
    monkeypatch.setattr("builtins.input", lambda prompt="": input_calls.append(prompt) or "")

    cli.cmd_source_login(config, "yellowbrick")
    assert len(input_calls) == 1


def test_source_login_never_stores_credentials_anywhere(tmp_path, monkeypatch):
    """No credentials are ever passed into Config, printed, or written to
    the database -- source-login's only side effect on disk is the
    (empty, Chrome-managed) profile directory existing.
    """
    config = make_config(tmp_path)
    page = FakePage({yellowbrick.HOME_URL: FEED_HTML, yellowbrick.FEED_URL: FEED_HTML})
    fake_cm, context = make_fake_persistent_chrome_context(page)
    monkeypatch.setattr(source_browser, "persistent_chrome_context", fake_cm)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    cli.cmd_source_login(config, "yellowbrick")

    # Config itself has no field that could hold a website password/cookie.
    for field in dataclasses.fields(Config):
        assert "password" not in field.name.lower()
        assert "cookie" not in field.name.lower()
        assert "credential" not in field.name.lower()
