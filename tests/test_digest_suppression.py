"""Tests for cross-source "already judged idea" suppression in digest
selection (Stage 7): an idea Brad has already judged via email (a
genuinely learning-eligible feedback row) must never resurface in a
website-source digest as if it were a new discovery -- even from a
materially changed later source version.

No network calls; every database lives under pytest's tmp_path. Reuses
test_cli_send_digest.py's fixtures (insert_scored_source uses a "YB{id}"
ticker default specifically to avoid colliding with build_taste_v1's own
"T{i}" training-record tickers).
"""

from __future__ import annotations

from ideascout import cli, db
from tests.test_cli_build_taste import make_needs_review_message, make_parsed_message
from tests.test_cli_send_digest import build_taste_v1, insert_scored_source, make_config, write_screen_rules


def judge_ticker(conn, message_id: str, *, ticker: str, company: str | None = None, excluded: bool = False) -> int:
    """Inserts one genuinely PARSED, learning-eligible feedback row
    judging `ticker` (unless excluded=True, which flips
    excluded_from_learning after insertion) -- the shape of a real Brad
    email judgment.
    """
    feedback_id = make_parsed_message(conn, message_id, ticker=ticker, company=company)
    if excluded:
        db.set_feedback_excluded_from_learning(conn, feedback_id, True)
    return feedback_id


# --- primary ticker match ----------------------------------------------------


def test_same_exact_ticker_suppresses_yellowbrick_candidate(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)

    conn = db.connect(config.database_path)
    judge_ticker(conn, "msg_bbgi_judgment", ticker="BBGI", company="Beasley Broadcast Group")
    insert_scored_source(
        conn, latest_taste, config, external_id="1", overall_prediction="WATCH",
        ticker="BBGI", company="Beasley Broadcast Group",
    )
    selected, suppressed_count = cli.select_digest_candidates(conn, latest_taste["version_number"])
    conn.close()

    assert suppressed_count == 1
    assert selected == []


def test_dollar_and_lowercase_ticker_normalize_the_same(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)

    conn = db.connect(config.database_path)
    judge_ticker(conn, "msg_bbgi_judgment", ticker="$BBGI")
    insert_scored_source(conn, latest_taste, config, external_id="1", overall_prediction="WATCH", ticker="bbgi")
    selected, suppressed_count = cli.select_digest_candidates(conn, latest_taste["version_number"])
    conn.close()

    assert suppressed_count == 1
    assert selected == []


def test_different_ticker_is_not_suppressed(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)

    conn = db.connect(config.database_path)
    judge_ticker(conn, "msg_bbgi_judgment", ticker="BBGI")
    insert_scored_source(conn, latest_taste, config, external_id="1", overall_prediction="WATCH", ticker="ZZZZ")
    selected, suppressed_count = cli.select_digest_candidates(conn, latest_taste["version_number"])
    conn.close()

    assert suppressed_count == 0
    assert len(selected) == 1


# --- eligibility gating: excluded / non-PARSED never suppress ---------------


def test_excluded_from_learning_feedback_does_not_suppress(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)

    conn = db.connect(config.database_path)
    judge_ticker(conn, "msg_bbgi_excluded", ticker="BBGI", excluded=True)
    insert_scored_source(conn, latest_taste, config, external_id="1", overall_prediction="WATCH", ticker="BBGI")
    selected, suppressed_count = cli.select_digest_candidates(conn, latest_taste["version_number"])
    conn.close()

    assert suppressed_count == 0
    assert len(selected) == 1


def test_non_parsed_feedback_does_not_suppress(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)

    conn = db.connect(config.database_path)
    make_needs_review_message(conn, "msg_bbgi_needs_review", ticker="BBGI")
    insert_scored_source(conn, latest_taste, config, external_id="1", overall_prediction="WATCH", ticker="BBGI")
    selected, suppressed_count = cli.select_digest_candidates(conn, latest_taste["version_number"])
    conn.close()

    assert suppressed_count == 0
    assert len(selected) == 1


# --- company-name fallback (ticker-absent candidates only) ------------------


def test_company_name_fallback_works_only_when_source_ticker_absent(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)

    conn = db.connect(config.database_path)
    judge_ticker(conn, "msg_beasley_judgment", ticker=None, company="Beasley Broadcast Group, Inc.")
    insert_scored_source(
        conn, latest_taste, config, external_id="1", overall_prediction="WATCH",
        ticker=None, company="Beasley Broadcast Group Inc",
    )
    selected, suppressed_count = cli.select_digest_candidates(conn, latest_taste["version_number"])
    conn.close()

    assert suppressed_count == 1
    assert selected == []


