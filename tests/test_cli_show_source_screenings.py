"""Tests for `python run.py show-source-screenings [--limit N]` -- a
small, strictly READ-ONLY audit command (Stage 5.5). Never writes to
digest_shown_sources, never marks anything as seen, never triggers
extraction/screening, never calls an LLM. Everything lives under
pytest's tmp_path; no production state is ever touched.
"""

from __future__ import annotations

import dataclasses
import json

from ideascout import cli, db, idea_extraction, shadow
from ideascout.config import Config
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


def make_screened_source(
    conn,
    *,
    external_id: str,
    created_at: str,
    version: str = "v1",
    previous_version_source_id: int | None = None,
    ticker: str | None = "XYZ",
    company: str | None = "Example Company",
    source_title: str | None = "Example Company writeup",
    overall_prediction: str = "PASS",
    mispricing: str = "Weak",
    variant_perception: str = "Weak",
    upside: str = "Unknown",
    business_quality: str = "Plausible",
    downside: str = "Acceptable",
    confidence: str = "HIGH",
    key_reasons: list[str] | None = None,
    key_concerns: list[str] | None = None,
    critical_questions: list[str] | None = None,
    canonical_url: str | None = None,
) -> int:
    """Directly inserts one collected_sources row + one source_screenings
    row, bypassing the whole collect-source/extraction/screening
    pipeline, for precise control over test scenarios (missing fields,
    timestamps, JSON content). `version` distinguishes multiple stored
    versions of the SAME external_id (each needs its own content_hash --
    collected_sources has a UNIQUE(source_name, external_id, content_hash)
    constraint) -- pass a different `version` string per call to simulate
    a document with more than one stored version.
    """
    canonical_url = canonical_url or f"https://www.joinyellowbrick.com/sp/{external_id}"
    content_hash = f"hash-{external_id}-{version}"
    source_id = db.insert_collected_source(
        conn, source_name="yellowbrick", external_id=external_id, canonical_url=canonical_url,
        discovered_at=created_at, discovery_title=source_title, source_date=None,
        source_title=source_title, author=None, ticker=ticker, company=company,
        source_type="stock_pitch", content_hash=content_hash, raw_capture_hash=f"raw-{external_id}-{version}",
        raw_html_path=f"/x/{external_id}-{version}.html", metadata_json="{}", created_at=created_at,
        previous_version_source_id=previous_version_source_id,
    )
    db.update_collected_source_extracted(
        conn, source_id=source_id, company=company, ticker=ticker, source_title=source_title,
        source_date=None, business_summary="x", core_thesis="x", why_mispriced="x",
        future_earnings_change="x", upside_case="x", downside_or_key_risks="x", catalysts="x",
        what_must_be_true="x", evidence_of_market_misunderstanding="x", known_unknowns="x",
        extraction_model_name="fake-model", extracted_at=created_at,
    )
    db.insert_source_screening(
        conn, source_id=source_id, created_at=created_at, taste_version=1, taste_sha256="tastehash",
        screen_rules_path="/x/IDEA_SCREEN_RULES.md", screen_rules_sha256="ruleshash",
        content_hash=content_hash, model_name="fake-model", overall_prediction=overall_prediction,
        mispricing=mispricing, variant_perception=variant_perception, upside=upside,
        business_quality=business_quality, downside=downside,
        key_reasons_json=json.dumps(key_reasons if key_reasons is not None else ["Hidden earnings power."]),
        key_concerns_json=json.dumps(key_concerns if key_concerns is not None else ["Execution risk."]),
        critical_questions_json=json.dumps(
            critical_questions if critical_questions is not None else ["When does the segment breakeven?"]
        ),
        confidence=confidence,
    )
    return source_id


# --- basic display / limits / ordering ----------------------------------------


def test_no_screenings_yet(tmp_path, capsys):
    config = make_config(tmp_path)
    exit_code = cli.cmd_show_source_screenings(config, limit=10)
    assert exit_code == 0
    assert "No source screenings recorded yet." in capsys.readouterr().out


