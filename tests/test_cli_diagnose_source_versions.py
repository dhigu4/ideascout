"""Tests for `python run.py diagnose-source-versions yellowbrick <id>` --
the READ-ONLY diagnostic added in Stage 5.4. Never writes to the DB, never
writes/deletes files, never re-fetches; everything lives under tmp_path.
"""

from __future__ import annotations

from pathlib import Path

from ideascout import cli, db
from tests.test_cli_collect_source import (
    FEED_HTML,
    PITCH_ABC_HTML,
    build_taste_v1,
    make_config,
    make_page,
    patch_browser,
    patch_extraction_and_screening,
    write_screen_rules,
)


def test_diagnose_unknown_source(tmp_path, capsys):
    config = make_config(tmp_path)
    exit_code = cli.cmd_diagnose_source_versions(config, "not-a-real-source", "1")
    assert exit_code == 2
    assert "Unknown source" in capsys.readouterr().out


def test_diagnose_no_versions_found(tmp_path, capsys):
    config = make_config(tmp_path)
    exit_code = cli.cmd_diagnose_source_versions(config, "yellowbrick", "999999")
    assert exit_code == 0
    assert "No stored versions found" in capsys.readouterr().out


def test_diagnose_single_version_reports_hashes_and_path(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    patch_browser(monkeypatch, make_page())
    patch_extraction_and_screening(monkeypatch)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    capsys.readouterr()

    exit_code = cli.cmd_diagnose_source_versions(config, "yellowbrick", "139483")
    assert exit_code == 0
    output = capsys.readouterr().out

    assert "1 version(s) stored" in output
    assert "content_hash:" in output
    assert "raw_capture_hash:" in output
    assert "raw_html_path:" in output
    assert "IDENTICAL" in output  # trivially true for a single version


def test_diagnose_reports_identical_when_versions_are_substantively_the_same(tmp_path, monkeypatch, capsys):
    """Directly inserts two versions with different raw HTML but the same
    substantive text (simulating the real production incident's two
    duplicate rows), and confirms the diagnostic correctly recomputes
    canonical text fresh from each raw file rather than trusting a
    possibly-stale content_hash.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)

    html_v1 = PITCH_ABC_HTML
    html_v2 = PITCH_ABC_HTML.replace("Sep 1", "Sep 1 ") + "<script>window.session='noise';</script>"

    raw_dir = tmp_path / "raw_manual"
    raw_dir.mkdir()
    path_v1 = raw_dir / "v1.html"
    path_v2 = raw_dir / "v2.html"
    path_v1.write_text(html_v1, encoding="utf-8")
    path_v2.write_text(html_v2, encoding="utf-8")

    for i, path in enumerate((path_v1, path_v2)):
        db.insert_collected_source(
            conn, source_name="yellowbrick", external_id="139483", canonical_url="https://x/sp/139483",
            discovered_at="2026-01-01T00:00:00+00:00", discovery_title="X", source_date=None,
            source_title="X", author=None, ticker=None, company=None, source_type="stock_pitch",
            content_hash=f"hash{i}", raw_capture_hash=f"rawhash{i}", raw_html_path=str(path),
            metadata_json="{}", created_at="2026-01-01T00:00:00+00:00",
        )
    conn.close()

    exit_code = cli.cmd_diagnose_source_versions(config, "yellowbrick", "139483")
    assert exit_code == 0
    output = capsys.readouterr().out

    assert "2 version(s) stored" in output
    assert "IDENTICAL" in output


def test_diagnose_reports_differs_when_versions_are_substantively_different(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)

    patch_browser(monkeypatch, make_page())
    patch_extraction_and_screening(monkeypatch)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    capsys.readouterr()

    changed_feed_html = FEED_HTML.replace(
        "XYZ Corp: Hidden Value Play", "XYZ Corp: Hidden Value Play (UPDATED)"
    )
    changed_abc_html = PITCH_ABC_HTML.replace(
        "5x normalized earnings", "2x normalized earnings after a guidance cut"
    )
    patch_browser(monkeypatch, make_page(feed_html=changed_feed_html, abc_html=changed_abc_html))
    patch_extraction_and_screening(monkeypatch)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    capsys.readouterr()

    exit_code = cli.cmd_diagnose_source_versions(config, "yellowbrick", "139483")
    assert exit_code == 0
    output = capsys.readouterr().out

    assert "2 version(s) stored" in output
    assert "DIFFERS" in output


def test_diagnose_reports_char_counts_completeness_and_text_excerpts(tmp_path, monkeypatch, capsys):
    """Stage 5.7 extension: Brad's read-only diagnostic for one stored
    Yellowbrick source must also show raw HTML/canonical char counts,
    collection_status, and the first/last ~1000 chars of canonical text --
    everything section 7 of the full-capture fix asked for, without
    running a real browser or touching production state.
    """
    config = make_config(tmp_path)
    patch_browser(monkeypatch, make_page())
    patch_extraction_and_screening(monkeypatch)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    capsys.readouterr()

    conn = db.connect(config.database_path)
    row = db.get_latest_collected_source(conn, "yellowbrick", "139483")
    conn.close()
    raw_html = Path(row["raw_html_path"]).read_text(encoding="utf-8")
    from ideascout.sources import yellowbrick
    canonical_text = yellowbrick.build_canonical_source_text(raw_html)

    exit_code = cli.cmd_diagnose_source_versions(config, "yellowbrick", "139483")
    assert exit_code == 0
    output = capsys.readouterr().out

    assert "collection_status: COLLECTED" in output
    assert f"raw HTML chars:    {len(raw_html)}" in output
    assert f"canonical chars:   {len(canonical_text)}" in output
    assert repr(canonical_text[:1000]) in output
    assert repr(canonical_text[-1000:]) in output


def test_diagnose_makes_no_writes_of_any_kind(tmp_path, monkeypatch):
    """Purely read-only: no DB changes, no file changes."""
    config = make_config(tmp_path)
    patch_browser(monkeypatch, make_page())
    patch_extraction_and_screening(monkeypatch)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)

    conn = db.connect(config.database_path)
    row = db.get_latest_collected_source(conn, "yellowbrick", "139483")
    conn.close()
    raw_path = Path(row["raw_html_path"])
    original_raw_content = raw_path.read_text(encoding="utf-8")
    original_mtime = raw_path.stat().st_mtime
    original_db_mtime = config.database_path.stat().st_mtime

    cli.cmd_diagnose_source_versions(config, "yellowbrick", "139483")

    assert raw_path.read_text(encoding="utf-8") == original_raw_content
    assert raw_path.stat().st_mtime == original_mtime
    assert config.database_path.stat().st_mtime == original_db_mtime