def test_company_name_fallback_never_used_when_candidate_has_a_ticker(tmp_path, monkeypatch):
    """A candidate WITH its own ticker that doesn't match anything must
    never fall through to a company-name check -- even if its company
    name happens to match a DIFFERENT judged company's normalized name
    under an unrelated ticker.
    """
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)

    conn = db.connect(config.database_path)
    judge_ticker(conn, "msg_judgment", ticker="OTHERTICKER", company="Beasley Broadcast Group")
    insert_scored_source(
        conn, latest_taste, config, external_id="1", overall_prediction="WATCH",
        ticker="BBGI", company="Beasley Broadcast Group",
    )
    selected, suppressed_count = cli.select_digest_candidates(conn, latest_taste["version_number"])
    conn.close()

    assert suppressed_count == 0
    assert len(selected) == 1


def test_fuzzy_or_similar_company_names_do_not_accidentally_suppress(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)

    conn = db.connect(config.database_path)
    judge_ticker(conn, "msg_judgment", ticker=None, company="Beasley Broadcast Group")
    # Similar but NOT identical after normalization -- must NOT suppress.
    insert_scored_source(
        conn, latest_taste, config, external_id="1", overall_prediction="WATCH",
        ticker=None, company="Beasley Broadcasting Group",
    )
    selected, suppressed_count = cli.select_digest_candidates(conn, latest_taste["version_number"])
    conn.close()

    assert suppressed_count == 0
    assert len(selected) == 1


def test_no_ticker_and_no_company_on_candidate_is_never_suppressed(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)

    conn = db.connect(config.database_path)
    judge_ticker(conn, "msg_judgment", ticker="BBGI", company="Beasley Broadcast Group")
    insert_scored_source(conn, latest_taste, config, external_id="1", overall_prediction="WATCH", ticker=None, company=None)
    selected, suppressed_count = cli.select_digest_candidates(conn, latest_taste["version_number"])
    conn.close()

    assert suppressed_count == 0
    assert len(selected) == 1


# --- suppression never mutates state ----------------------------------------


def test_suppression_does_not_write_digest_shown_sources(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)

    conn = db.connect(config.database_path)
    judge_ticker(conn, "msg_judgment", ticker="BBGI")
    source_id = insert_scored_source(conn, latest_taste, config, external_id="1", overall_prediction="WATCH", ticker="BBGI")
    cli.select_digest_candidates(conn, latest_taste["version_number"])
    shown_count = conn.execute(
        "SELECT COUNT(*) FROM digest_shown_sources WHERE source_id = ?", (source_id,)
    ).fetchone()[0]
    conn.close()

    assert shown_count == 0


def test_suppression_does_not_alter_source_screenings(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)

    conn = db.connect(config.database_path)
    judge_ticker(conn, "msg_judgment", ticker="BBGI")
    source_id = insert_scored_source(conn, latest_taste, config, external_id="1", overall_prediction="WATCH", ticker="BBGI")
    before = dict(conn.execute("SELECT * FROM source_screenings WHERE source_id = ?", (source_id,)).fetchone())

    cli.select_digest_candidates(conn, latest_taste["version_number"])

    after = dict(conn.execute("SELECT * FROM source_screenings WHERE source_id = ?", (source_id,)).fetchone())
    conn.close()

    assert before == after


def test_suppression_via_preview_digest_makes_no_db_writes(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)

    conn = db.connect(config.database_path)
    judge_ticker(conn, "msg_judgment", ticker="BBGI")
    insert_scored_source(conn, latest_taste, config, external_id="1", overall_prediction="WATCH", ticker="BBGI")
    conn.close()

    exit_code = cli.cmd_preview_digest(config)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Already-judged ideas suppressed: 1" in output
    assert "No new ideas to show" in output

    conn = db.connect(config.database_path)
    assert conn.execute("SELECT COUNT(*) FROM digest_shown_sources").fetchone()[0] == 0
    conn.close()


# --- version semantics: suppression survives a materially changed version --