def test_default_limit_is_ten(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    for i in range(15):
        make_screened_source(conn, external_id=str(i), created_at=f"2026-01-01T00:{i:02d}:00+00:00")
    conn.close()

    exit_code = cli.cmd_show_source_screenings(config, limit=10)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert output.count("| PASS") == 10


def test_custom_limit(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    for i in range(15):
        make_screened_source(conn, external_id=str(i), created_at=f"2026-01-01T00:{i:02d}:00+00:00")
    conn.close()

    exit_code = cli.cmd_show_source_screenings(config, limit=3)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert output.count("| PASS") == 3


def test_newest_first(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    make_screened_source(conn, external_id="1", created_at="2026-01-01T00:00:00+00:00", ticker="OLD")
    make_screened_source(conn, external_id="2", created_at="2026-01-03T00:00:00+00:00", ticker="NEWEST")
    make_screened_source(conn, external_id="3", created_at="2026-01-02T00:00:00+00:00", ticker="MIDDLE")
    conn.close()

    cli.cmd_show_source_screenings(config, limit=10)
    output = capsys.readouterr().out

    pos_newest = output.index("NEWEST")
    pos_middle = output.index("MIDDLE")
    pos_old = output.index("OLD")
    assert pos_newest < pos_middle < pos_old


# --- document-level deduplication (Stage 5.6) ----------------------------------


def test_two_versions_of_same_external_id_appear_once_by_default(tmp_path, capsys):
    """Reproduces the reported Samsung duplicate: two stored versions of
    the same (source_name, external_id), from before the Stage 5.4
    idempotency fix, must show up only ONCE in the default audit view.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    make_screened_source(
        conn, external_id="samsung1", created_at="2026-01-01T00:00:00+00:00", version="v1",
        ticker="SSNLF", company="Samsung Electronics",
    )
    make_screened_source(
        conn, external_id="samsung1", created_at="2026-01-02T00:00:00+00:00", version="v2",
        ticker="SSNLF", company="Samsung Electronics",
    )
    conn.close()

    exit_code = cli.cmd_show_source_screenings(config, limit=10)
    assert exit_code == 0
    output = capsys.readouterr().out

    assert output.count("SSNLF | Samsung Electronics") == 1


def test_default_view_chooses_the_newest_version(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    v1_id = make_screened_source(
        conn, external_id="1", created_at="2026-01-01T00:00:00+00:00", version="v1",
        overall_prediction="PASS",
    )
    make_screened_source(
        conn, external_id="1", created_at="2026-01-02T00:00:00+00:00", version="v2",
        overall_prediction="WATCH", previous_version_source_id=v1_id,
    )
    conn.close()

    cli.cmd_show_source_screenings(config, limit=10)
    output = capsys.readouterr().out

    assert "| WATCH" in output
    assert "| PASS" not in output


def test_all_versions_flag_shows_both(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    make_screened_source(
        conn, external_id="1", created_at="2026-01-01T00:00:00+00:00", version="v1", overall_prediction="PASS"
    )
    make_screened_source(
        conn, external_id="1", created_at="2026-01-02T00:00:00+00:00", version="v2", overall_prediction="WATCH"
    )
    conn.close()

    exit_code = cli.cmd_show_source_screenings(config, limit=10, all_versions=True)
    assert exit_code == 0
    output = capsys.readouterr().out

    assert "| PASS" in output
    assert "| WATCH" in output


def test_all_versions_flag_preserves_newest_first_ordering(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    make_screened_source(
        conn, external_id="1", created_at="2026-01-01T00:00:00+00:00", version="v1", overall_prediction="PASS"
    )
    make_screened_source(
        conn, external_id="1", created_at="2026-01-02T00:00:00+00:00", version="v2", overall_prediction="WATCH"
    )
    conn.close()

    cli.cmd_show_source_screenings(config, limit=10, all_versions=True)
    output = capsys.readouterr().out

    assert output.index("WATCH") < output.index("PASS")


def test_limit_applies_after_deduplication_not_before(tmp_path, capsys):
    """Three documents, one of which has 3 stored versions (9 screening
    rows total) -- the default view must still return exactly 3 rows (one
    per document) when limit=10, never counting extra historical versions
    against the limit.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    for v in ("v1", "v2", "v3"):
        make_screened_source(conn, external_id="dup", created_at=f"2026-01-01T00:00:00+00:00", version=v, ticker="DUP")
    make_screened_source(conn, external_id="other1", created_at="2026-01-02T00:00:00+00:00", ticker="OTH1")
    make_screened_source(conn, external_id="other2", created_at="2026-01-03T00:00:00+00:00", ticker="OTH2")
    conn.close()

    exit_code = cli.cmd_show_source_screenings(config, limit=10)
    assert exit_code == 0
    output = capsys.readouterr().out

    assert output.count("| PASS") == 3
    assert output.count("DUP |") == 1


def test_limit_of_one_after_dedup_returns_the_single_newest_document(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    make_screened_source(conn, external_id="dup", created_at="2026-01-01T00:00:00+00:00", version="v1", ticker="DUP")
    make_screened_source(conn, external_id="dup", created_at="2026-01-02T00:00:00+00:00", version="v2", ticker="DUP")
    make_screened_source(conn, external_id="other", created_at="2026-01-03T00:00:00+00:00", ticker="OTHER")
    conn.close()

    exit_code = cli.cmd_show_source_screenings(config, limit=1)
    assert exit_code == 0
    output = capsys.readouterr().out

    assert output.count("| PASS") == 1
    assert "OTHER" in output
    assert "DUP" not in output


def test_different_external_ids_remain_separate_under_deduplication(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    make_screened_source(conn, external_id="1", created_at="2026-01-01T00:00:00+00:00", ticker="AAA")
    make_screened_source(conn, external_id="2", created_at="2026-01-02T00:00:00+00:00", ticker="BBB")
    conn.close()

    cli.cmd_show_source_screenings(config, limit=10)
    output = capsys.readouterr().out

    assert output.count("| PASS") == 2
    assert "AAA" in output
    assert "BBB" in output


def test_deduplication_read_only_guarantees_still_hold(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    make_screened_source(conn, external_id="1", created_at="2026-01-01T00:00:00+00:00", version="v1")
    make_screened_source(conn, external_id="1", created_at="2026-01-02T00:00:00+00:00", version="v2")
    conn.close()

    before_sources = [dict(r) for r in db.connect(config.database_path).execute("SELECT * FROM collected_sources")]
    before_screenings = [dict(r) for r in db.connect(config.database_path).execute("SELECT * FROM source_screenings")]

    cli.cmd_show_source_screenings(config, limit=10)
    cli.cmd_show_source_screenings(config, limit=10, all_versions=True)

    after_sources = [dict(r) for r in db.connect(config.database_path).execute("SELECT * FROM collected_sources")]
    after_screenings = [dict(r) for r in db.connect(config.database_path).execute("SELECT * FROM source_screenings")]
    digest_rows = db.connect(config.database_path).execute("SELECT COUNT(*) FROM digest_shown_sources").fetchone()[0]

    assert before_sources == after_sources
    assert before_screenings == after_screenings
    assert digest_rows == 0


# --- graceful fallback for missing ticker/company ------------------------------


def test_missing_ticker_falls_back_to_company_only(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    make_screened_source(conn, external_id="1", created_at="2026-01-01T00:00:00+00:00", ticker=None, company="Solo Company")
    conn.close()

    cli.cmd_show_source_screenings(config, limit=10)
    output = capsys.readouterr().out
    assert "Solo Company | PASS" in output


def test_missing_company_falls_back_to_ticker_only(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    make_screened_source(conn, external_id="1", created_at="2026-01-01T00:00:00+00:00", ticker="SOLO", company=None)
    conn.close()

    cli.cmd_show_source_screenings(config, limit=10)
    output = capsys.readouterr().out
    assert "SOLO | PASS" in output


def test_missing_ticker_and_company_falls_back_to_source_title(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    make_screened_source(
        conn, external_id="1", created_at="2026-01-01T00:00:00+00:00",
        ticker=None, company=None, source_title="Mystery Pitch Writeup",
    )
    conn.close()

    cli.cmd_show_source_screenings(config, limit=10)
    output = capsys.readouterr().out
    assert "Mystery Pitch Writeup | PASS" in output


def test_missing_everything_falls_back_to_external_id(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    make_screened_source(
        conn, external_id="999999", created_at="2026-01-01T00:00:00+00:00",
        ticker=None, company=None, source_title=None,
    )
    conn.close()

    cli.cmd_show_source_screenings(config, limit=10)
    output = capsys.readouterr().out
    assert "999999 | PASS" in output


# --- field display / JSON parsing / caps ---------------------------------------


def test_all_categorical_fields_and_url_are_shown(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    make_screened_source(
        conn, external_id="1", created_at="2026-01-01T00:00:00+00:00", ticker="ABCD", company="Example Company",
        overall_prediction="PASS", mispricing="Weak", variant_perception="Weak", upside="Unknown",
        business_quality="Plausible", downside="Acceptable", confidence="HIGH",
        canonical_url="https://www.joinyellowbrick.com/sp/1",
    )
    conn.close()

    cli.cmd_show_source_screenings(config, limit=10)
    output = capsys.readouterr().out

    assert "ABCD | Example Company | PASS" in output
    assert "Mispricing: Weak | Variant: Weak | Upside: Unknown" in output
    assert "Business quality: Plausible | Downside: Acceptable | Confidence: HIGH" in output
    assert "URL: https://www.joinyellowbrick.com/sp/1" in output


def test_json_reasons_concerns_questions_are_parsed_and_displayed(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    make_screened_source(
        conn, external_id="1", created_at="2026-01-01T00:00:00+00:00",
        key_reasons=["Hidden earnings power.", "Clear mispricing reason."],
        key_concerns=["Execution risk.", "Balance sheet leverage."],
        critical_questions=["When does the segment breakeven?", "Who runs the new division?"],
    )
    conn.close()

    cli.cmd_show_source_screenings(config, limit=10)
    output = capsys.readouterr().out

    assert "Why:" in output
    assert "- Hidden earnings power." in output
    assert "- Clear mispricing reason." in output
    assert "Concerns:" in output
    assert "- Execution risk." in output
    assert "- Balance sheet leverage." in output
    assert "Questions:" in output
    assert "- When does the segment breakeven?" in output
    assert "- Who runs the new division?" in output


def test_caps_at_two_reasons_concerns_and_questions(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    make_screened_source(
        conn, external_id="1", created_at="2026-01-01T00:00:00+00:00",
        key_reasons=["Reason A", "Reason B", "Reason C"],
        key_concerns=["Concern A", "Concern B", "Concern C"],
        critical_questions=["Question A", "Question B", "Question C"],
    )
    conn.close()

    cli.cmd_show_source_screenings(config, limit=10)
    output = capsys.readouterr().out

    assert "Reason A" in output and "Reason B" in output
    assert "Reason C" not in output
    assert "Concern A" in output and "Concern B" in output
    assert "Concern C" not in output
    assert "Question A" in output and "Question B" in output
    assert "Question C" not in output


def test_zero_reasons_concerns_questions_does_not_crash(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    make_screened_source(
        conn, external_id="1", created_at="2026-01-01T00:00:00+00:00",
        key_reasons=[], key_concerns=[], critical_questions=[],
    )
    conn.close()

    exit_code = cli.cmd_show_source_screenings(config, limit=10)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Why:" in output
    assert "Concerns:" in output
    assert "Questions:" in output


# --- read-only guarantees -------------------------------------------------------


def test_never_writes_to_digest_shown_sources(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = make_screened_source(conn, external_id="1", created_at="2026-01-01T00:00:00+00:00")
    conn.close()

    cli.cmd_show_source_screenings(config, limit=10)

    conn = db.connect(config.database_path)
    shown = conn.execute("SELECT COUNT(*) FROM digest_shown_sources").fetchone()[0]
    is_this_one_shown = conn.execute(
        "SELECT COUNT(*) FROM digest_shown_sources WHERE source_id = ?", (source_id,)
    ).fetchone()[0]
    conn.close()

    assert shown == 0
    assert is_this_one_shown == 0


def test_never_mutates_any_existing_row(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    make_screened_source(conn, external_id="1", created_at="2026-01-01T00:00:00+00:00")
    conn.close()

    before_sources = [dict(r) for r in db.connect(config.database_path).execute("SELECT * FROM collected_sources")]
    before_screenings = [dict(r) for r in db.connect(config.database_path).execute("SELECT * FROM source_screenings")]

    cli.cmd_show_source_screenings(config, limit=10)

    after_sources = [dict(r) for r in db.connect(config.database_path).execute("SELECT * FROM collected_sources")]
    after_screenings = [dict(r) for r in db.connect(config.database_path).execute("SELECT * FROM source_screenings")]

    assert before_sources == after_sources
    assert before_screenings == after_screenings


def test_never_calls_an_llm(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    make_screened_source(conn, external_id="1", created_at="2026-01-01T00:00:00+00:00")
    conn.close()

    def fail_if_called(*args, **kwargs):
        raise AssertionError("show-source-screenings must never build an LLM client")

    monkeypatch.setattr(idea_extraction, "build_client", fail_if_called)
    monkeypatch.setattr(idea_extraction, "extract_idea", fail_if_called)
    monkeypatch.setattr(shadow, "build_client", fail_if_called)
    monkeypatch.setattr(shadow, "screen_idea", fail_if_called)

    exit_code = cli.cmd_show_source_screenings(config, limit=10)
    assert exit_code == 0


def test_refuses_when_production_database_missing(tmp_path):
    import pytest

    config = Config(
        agentmail_api_key=None, agentmail_inbox_id=None,
        database_path=tmp_path / "ideas.db",  # deliberately never created
        log_path=tmp_path / "app.log", anthropic_api_key=None,
        screen_rules_path=tmp_path / "IDEA_SCREEN_RULES.md",
        browser_profiles_dir=tmp_path / "browser-profiles",
        raw_storage_dir=tmp_path / "raw",
    )
    with pytest.raises(db.ProductionDatabaseMissingError):
        cli.cmd_show_source_screenings(config, limit=10)


def test_uses_only_tmp_path_state(tmp_path):
    """Sanity check on the test infrastructure itself: the config this
    file's tests build always points under tmp_path, never at real
    production state.
    """
    config = make_config(tmp_path)
    real_idea_scout_local = __import__("pathlib").Path.home() / "IdeaScoutLocal"
    assert config.database_path.is_relative_to(tmp_path)
    assert real_idea_scout_local not in config.database_path.parents
