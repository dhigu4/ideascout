"""Tests for `python run.py source-status yellowbrick`."""

from __future__ import annotations

import dataclasses

from ideascout import cli, db
from ideascout.config import Config
from ideascout.sources import browser as source_browser
from tests.test_cli_build_taste import make_config as _base_make_config
from tests.test_cli_collect_source import make_page, patch_browser, patch_extraction_and_screening


def make_config(tmp_path, **overrides) -> Config:
    config = _base_make_config(tmp_path)
    fields = {
        "browser_profiles_dir": tmp_path / "browser-profiles",
        "raw_storage_dir": tmp_path / "raw",
        "screen_rules_path": tmp_path / "IDEA_SCREEN_RULES.md",
    }
    fields.update(overrides)
    return dataclasses.replace(config, **fields)


def test_source_status_unknown_source(tmp_path, capsys):
    config = make_config(tmp_path)
    exit_code = cli.cmd_source_status(config, "not-a-real-source")
    assert exit_code == 2
    assert "Unknown source" in capsys.readouterr().out


def test_source_status_before_any_login_or_collection(tmp_path, capsys):
    config = make_config(tmp_path)
    exit_code = cli.cmd_source_status(config, "yellowbrick")
    assert exit_code == 0
    output = capsys.readouterr().out

    assert "Authentication profile present: NO" in output
    assert "Last successful collection: never" in output
    assert "Known documents: 0" in output
    assert "Source versions: 0" in output
    assert "Pending extraction versions: 0" in output
    assert "Extracted versions: 0" in output
    assert "Screened versions: 0" in output
    assert "Collection errors: 0" in output
    # Never exposes cookies/credentials -- nothing password/cookie-shaped
    # is printed by construction (there's no such field to print).
    assert "password" not in output.lower()
    assert "cookie" not in output.lower()


def test_source_status_reflects_login_profile_presence(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    (config.browser_profiles_dir / "yellowbrick").mkdir(parents=True)

    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.cmd_source_status(config, "yellowbrick")
    assert "Authentication profile present: YES" in buf.getvalue()


def test_source_status_reflects_collection_and_extraction_progress(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    patch_browser(monkeypatch, make_page())
    patch_extraction_and_screening(monkeypatch)

    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    capsys.readouterr()  # discard collect-source's own output

    exit_code = cli.cmd_source_status(config, "yellowbrick")
    assert exit_code == 0
    output = capsys.readouterr().out

    assert "Known documents: 2" in output
    assert "Source versions: 2" in output
    assert "Extracted versions: 2" in output
    assert "Pending extraction versions: 0" in output
    assert "Last successful collection:" in output
    assert "never" not in output


def test_source_status_distinguishes_documents_from_versions_after_a_real_change(tmp_path, monkeypatch, capsys):
    """One document that genuinely changed once must show "Known
    documents: 1" alongside "Source versions: 2" -- this is exactly the
    distinction that made "Known documents: 1 / Extracted: 2" look
    mysterious in the real production incident this fixes.
    """
    from tests.test_cli_collect_source import FEED_HTML, PITCH_ABC_HTML_EDITED

    config = make_config(tmp_path)
    patch_browser(monkeypatch, make_page())
    patch_extraction_and_screening(monkeypatch)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    capsys.readouterr()

    changed_feed_html = FEED_HTML.replace(
        "XYZ Corp: Hidden Value Play", "XYZ Corp: Hidden Value Play (UPDATED)"
    )
    changed_abc_html = PITCH_ABC_HTML_EDITED.replace(
        "XYZ Corp: Hidden Value Play", "XYZ Corp: Hidden Value Play (UPDATED)"
    )
    patch_browser(monkeypatch, make_page(feed_html=changed_feed_html, abc_html=changed_abc_html))
    patch_extraction_and_screening(monkeypatch)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    capsys.readouterr()

    cli.cmd_source_status(config, "yellowbrick")
    output = capsys.readouterr().out

    assert "Known documents: 2" in output  # still 2 distinct pitches
    assert "Source versions: 3" in output  # one of them now has 2 versions