def test_materially_changed_latest_version_remains_suppressed_if_same_judged_ticker(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)

    conn = db.connect(config.database_path)
    judge_ticker(conn, "msg_judgment", ticker="BBGI")
    # Old version.
    insert_scored_source(
        conn, latest_taste, config, external_id="1", overall_prediction="WATCH",
        ticker="BBGI", content_hash="old-content", created_at="2026-01-01T00:00:00+00:00",
    )
    # A NEW, materially changed version of the SAME external_id -- still BBGI.
    insert_scored_source(
        conn, latest_taste, config, external_id="1", overall_prediction="INVESTIGATE_NOW",
        ticker="BBGI", content_hash="new-content", created_at="2026-01-02T00:00:00+00:00",
    )
    selected, suppressed_count = cli.select_digest_candidates(conn, latest_taste["version_number"])
    conn.close()

    assert suppressed_count == 1
    assert selected == []


# --- unrelated behavior explicitly unchanged --------------------------------


def test_pass_predictions_still_excluded_independent_of_suppression(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)

    conn = db.connect(config.database_path)
    judge_ticker(conn, "msg_judgment", ticker="BBGI")
    insert_scored_source(conn, latest_taste, config, external_id="1", overall_prediction="PASS", ticker="BBGI")
    insert_scored_source(conn, latest_taste, config, external_id="2", overall_prediction="WATCH", ticker="OTHER")
    selected, suppressed_count = cli.select_digest_candidates(conn, latest_taste["version_number"])
    conn.close()

    # The PASS row is excluded by the existing non-PASS filter, not by
    # suppression -- it must never be counted as "already-judged suppressed".
    assert suppressed_count == 0
    assert len(selected) == 1
    assert selected[0]["ticker"] == "OTHER"


def test_ordering_and_max_five_unchanged_alongside_suppression(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)

    conn = db.connect(config.database_path)
    judge_ticker(conn, "msg_judgment", ticker="BBGI")
    # One suppressed candidate, plus 6 legitimate ones (3 WATCH, 3 INVESTIGATE_NOW).
    insert_scored_source(
        conn, latest_taste, config, external_id="suppressed", overall_prediction="INVESTIGATE_NOW", ticker="BBGI",
    )
    for i in range(3):
        insert_scored_source(
            conn, latest_taste, config, external_id=f"watch{i}", overall_prediction="WATCH",
            created_at=f"2026-01-01T00:0{i}:00+00:00",
        )
    for i in range(3):
        insert_scored_source(
            conn, latest_taste, config, external_id=f"inv{i}", overall_prediction="INVESTIGATE_NOW",
            created_at=f"2026-01-01T00:1{i}:00+00:00",
        )
    selected, suppressed_count = cli.select_digest_candidates(conn, latest_taste["version_number"])
    conn.close()

    assert suppressed_count == 1
    assert len(selected) == 5
    predictions_in_order = [row["overall_prediction"] for row in selected]
    assert predictions_in_order.count("INVESTIGATE_NOW") == 3
    assert predictions_in_order[:3] == ["INVESTIGATE_NOW"] * 3  # INVESTIGATE_NOW still sorts first


def test_preview_and_send_digest_agree_on_suppression(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)

    conn = db.connect(config.database_path)
    judge_ticker(conn, "msg_judgment", ticker="BBGI")
    insert_scored_source(conn, latest_taste, config, external_id="1", overall_prediction="WATCH", ticker="BBGI")
    conn.close()

    cli.cmd_preview_digest(config)
    preview_output = capsys.readouterr().out

    from tests.test_cli_send_digest import patch_agentmail

    patch_agentmail(monkeypatch)
    cli.cmd_send_digest(config, dry_run=True)
    send_output = capsys.readouterr().out

    assert "Already-judged ideas suppressed: 1" in preview_output
    assert "Already-judged ideas suppressed: 1" in send_output


# --- pure normalization helpers, directly measured --------------------------


def test_normalize_ticker_strips_dollar_and_whitespace_and_uppercases():
    assert cli._normalize_ticker("  $bbgi  ") == "BBGI"
    assert cli._normalize_ticker("BBGI") == "BBGI"
    assert cli._normalize_ticker(None) is None
    assert cli._normalize_ticker("") is None
    assert cli._normalize_ticker("   ") is None


def test_normalize_company_name_strips_basic_punctuation_and_case():
    assert cli._normalize_company_name("Beasley Broadcast Group, Inc.") == "beasley broadcast group inc"
    assert cli._normalize_company_name("Beasley Broadcast Group Inc") == "beasley broadcast group inc"
    assert cli._normalize_company_name(None) is None


def test_normalize_company_name_does_not_merge_similar_but_different_names():
    assert cli._normalize_company_name("Beasley Broadcast Group") != cli._normalize_company_name(
        "Beasley Broadcasting Group"
    )
