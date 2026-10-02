"""Tests for the Yellowbrick 404/invalid-page production fix at the CLI
level: collect-source's integration with the new page-validity gate,
downstream eligibility (blind-review/digest) correctly excluding an
invalid latest source even when old extraction/screening rows exist, and
the new `audit-source-content` / `repair-source-content` commands.

Never launches a real browser; source_browser.persistent_chrome_context is
monkeypatched to a fake context/page (tests/browser_fakes.py). No real LLM
calls -- idea_extraction/shadow are monkeypatched wherever extraction or
screening could run. Every database lives under pytest's tmp_path.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from ideascout import cli, db, idea_extraction, shadow, taste
from ideascout.sources import browser as source_browser
from ideascout.sources import yellowbrick
from tests.browser_fakes import FakePage, make_fake_persistent_chrome_context
from tests.test_cli_build_taste import insert_n_eligible, make_config as _base_make_config, patch_taste
from tests.test_cli_shadow import make_extracted_idea, make_prediction

NOT_FOUND_HTML = """
<html><head><title>Yellowbrick Investing</title></head><body>
<h1>Page not found</h1>
<p>The page you are looking for does not exist, has been removed, or is
temporarily unavailable.</p>
</body></html>
"""

REAL_PITCH_HTML = """
<html><head><title>INS.DE stock pitch - 2026-09-28</title></head><body>
<article class="pitch-card">
<h1>Instone Real Estate Group SE (INS.DE)</h1>
<time datetime="2026-09-28">Sep 28</time>
<p>Instone trades at a steep discount to NAV after a sector-wide derating.
The core land bank is conservatively marked and recent presales suggest
the market is too pessimistic about near-term cash generation. Catalysts
include asset disposals and a return to dividend payments. Key risks
include financing costs and continued weakness in German residential
construction.</p>
</article>
</body></html>
"""

FEED_HTML_144271 = """
<html><body>
<article class="pitch-card">
  <h2>Instone Real Estate Group SE</h2>
  <a href="/sp/144271">Read full article</a>
