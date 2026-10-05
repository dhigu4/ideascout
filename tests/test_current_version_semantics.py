"""Tests for the newest-source-version rule across the default display, digest
selection (preview-digest and send-digest), and the read-only narrative audit.

Rule under test: for each logical document (source_name + external_id), the
NEWEST stored source version is decided first, regardless of collection_status.
Only if that newest version is COLLECTED may its screening participate. An
older COLLECTED version never stands in for a newer INCOMPLETE_CONTENT one.
--all-versions remains unfiltered.

No LLM or network calls. Every database lives under pytest's tmp_path.
"""

from __future__ import annotations

import contextlib
import io
import json

from ideascout import cli, db, shadow
from tests.test_cli_send_digest import patch_agentmail
from tests.test_screening_narrative import (
    FULL,
    build_taste_v1,
    fail_all_llm,
    make_config,
    prediction,
    snapshot_collected,
    snapshot_screenings,
    write_rules,
)


def insert_version(conn, latest_taste, rules_sha, *, external_id, content_hash, collection_status, pred, company):
    """One stored source version plus its screening under the current taste."""
    source_id = db.insert_collected_source(
        conn, source_name="yellowbrick", external_id=external_id,
        canonical_url=f"https://www.joinyellowbrick.com/sp/{external_id}",
        discovered_at="2026-01-01T00:00:00+00:00", discovery_title=company, source_date=None,
        source_title=company, author=None, ticker=company.split()[0].upper(), company=company,
        source_type="stock_pitch", content_hash=content_hash, raw_html_path=f"/x/{content_hash}.html",
        metadata_json="{}", created_at="2026-01-01T00:00:00+00:00", collection_status=collection_status,
    )
    db.update_collected_source_extracted(
        conn, source_id=source_id, company=company, ticker=company.split()[0].upper(), source_title=company,
        source_date=None, business_summary="x", core_thesis="x", why_mispriced="x",
        future_earnings_change="x", upside_case="x", downside_or_key_risks="x", catalysts="x",
        what_must_be_true="x", evidence_of_market_misunderstanding="x", known_unknowns="x",
        extraction_model_name="fake-model", extracted_at="2026-01-01T00:00:00+00:00",
    )
    screening_id = db.insert_source_screening(
        conn, source_id=source_id, created_at="2026-01-01T00:00:00+00:00",
        taste_version=latest_taste["version_number"], taste_sha256=latest_taste["idea_taste_sha256"],
        screen_rules_path="IDEA_SCREEN_RULES.md", screen_rules_sha256=rules_sha,
        content_hash=content_hash, model_name="fake-model", overall_prediction=pred.overall_prediction,
        mispricing=pred.mispricing, variant_perception=pred.variant_perception, upside=pred.upside,
        business_quality=pred.business_quality, downside=pred.downside,
        key_reasons_json=json.dumps(pred.key_reasons),
        key_concerns_json=json.dumps(pred.key_concerns),
        critical_questions_json=json.dumps(pred.critical_questions), confidence=pred.confidence,
    )
    return source_id, screening_id


def _digest_companies(conn, taste_version):
    selected, _, _ = cli.select_digest_candidates_detailed(conn, taste_version)
    return {row["company"] for row in selected}


def _display_companies(config, *, all_versions=False):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.cmd_show_source_screenings(config, limit=50, all_versions=all_versions)
    return buf.getvalue()


