"""Regression tests for re-observation of historical source content under
latest-version semantics (Migration 17's current-source pointer).

Production case: yellowbrick/144273 had a valid semantic version A, then a
later invalid (PAGE_NOT_FOUND) version B. A later valid fetch produced the
same semantic hash as A, so the idempotent path correctly reused A's row
(UNIQUE(source_name, external_id, content_hash) forbids a duplicate) -- but
"current" was defined as max(source_id), so B stayed current forever.

The fix records the most recently OBSERVED semantic version per document in
collected_source_current. Every successful observation moves the pointer; no
row is inserted just to make it newest, and no provenance is deleted.

Collect-source scenarios run the real command with a fake browser and
monkeypatched extraction/screening (no LLM, no network). Every database lives
under pytest's tmp_path.
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3

from ideascout import cli, db, shadow
from tests.test_cli_collect_source_idempotent_retry import (
    NOT_FOUND_HTML,
    PITCH_URL,
    REAL_PITCH_HTML,
    build_taste_v1,
    fail_llm,
    make_config,
    make_page,
    patch_browser,
    patch_extraction_and_screening,
    write_screen_rules,
)
from tests.test_cli_shadow import make_prediction

# Valid, but a different semantic version C (not the same hash as A).
REAL_PITCH_VARIANT_HTML = REAL_PITCH_HTML.replace(
    "trades at a discount after", "trades at a deep discount after"
)
assert REAL_PITCH_VARIANT_HTML != REAL_PITCH_HTML


def _observe(config, monkeypatch, pitch_html, *, extraction_calls=None, screening_calls=None):
    patch_browser(monkeypatch, make_page(pitch_html))
    if extraction_calls is not None:
        patch_extraction_and_screening(monkeypatch, extraction_calls=extraction_calls, screening_calls=screening_calls)
    else:
        patch_extraction_and_screening(monkeypatch)
    assert cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10) == 0


EXTERNAL_ID = PITCH_URL.rsplit("/", 1)[-1]


def _versions(config):
    conn = db.connect(config.database_path)
    rows = [dict(r) for r in conn.execute(
        "SELECT source_id, content_hash, collection_status, raw_html_path FROM collected_sources "
        "WHERE external_id = ? ORDER BY source_id", (EXTERNAL_ID,))]
    current = db.get_latest_collected_source(conn, "yellowbrick", EXTERNAL_ID)
    conn.close()
    return rows, (current["source_id"] if current else None)


def _display(config, *, all_versions=False):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.cmd_show_source_screenings(config, limit=50, all_versions=all_versions)
    return buf.getvalue()


def _audit(config):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.cmd_audit_screening_narratives(config, "yellowbrick")
    return buf.getvalue()


# --- CASE 1: A -> B -> A ----------------------------------------------------------


def test_valid_invalid_valid_again_makes_a_current_without_duplicate(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    _observe(config, monkeypatch, REAL_PITCH_HTML)  # A
    _observe(config, monkeypatch, NOT_FOUND_HTML)  # B invalid
    rows_after_b, current_after_b = _versions(config)
    a_id = rows_after_b[0]["source_id"]
    b_id = rows_after_b[1]["source_id"]
    assert current_after_b == b_id

    _observe(config, monkeypatch, REAL_PITCH_HTML)  # valid again -- same hash as A
    rows, current = _versions(config)

    assert len(rows) == 2  # no duplicate A row
    assert current == a_id  # A is current again
    assert [r["source_id"] for r in rows] == [a_id, b_id]
    # B is preserved historically, exactly as it was stored.
    assert rows[1]["collection_status"] == "INCOMPLETE_CONTENT"
    assert rows[0]["collection_status"] == "COLLECTED"


def test_blind_review_eligibility_uses_a_after_recovery(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)
    _observe(config, monkeypatch, REAL_PITCH_HTML)  # A
    _observe(config, monkeypatch, NOT_FOUND_HTML)  # B invalid -> A is not current
    rows, _ = _versions(config)
    a_id = rows[0]["source_id"]

    conn = db.connect(config.database_path)
    while_b_current = {r["source_id"] for r in db.get_blind_review_candidate_sources(conn)}
    conn.close()
    assert a_id not in while_b_current  # superseded by the invalid B

    _observe(config, monkeypatch, REAL_PITCH_HTML)  # A recovered -> current again
    conn = db.connect(config.database_path)
    after_recovery = {r["source_id"] for r in db.get_blind_review_candidate_sources(conn)}
    conn.close()
    assert a_id in after_recovery  # A is current, EXTRACTED and otherwise eligible


def test_display_shows_a_after_recovery(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)
    _observe(config, monkeypatch, REAL_PITCH_HTML)
    _observe(config, monkeypatch, NOT_FOUND_HTML)
    assert f"sp/{EXTERNAL_ID}" not in _display(config)  # suppressed while B is current
    _observe(config, monkeypatch, REAL_PITCH_HTML)
    assert f"sp/{EXTERNAL_ID}" in _display(config)  # A is current again


def test_digest_considers_a_after_recovery(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    _observe(config, monkeypatch, REAL_PITCH_HTML)
    _observe(config, monkeypatch, NOT_FOUND_HTML)
    _observe(config, monkeypatch, REAL_PITCH_HTML)

    conn = db.connect(config.database_path)
    selected, _, _ = cli.select_digest_candidates_detailed(conn, latest["version_number"])
    conn.close()
    selected_ids = {row["source_id"] for row in selected}
    rows, current = _versions(config)
    assert current in selected_ids


def test_narrative_audit_considers_a_after_recovery(tmp_path, monkeypatch, capsys):
    """A is observed, screened as WATCH with no narrative, then B invalid,
    then A recovered: the audit must list A (the current version) and not B.
    """
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)

    incomplete = make_prediction()
    incomplete = shadow.ShadowPrediction(
        overall_prediction="WATCH", mispricing="Plausible", variant_perception="Plausible",
        upside="Potentially sufficient", business_quality="Plausible", downside="Problematic",
        key_reasons=[], key_concerns=[], critical_questions=[], confidence="MEDIUM",
    )
    patch_browser(monkeypatch, make_page(REAL_PITCH_HTML))
    patch_extraction_and_screening(monkeypatch)
    monkeypatch.setattr(shadow, "screen_idea", lambda *a, **k: incomplete)
    monkeypatch.setattr(shadow, "repair_narrative", lambda *a, **k: shadow.NarrativeRepair())
    assert cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10) == 0

    _observe(config, monkeypatch, NOT_FOUND_HTML)
    assert f"external_id={EXTERNAL_ID}" not in _audit(config)  # B current -> suppressed

    _observe(config, monkeypatch, REAL_PITCH_HTML)  # A recovered
    assert f"external_id={EXTERNAL_ID}" in _audit(config)


# --- CASE 2: B stays current while it remains invalid -------------------------------


def test_repeated_invalid_b_remains_current_and_suppressed(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)
    _observe(config, monkeypatch, REAL_PITCH_HTML)  # A
    _observe(config, monkeypatch, NOT_FOUND_HTML)  # B
    rows_b1, current_b1 = _versions(config)
    _observe(config, monkeypatch, NOT_FOUND_HTML)  # B again
    rows_b2, current_b2 = _versions(config)

    assert len(rows_b2) == 2  # no duplicate B
    assert current_b1 == current_b2 == rows_b1[1]["source_id"]  # B remains current, retryable
    assert f"sp/{EXTERNAL_ID}" not in _display(config)


# --- CASE 3: new valid C after B becomes current ------------------------------------


def test_new_valid_c_after_b_becomes_current(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)
    _observe(config, monkeypatch, REAL_PITCH_HTML)  # A
    _observe(config, monkeypatch, NOT_FOUND_HTML)  # B
    extraction_calls, screening_calls = [], []
    _observe(config, monkeypatch, REAL_PITCH_VARIANT_HTML, extraction_calls=extraction_calls, screening_calls=screening_calls)  # C

    rows, current = _versions(config)
    assert len(rows) == 3
    assert current == rows[2]["source_id"]  # C is the new current version
    assert len(extraction_calls) == 1  # C extracted once
    assert len(screening_calls) == 1


# --- CASE 4: repeated unchanged A is idempotent ----------------------------------------


def test_repeated_unchanged_a_is_idempotent_and_stays_current(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)
    _observe(config, monkeypatch, REAL_PITCH_HTML)
    rows_1, current_1 = _versions(config)
    _observe(config, monkeypatch, REAL_PITCH_HTML)
    _observe(config, monkeypatch, REAL_PITCH_HTML)
    rows_3, current_3 = _versions(config)

    assert rows_3 == rows_1  # no new rows, byte-identical provenance
    assert current_1 == current_3 == rows_1[0]["source_id"]


# --- CASE 5: history stays visible --------------------------------------------------------


def test_all_versions_retains_full_history_across_recovery(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)
    _observe(config, monkeypatch, REAL_PITCH_HTML)  # A
    _observe(config, monkeypatch, NOT_FOUND_HTML)  # B
    _observe(config, monkeypatch, REAL_PITCH_VARIANT_HTML)  # C
    _observe(config, monkeypatch, REAL_PITCH_HTML)  # A again

    rows, _ = _versions(config)
    assert len(rows) == 3  # A, B, C -- all preserved, nothing deleted or rewritten
    raw_paths = [r["raw_html_path"] for r in rows]
    assert all(_exists(p) for p in raw_paths)

    history = _display(config, all_versions=True)
    assert f"sp/{EXTERNAL_ID}" in history


def _exists(path):
    from pathlib import Path

    return Path(path).exists()


# --- migration 17 backfill -----------------------------------------------------------------


def test_migration_17_backfills_pointer_to_existing_max_source_id(tmp_path):
    db_path = tmp_path / "ideas.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    for sql in db.MIGRATIONS[:16]:
        conn.executescript(sql)
    conn.execute("PRAGMA user_version = 16")
    conn.execute(
        "INSERT INTO collected_sources (source_name, external_id, canonical_url, discovered_at, discovery_title, "
        "source_title, content_hash, raw_html_path, metadata_json, created_at, collection_status) VALUES "
        "('yellowbrick','144273','u','2026-01-01','t','t','hA','/a','{}','2026-01-01','COLLECTED')"
    )
    conn.execute(
        "INSERT INTO collected_sources (source_name, external_id, canonical_url, discovered_at, discovery_title, "
        "source_title, content_hash, raw_html_path, metadata_json, created_at, collection_status) VALUES "
        "('yellowbrick','144273','u','2026-01-02','t','t','hB','/b','{}','2026-01-02','INCOMPLETE_CONTENT')"
    )
    conn.commit()
    conn.close()

    conn = db.connect(db_path)  # applies migration 17
    pointer = conn.execute(
        "SELECT source_id FROM collected_source_current WHERE external_id = '144273'"
    ).fetchone()
    max_id = conn.execute("SELECT MAX(source_id) FROM collected_sources").fetchone()[0]
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()

    assert pointer["source_id"] == max_id  # backfill matches the old "current" meaning
    assert version == len(db.MIGRATIONS)


def test_collected_source_unique_constraint_still_enforced(tmp_path):
    """The recovery fix must not weaken UNIQUE(source_name, external_id, content_hash)."""
    import pytest

    config_path = tmp_path / "ideas.db"
    conn = db.connect(config_path)
    kwargs = dict(
        source_name="yellowbrick", external_id="1", canonical_url="u", discovered_at="2026-01-01",
        discovery_title="t", source_date=None, source_title="t", author=None, ticker=None, company=None,
        source_type="stock_pitch", content_hash="same", raw_html_path="/x", metadata_json="{}",
        created_at="2026-01-01",
    )
    db.insert_collected_source(conn, **kwargs)
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_collected_source(conn, **kwargs)
    conn.close()