</article>
</body></html>
"""

PITCH_URL_144271 = "https://www.joinyellowbrick.com/sp/144271"


def make_config(tmp_path, **overrides):
    config = _base_make_config(tmp_path)
    fields = {
        "browser_profiles_dir": tmp_path / "browser-profiles",
        "raw_storage_dir": tmp_path / "raw",
        "screen_rules_path": tmp_path / "IDEA_SCREEN_RULES.md",
    }
    fields.update(overrides)
    return dataclasses.replace(config, **fields)


def write_screen_rules(config, content: str = "1. Mispricing\n   Is there a specific reason the market may be wrong?\n"):
    config.screen_rules_path.write_text(content, encoding="utf-8", newline="")


def build_taste_v1(config, monkeypatch, body: str = "## Strong Positive Signals\nHidden earnings power.\n"):
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()
    patch_taste(monkeypatch, body=body)
    exit_code = cli.cmd_build_taste(config)
    assert exit_code == 0
    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    conn.close()
    return latest


def make_page_144271(*, pitch_html=NOT_FOUND_HTML, content_sequence=None) -> FakePage:
    kwargs = {}
    if content_sequence is not None:
        kwargs["content_sequence_by_url"] = {PITCH_URL_144271: content_sequence}
        html_by_url = {yellowbrick.HOME_URL: FEED_HTML_144271, yellowbrick.FEED_URL: FEED_HTML_144271}
    else:
        html_by_url = {
            yellowbrick.HOME_URL: FEED_HTML_144271,
            yellowbrick.FEED_URL: FEED_HTML_144271,
            PITCH_URL_144271: pitch_html,
        }
    return FakePage(html_by_url, **kwargs)


def patch_browser(monkeypatch, page: FakePage):
    fake_cm, context = make_fake_persistent_chrome_context(page)
    monkeypatch.setattr(source_browser, "persistent_chrome_context", fake_cm)
    return context


def patch_extraction_and_screening(monkeypatch, *, extraction_calls=None, screening_calls=None):
    extracted = make_extracted_idea()
    prediction = make_prediction()

    def fake_extract_idea(client, model, source_text, logger=None):
        if extraction_calls is not None:
            extraction_calls.append(source_text)
        return extracted

    def fake_screen_idea(client, model, *, idea_taste_body, screen_rules_body, idea_record, logger=None):
        if screening_calls is not None:
            screening_calls.append(dict(idea_record))
        return prediction

    monkeypatch.setattr(idea_extraction, "build_client", lambda api_key: object())
    monkeypatch.setattr(idea_extraction, "extract_idea", fake_extract_idea)
    monkeypatch.setattr(shadow, "build_client", lambda api_key: object())
    monkeypatch.setattr(shadow, "screen_idea", fake_screen_idea)


def fail_llm(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("must never call an LLM for an invalid/incomplete source")

    monkeypatch.setattr(idea_extraction, "build_client", fail_if_called)
    monkeypatch.setattr(idea_extraction, "extract_idea", fail_if_called)
    monkeypatch.setattr(shadow, "build_client", fail_if_called)
    monkeypatch.setattr(shadow, "screen_idea", fail_if_called)


# --- collect-source integration: 404/invalid pages ------------------------------


def test_collect_source_persistent_404_saved_incomplete_no_llm_calls(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)

    page = make_page_144271(content_sequence=[NOT_FOUND_HTML, NOT_FOUND_HTML, NOT_FOUND_HTML])
    patch_browser(monkeypatch, page)
    fail_llm(monkeypatch)

    exit_code = cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "INCOMPLETE_CONTENT: 144271" in output
    assert "Incomplete content (saved, not extracted/screened): 1" in output

    conn = db.connect(config.database_path)
    row = db.get_latest_collected_source(conn, "yellowbrick", "144271")
    conn.close()

    assert row["collection_status"] == "INCOMPLETE_CONTENT"
    assert row["extraction_status"] == "PENDING"
    assert json.loads(row["metadata_json"])["invalid_reason"] == "PAGE_NOT_FOUND"
    assert Path(row["raw_html_path"]).exists()  # raw provenance still saved


def test_collect_source_transient_404_recovers_within_same_run(tmp_path, monkeypatch, capsys):
    """fetch()'s own bounded wait/reload retry recovers the page WITHIN
    one collect-source run -- no second run needed.
    """
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)

    page = make_page_144271(content_sequence=[NOT_FOUND_HTML, NOT_FOUND_HTML, REAL_PITCH_HTML])
    patch_browser(monkeypatch, page)
    extraction_calls, screening_calls = [], []
    patch_extraction_and_screening(monkeypatch, extraction_calls=extraction_calls, screening_calls=screening_calls)

    exit_code = cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Incomplete content (saved, not extracted/screened): 0" in output

    conn = db.connect(config.database_path)
    row = db.get_latest_collected_source(conn, "yellowbrick", "144271")
    conn.close()

    assert row["collection_status"] == "COLLECTED"
    assert row["extraction_status"] == "EXTRACTED"
    assert len(extraction_calls) == 1
    assert len(screening_calls) == 1


def test_collect_source_404_then_later_recovery_extracts_and_screens_once(tmp_path, monkeypatch, capsys):
    """FUTURE RECOVERY (task section 4): Day 1 -> 404 -> incomplete, no
    extraction. Later -> a fresh collect-source run (same external_id,
    already 'known') -> valid pitch -> new version saved, extracted once,
    screened once. The known/incomplete external_id is never permanently
    skipped by ordinary dedupe.
    """
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)

    day1_page = make_page_144271(content_sequence=[NOT_FOUND_HTML, NOT_FOUND_HTML, NOT_FOUND_HTML])
    patch_browser(monkeypatch, day1_page)
    fail_llm(monkeypatch)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    capsys.readouterr()

    conn = db.connect(config.database_path)
    assert db.count_collected_source_versions(conn, "yellowbrick") == 1
    conn.close()

    later_page = make_page_144271(pitch_html=REAL_PITCH_HTML)
    patch_browser(monkeypatch, later_page)
    extraction_calls, screening_calls = [], []
    patch_extraction_and_screening(monkeypatch, extraction_calls=extraction_calls, screening_calls=screening_calls)

    exit_code = cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Incomplete content (saved, not extracted/screened): 0" in output

    conn = db.connect(config.database_path)
    latest = db.get_latest_collected_source(conn, "yellowbrick", "144271")
    versions = db.count_collected_source_versions(conn, "yellowbrick")
    conn.close()

    assert latest["collection_status"] == "COLLECTED"
    assert latest["extraction_status"] == "EXTRACTED"
    assert versions == 2  # the incomplete v1 + the new valid version
    assert len(extraction_calls) == 1
    assert len(screening_calls) == 1


def test_collect_source_144270_and_144269_remain_safely_incomplete_if_still_404(tmp_path, monkeypatch, capsys):
    """Direct reproduction of the production scenario: 144270 and 144269
    are both still 404 on refetch -- both must stay non-extractable,
    neither should ever reach extraction/screening.
    """
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)

    feed_html = """
    <html><body>
    <article class="pitch-card"><h2>Pitch A</h2><a href="/sp/144270">Read full article</a></article>
    <article class="pitch-card"><h2>Pitch B</h2><a href="/sp/144269">Read full article</a></article>
    </body></html>
    """
    url_144270 = "https://www.joinyellowbrick.com/sp/144270"
    url_144269 = "https://www.joinyellowbrick.com/sp/144269"
    page = FakePage(
        {
            yellowbrick.HOME_URL: feed_html,
            yellowbrick.FEED_URL: feed_html,
            url_144270: NOT_FOUND_HTML,
            url_144269: NOT_FOUND_HTML,
        }
    )
    patch_browser(monkeypatch, page)
    fail_llm(monkeypatch)

    exit_code = cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Incomplete content (saved, not extracted/screened): 2" in output

    conn = db.connect(config.database_path)
    row_a = db.get_latest_collected_source(conn, "yellowbrick", "144270")
    row_b = db.get_latest_collected_source(conn, "yellowbrick", "144269")
    conn.close()

    for row in (row_a, row_b):
        assert row["collection_status"] == "INCOMPLETE_CONTENT"
        assert row["extraction_status"] == "PENDING"


def test_different_external_ids_with_identical_404_hash_no_cross_source_confusion(tmp_path, monkeypatch):
    """Several different external_ids can legitimately share the identical
    404 canonical hash -- this must never create a UNIQUE constraint
    collision or cross-source confusion, since the constraint is scoped
    by (source_name, external_id, content_hash).
    """
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)

    feed_html = """
    <html><body>
    <article class="pitch-card"><h2>Pitch A</h2><a href="/sp/1">Read full article</a></article>
    <article class="pitch-card"><h2>Pitch B</h2><a href="/sp/2">Read full article</a></article>
    <article class="pitch-card"><h2>Pitch C</h2><a href="/sp/3">Read full article</a></article>
    </body></html>
    """
    page = FakePage(
        {
            yellowbrick.HOME_URL: feed_html,
            yellowbrick.FEED_URL: feed_html,
            "https://www.joinyellowbrick.com/sp/1": NOT_FOUND_HTML,
            "https://www.joinyellowbrick.com/sp/2": NOT_FOUND_HTML,
            "https://www.joinyellowbrick.com/sp/3": NOT_FOUND_HTML,
        }
    )
    patch_browser(monkeypatch, page)
    fail_llm(monkeypatch)

    exit_code = cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    rows = [db.get_latest_collected_source(conn, "yellowbrick", eid) for eid in ("1", "2", "3")]
    conn.close()

    content_hashes = {row["content_hash"] for row in rows}
    external_ids = {row["external_id"] for row in rows}
    assert len(content_hashes) == 1  # identical 404 content -> identical hash
    assert len(external_ids) == 3  # but each remains its own distinct source


# --- downstream eligibility: blind-review / digest -------------------------------


def _insert_collected_and_extracted(conn, *, external_id, ticker, company, collection_status="COLLECTED"):
    source_id = db.insert_collected_source(
        conn, source_name="yellowbrick", external_id=external_id,
        canonical_url=f"https://www.joinyellowbrick.com/sp/{external_id}",
        discovered_at="2026-01-01T00:00:00+00:00", discovery_title=company, source_date=None,
        source_title=company, author=None, ticker=ticker, company=company,
        source_type="stock_pitch", content_hash=f"hash-{external_id}", raw_html_path=f"/x/{external_id}.html",
        metadata_json="{}", created_at="2026-01-01T00:00:00+00:00", collection_status=collection_status,
    )
    db.update_collected_source_extracted(
        conn, source_id=source_id, company=company, ticker=ticker, source_title=company,
        source_date=None, business_summary="x", core_thesis="x", why_mispriced="x",
        future_earnings_change="x", upside_case="x", downside_or_key_risks="x", catalysts="x",
        what_must_be_true="x", evidence_of_market_misunderstanding="x", known_unknowns="x",
        extraction_model_name="fake-model", extracted_at="2026-01-01T00:00:00+00:00",
    )
    return source_id


def test_invalid_latest_source_excluded_from_blind_review_despite_old_extraction(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = _insert_collected_and_extracted(conn, external_id="144271", ticker="INS", company="Instone")
    # Simulate the historical bug being repaired: extraction_status stays
    # EXTRACTED (never deleted), but collection_status is corrected.
    db.mark_collected_source_invalid(conn, source_id=source_id, reason="PAGE_NOT_FOUND")
    conn.close()

    conn = db.connect(config.database_path)
    candidates = db.get_blind_review_candidate_sources(conn)
    conn.close()

    assert source_id not in {row["source_id"] for row in candidates}


def test_invalid_latest_source_excluded_from_digest_despite_old_screening(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)

    conn = db.connect(config.database_path)
    source_id = _insert_collected_and_extracted(conn, external_id="144271", ticker="INS", company="Instone")
    db.insert_source_screening(
        conn, source_id=source_id, created_at="2026-01-01T00:00:00+00:00",
        taste_version=latest_taste["version_number"], taste_sha256=latest_taste["idea_taste_sha256"],
        screen_rules_path=str(config.screen_rules_path), screen_rules_sha256="rules_hash",
        content_hash="hash-144271", model_name="fake-model", overall_prediction="WATCH",
        mispricing="Plausible", variant_perception="Plausible", upside="Potentially sufficient",
        business_quality="Plausible", downside="Acceptable",
        key_reasons_json='["Reason one."]', key_concerns_json='["Concern one."]',
        critical_questions_json='["Question one?"]', confidence="MEDIUM",
    )
    # Historical bug repaired: collection_status corrected, screening kept.
    db.mark_collected_source_invalid(conn, source_id=source_id, reason="PAGE_NOT_FOUND")
    conn.close()

    conn = db.connect(config.database_path)
    candidates = db.get_unshown_screened_sources_for_taste_version(conn, latest_taste["version_number"])
    selected, _ = cli.select_digest_candidates(conn, latest_taste["version_number"])
    conn.close()

    assert source_id not in {row["source_id"] for row in candidates}
    assert source_id not in {row["source_id"] for row in selected}


def test_diagnostics_still_show_historical_invalid_record(tmp_path, capsys):
    """Repairing/excluding a source from selection must never hide it from
    explicit read-only diagnostics.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = db.insert_collected_source(
        conn, source_name="yellowbrick", external_id="144271",
        canonical_url="https://www.joinyellowbrick.com/sp/144271",
        discovered_at="2026-01-01T00:00:00+00:00", discovery_title="Instone", source_date=None,
        source_title="Instone", author=None, ticker=None, company=None,
        source_type="stock_pitch", content_hash="hash-144271",
        raw_html_path=str((tmp_path / "raw_144271.html")), metadata_json="{}",
        created_at="2026-01-01T00:00:00+00:00", collection_status="COLLECTED",
    )
    (tmp_path / "raw_144271.html").write_text(NOT_FOUND_HTML, encoding="utf-8")
    db.mark_collected_source_invalid(conn, source_id=source_id, reason="PAGE_NOT_FOUND")
    conn.close()

    exit_code = cli.cmd_diagnose_source_versions(config, "yellowbrick", "144271")
    assert exit_code == 0
    output = capsys.readouterr().out
    assert f"source_id={source_id}" in output
    assert "collection_status: INCOMPLETE_CONTENT" in output
    assert "invalid_reason:    PAGE_NOT_FOUND" in output