def test_newest_collected_version_screen_is_current_and_visible(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_version(conn, latest, rules_sha, external_id="1", content_hash="v1", collection_status="COLLECTED", pred=prediction("WATCH", **FULL), company="Visible Co")
    conn.close()

    assert "Visible Co" in _display_companies(config)
    conn = db.connect(config.database_path)
    assert "Visible Co" in _digest_companies(conn, latest["version_number"])
    conn.close()


def test_older_collected_newer_incomplete_is_suppressed_from_display(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_version(conn, latest, rules_sha, external_id="2", content_hash="old-ok", collection_status="COLLECTED", pred=prediction("WATCH", **FULL), company="Stale Co")
    insert_version(conn, latest, rules_sha, external_id="2", content_hash="new-bad", collection_status="INCOMPLETE_CONTENT", pred=prediction("INSUFFICIENT_INFORMATION", downside="Unknown"), company="Stale Co")
    conn.close()

    assert "Stale Co" not in _display_companies(config)


def test_older_collected_newer_incomplete_is_suppressed_from_preview_digest(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_version(conn, latest, rules_sha, external_id="3", content_hash="old-ok", collection_status="COLLECTED", pred=prediction("WATCH", **FULL), company="Preview Stale Co")
    insert_version(conn, latest, rules_sha, external_id="3", content_hash="new-bad", collection_status="INCOMPLETE_CONTENT", pred=prediction("WATCH", **FULL), company="Preview Stale Co")
    conn.close()

    fail_all_llm(monkeypatch)
    cli.cmd_preview_digest(config)
    output = capsys.readouterr().out
    assert "Preview Stale Co" not in output


def test_older_collected_newer_incomplete_is_suppressed_from_send_digest(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_version(conn, latest, rules_sha, external_id="4", content_hash="old-ok", collection_status="COLLECTED", pred=prediction("WATCH", **FULL), company="Send Stale Co")
    insert_version(conn, latest, rules_sha, external_id="4", content_hash="new-bad", collection_status="INCOMPLETE_CONTENT", pred=prediction("WATCH", **FULL), company="Send Stale Co")
    conn.close()

    messages = patch_agentmail(monkeypatch)
    fail_all_llm(monkeypatch)
    with contextlib.redirect_stdout(io.StringIO()):
        cli.cmd_send_digest(config, dry_run=False)
    assert all("Send Stale Co" not in call["text"] for call in messages.calls)


def test_all_versions_still_shows_historical_screening(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_version(conn, latest, rules_sha, external_id="5", content_hash="old-ok", collection_status="COLLECTED", pred=prediction("WATCH", **FULL), company="History Co")
    insert_version(conn, latest, rules_sha, external_id="5", content_hash="new-bad", collection_status="INCOMPLETE_CONTENT", pred=prediction("INSUFFICIENT_INFORMATION", downside="Unknown"), company="History Co")
    conn.close()

    assert "History Co" in _display_companies(config, all_versions=True)


def test_newer_version_becoming_collected_with_valid_screen_becomes_current(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_version(conn, latest, rules_sha, external_id="6", content_hash="old-ok", collection_status="COLLECTED", pred=prediction("WATCH", **FULL), company="Recovered Co")
    new_id, _ = insert_version(conn, latest, rules_sha, external_id="6", content_hash="new-bad", collection_status="INCOMPLETE_CONTENT", pred=prediction("WATCH", **FULL), company="Recovered Co")
    conn.close()
    assert "Recovered Co" not in _display_companies(config)

    # Recovery: the newer version is repaired to COLLECTED and gets a valid screen.
    conn = db.connect(config.database_path)
    conn.execute("UPDATE collected_sources SET collection_status = 'COLLECTED' WHERE source_id = ?", (new_id,))
    conn.commit()
    conn.close()

    assert "Recovered Co" in _display_companies(config)
    conn = db.connect(config.database_path)
    assert "Recovered Co" in _digest_companies(conn, latest["version_number"])
    conn.close()


# --- read-only narrative audit ------------------------------------------------------


def _audit(config, source_name=None):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert cli.cmd_audit_screening_narratives(config, source_name) == 0
    return buf.getvalue()


def test_audit_finds_incomplete_watch(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_version(conn, latest, rules_sha, external_id="144273", content_hash="w", collection_status="COLLECTED", pred=prediction("WATCH"), company="I-Tech AB")
    conn.close()

    output = _audit(config)
    assert "I-Tech AB" in output
    assert "external_id=144273" in output
    assert "Missing: key_reasons, key_concerns, critical_questions" in output


def test_audit_finds_incomplete_investigate_now(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_version(conn, latest, rules_sha, external_id="7", content_hash="i", collection_status="COLLECTED", pred=prediction("INVESTIGATE_NOW", reasons=["r"]), company="Invest Co")
    conn.close()

    output = _audit(config)
    assert "Invest Co" in output
    assert "INVESTIGATE_NOW" in output
    assert "key_concerns" in output


def test_audit_does_not_report_complete_watch(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_version(conn, latest, rules_sha, external_id="8", content_hash="c", collection_status="COLLECTED", pred=prediction("WATCH", **FULL), company="Complete Co")
    conn.close()

    assert "Complete Co" not in _audit(config)


def test_successful_repair_removes_row_from_audit(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    _, screening_id = insert_version(conn, latest, rules_sha, external_id="9", content_hash="r", collection_status="COLLECTED", pred=prediction("WATCH", reasons=["r"], concerns=["c"]), company="Repair Co")
    conn.execute(
        "INSERT INTO source_screening_narrative_repairs (screening_id, attempted_at, model_name, status, missing_fields_json, key_reasons_json, key_concerns_json, critical_questions_json) VALUES (?, '2026-01-01', 'm', 'REPAIRED', '[\"critical_questions\"]', '[]', '[]', '[\"What drives it?\"]')",
        (screening_id,),
    )
    conn.commit()
    conn.close()

    assert "Repair Co" not in _audit(config)


def test_pass_sparse_narrative_is_ignored_by_audit(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_version(conn, latest, rules_sha, external_id="10", content_hash="p", collection_status="COLLECTED", pred=prediction("PASS"), company="Pass Co")
    insert_version(conn, latest, rules_sha, external_id="11", content_hash="ii", collection_status="COLLECTED", pred=prediction("INSUFFICIENT_INFORMATION", downside="Unknown"), company="Insufficient Co")
    conn.close()

    output = _audit(config)
    assert "Pass Co" not in output
    assert "Insufficient Co" not in output


def test_newest_invalid_version_suppresses_older_screen_in_audit(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_version(conn, latest, rules_sha, external_id="12", content_hash="old", collection_status="COLLECTED", pred=prediction("WATCH"), company="Hidden Old Co")
    insert_version(conn, latest, rules_sha, external_id="12", content_hash="new", collection_status="INCOMPLETE_CONTENT", pred=prediction("WATCH"), company="Hidden Old Co")
    conn.close()

    assert "Hidden Old Co" not in _audit(config)


def test_audit_is_read_only_zero_mutation(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_version(conn, latest, rules_sha, external_id="13", content_hash="w", collection_status="COLLECTED", pred=prediction("WATCH"), company="Mutation Co")
    conn.close()

    before_s, before_c = snapshot_screenings(config), snapshot_collected(config)
    fail_all_llm(monkeypatch)
    _audit(config)
    _audit(config, "yellowbrick")
    assert snapshot_screenings(config) == before_s
    assert snapshot_collected(config) == before_c