# --- audit-source-content / repair-source-content --------------------------------


def _real_content_hash(html: str) -> str:
    """The EXACT hash fetch()/parse_pitch_page would compute for this raw
    HTML -- used so a test's simulated "already captured" row has the SAME
    content_hash a live refetch of unchanged content would produce (the
    real repair path compares the two to decide new-version vs. repair-
    in-place).
    """
    import hashlib

    return hashlib.sha256(yellowbrick.build_canonical_source_text(html).encode("utf-8")).hexdigest()


def _insert_bad_collected_source(tmp_path, conn, *, external_id, raw_html=NOT_FOUND_HTML) -> int:
    """Simulates the historical bug: a 404 page wrongly left as COLLECTED
    (i.e. captured before classify_page_validity existed).
    """
    raw_path = tmp_path / "raw" / "yellowbrick" / external_id
    raw_path.mkdir(parents=True, exist_ok=True)
    content_hash = _real_content_hash(raw_html)
    html_path = raw_path / f"{content_hash}.html"
    html_path.write_text(raw_html, encoding="utf-8")
    return db.insert_collected_source(
        conn, source_name="yellowbrick", external_id=external_id,
        canonical_url=f"https://www.joinyellowbrick.com/sp/{external_id}",
        discovered_at="2026-01-01T00:00:00+00:00", discovery_title="Yellowbrick Investing", source_date=None,
        source_title="Yellowbrick Investing", author=None, ticker=None, company=None,
        source_type="stock_pitch", content_hash=content_hash, raw_html_path=str(html_path),
        metadata_json="{}", created_at="2026-01-01T00:00:00+00:00", collection_status="COLLECTED",
    )


def test_audit_is_read_only_and_finds_deterministic_bad_rows(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    bad_id = _insert_bad_collected_source(tmp_path, conn, external_id="144271")
    good_id = db.insert_collected_source(
        conn, source_name="yellowbrick", external_id="999999",
        canonical_url="https://www.joinyellowbrick.com/sp/999999",
        discovered_at="2026-01-01T00:00:00+00:00", discovery_title="Good Co", source_date=None,
        source_title="Good Co", author=None, ticker="GOOD", company="Good Co",
        source_type="stock_pitch", content_hash="hash-good", raw_html_path=str(tmp_path / "good.html"),
        metadata_json="{}", created_at="2026-01-01T00:00:00+00:00", collection_status="COLLECTED",
    )
    (tmp_path / "good.html").write_text(REAL_PITCH_HTML, encoding="utf-8")
    conn.close()

    before_sources = _snapshot(config)

    exit_code = cli.cmd_audit_source_content(config, "yellowbrick")
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "1 suspect yellowbrick source(s)" in output
    assert "external_id=144271" in output
    assert f"source_id={bad_id}" in output
    assert "reason=PAGE_NOT_FOUND" in output
    assert "999999" not in output

    assert _snapshot(config) == before_sources  # zero DB mutation


def _snapshot(config):
    conn = db.connect(config.database_path)
    rows = [dict(r) for r in conn.execute("SELECT * FROM collected_sources ORDER BY source_id")]
    conn.close()
    return rows


def test_repair_dry_run_changes_nothing(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    _insert_bad_collected_source(tmp_path, conn, external_id="144271")
    conn.close()

    before = _snapshot(config)

    exit_code = cli.cmd_repair_source_content(config, "yellowbrick", dry_run=True)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "1 suspect yellowbrick source(s) to repair" in output
    assert "[DRY RUN]" in output
    assert "No database changes made" in output

    assert _snapshot(config) == before


def test_repair_real_recovers_now_valid_source_and_leaves_it_pending_extraction(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    old_source_id = _insert_bad_collected_source(tmp_path, conn, external_id="144271")
    conn.close()

    recovered_page = FakePage({PITCH_URL_144271: REAL_PITCH_HTML})
    patch_browser(monkeypatch, recovered_page)
    fail_llm(monkeypatch)  # repair-source-content itself must never call an LLM

    exit_code = cli.cmd_repair_source_content(config, "yellowbrick", dry_run=False)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Backup created:" in output
    assert "now valid" in output
    assert "collect-source yellowbrick" in output  # tells Brad the follow-up command

    conn = db.connect(config.database_path)
    latest = db.get_latest_collected_source(conn, "yellowbrick", "144271")
    old_row = conn.execute("SELECT * FROM collected_sources WHERE source_id = ?", (old_source_id,)).fetchone()
    conn.close()

    assert latest["source_id"] != old_source_id
    assert latest["collection_status"] == "COLLECTED"
    assert latest["extraction_status"] == "PENDING"  # repair itself never extracts -- zero LLM calls
    assert latest["previous_version_source_id"] == old_source_id
    # The old bad row is preserved exactly, not deleted or mutated.
    assert old_row is not None
    assert old_row["collection_status"] == "COLLECTED"


def test_repair_real_marks_persistent_404_non_extractable_in_place(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    bad_id = _insert_bad_collected_source(tmp_path, conn, external_id="144270")
    conn.close()

    still_404_page = FakePage({"https://www.joinyellowbrick.com/sp/144270": NOT_FOUND_HTML})
    patch_browser(monkeypatch, still_404_page)
    fail_llm(monkeypatch)

    exit_code = cli.cmd_repair_source_content(config, "yellowbrick", dry_run=False)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "still invalid" in output
    assert "marked non-extractable" in output

    conn = db.connect(config.database_path)
    row = conn.execute("SELECT * FROM collected_sources WHERE source_id = ?", (bad_id,)).fetchone()
    count = conn.execute("SELECT COUNT(*) FROM collected_sources").fetchone()[0]
    conn.close()

    assert row["collection_status"] == "INCOMPLETE_CONTENT"
    assert json.loads(row["metadata_json"])["invalid_reason"] == "PAGE_NOT_FOUND"
    assert count == 1  # repaired IN PLACE -- no new row created


def test_repair_preserves_old_extraction_and_screening_provenance(tmp_path, monkeypatch, capsys):
    """Repairing a source that was ALREADY (wrongly) extracted/screened
    must never delete the extraction fields or the source_screenings row
    -- they stay as audit trail.
    """
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)

    conn = db.connect(config.database_path)
    bad_id = _insert_bad_collected_source(tmp_path, conn, external_id="144270")
    db.update_collected_source_extracted(
        conn, source_id=bad_id, company=None, ticker=None, source_title="Yellowbrick Investing",
        source_date=None, business_summary="", core_thesis="", why_mispriced="",
        future_earnings_change="", upside_case="", downside_or_key_risks="", catalysts="",
        what_must_be_true="", evidence_of_market_misunderstanding="", known_unknowns="",
        extraction_model_name="fake-model", extracted_at="2026-01-01T00:00:00+00:00",
    )
    db.insert_source_screening(
        conn, source_id=bad_id, created_at="2026-01-01T00:00:00+00:00",
        taste_version=latest_taste["version_number"], taste_sha256=latest_taste["idea_taste_sha256"],
        screen_rules_path=str(config.screen_rules_path), screen_rules_sha256="rules_hash",
        content_hash="hash-144270", model_name="fake-model", overall_prediction="INSUFFICIENT_INFORMATION",
        mispricing="Implausible", variant_perception="Implausible", upside="Insufficient",
        business_quality="Implausible", downside="Severe",
        key_reasons_json="[]", key_concerns_json="[]", critical_questions_json="[]", confidence="LOW",
    )
    conn.close()

    still_404_page = FakePage({"https://www.joinyellowbrick.com/sp/144270": NOT_FOUND_HTML})
    patch_browser(monkeypatch, still_404_page)
    fail_llm(monkeypatch)

    cli.cmd_repair_source_content(config, "yellowbrick", dry_run=False)
    capsys.readouterr()

    conn = db.connect(config.database_path)
    row = conn.execute("SELECT * FROM collected_sources WHERE source_id = ?", (bad_id,)).fetchone()
    screenings = conn.execute("SELECT COUNT(*) FROM source_screenings WHERE source_id = ?", (bad_id,)).fetchone()[0]
    conn.close()

    assert row["extraction_status"] == "EXTRACTED"  # never deleted/reset
    assert screenings == 1  # the old screening is kept, not deleted
    assert row["collection_status"] == "INCOMPLETE_CONTENT"  # but no longer eligible for reuse


def test_audit_and_repair_unknown_source(tmp_path, capsys):
    config = make_config(tmp_path)
    assert cli.cmd_audit_source_content(config, "not-a-real-source") == 2
    assert "Unknown source" in capsys.readouterr().out
    assert cli.cmd_repair_source_content(config, "not-a-real-source", dry_run=True) == 2
    assert "Unknown source" in capsys.readouterr().out


def test_audit_no_suspects(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    conn.close()
    exit_code = cli.cmd_audit_source_content(config, "yellowbrick")
    assert exit_code == 0
    assert "No suspect yellowbrick sources found" in capsys.readouterr().out
