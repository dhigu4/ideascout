"""Tests for shadow-score / shadow-status / show-shadow-results (Stage 4:
blind shadow screening for the Taste v1 holdout).

Nothing here ever calls a real LLM (idea_extraction.extract_idea and
shadow.screen_idea are monkeypatched, like taste generation in
test_cli_build_taste.py) and nothing here ever touches real production
state: every database, every idea-taste.md, and every IDEA_SCREEN_RULES.md
lives under pytest's tmp_path. In particular, Config.screen_rules_path
defaults to Brad's REAL InvestmentBrain repo -- make_config() below always
overrides it to a tmp_path file, and a dedicated test proves that default
is never silently used by anything reachable from these tests.
"""

from __future__ import annotations

import dataclasses

import pytest

from ideascout import cli, db, idea_extraction, shadow, structured_llm, taste
from ideascout.config import Config
from tests.test_cli_build_taste import (
    insert_feedback_row,
    insert_n_eligible,
    insert_raw_message,
    make_excluded_message,
    make_needs_review_message,
    make_parsed_message,
    patch_taste,
)
from tests.test_cli_build_taste import make_config as _base_make_config


def make_config(tmp_path, **overrides) -> Config:
    config = _base_make_config(tmp_path)
    fields = {"screen_rules_path": tmp_path / "IDEA_SCREEN_RULES.md"}
    fields.update(overrides)
    return dataclasses.replace(config, **fields)


def write_screen_rules(config, content: str = "1. Mispricing\n   Is there a specific reason the market may be wrong?\n") -> str:
    config.screen_rules_path.write_text(content, encoding="utf-8", newline="")
    return content


def build_taste_v1(config, monkeypatch, body: str = "## Strong Positive Signals\nHidden earnings power is the most consistent theme.\n"):
    patch_taste(monkeypatch, body=body)
    exit_code = cli.cmd_build_taste(config)
    assert exit_code == 0
    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    conn.close()
    return latest


GOOD_SOURCE_BODY = (
    "MAYBE. Interesting but risky.\n\n"
    "---------- Forwarded message ---------\n"
    "From: analyst@example.com\n"
    "Subject: HOLD Corp writeup\n\n"
    "HOLD Corp trades at 5x normalized earnings due to a temporary loss-making "
    "segment that is expected to reach breakeven next year, creating hidden "
    "earnings power the market has not yet recognized."
)

UNSCORABLE_BODY = "MAYBE. Interesting but risky. No forward, no quote, nothing separable here."


def make_holdout_idea(
    conn,
    message_id: str,
    *,
    body_raw: str = GOOD_SOURCE_BODY,
    verdict: str = "MAYBE",
    ticker: str = "HOLD",
    company=None,
    event_type: str = "FEEDBACK",
    user_comment: str = "Interesting but risky.",
) -> int:
    insert_raw_message(conn, message_id, body_raw=body_raw)
    feedback_id = insert_feedback_row(
        conn, message_id, event_type=event_type, verdict=verdict, ticker=ticker, company=company,
        user_comment=user_comment,
    )
    db.set_feedback_parse_status(conn, message_id, "PARSED")
    return feedback_id


def make_extracted_idea(**overrides) -> idea_extraction.ExtractedIdea:
    fields = dict(
        company="HOLD Corp", ticker="HOLD",
        business_summary="A widget maker.", core_thesis="Hidden earnings power.",
        why_mispriced="Loss-making segment obscures true earnings.",
        future_earnings_change="Segment breakeven expected 2027.", upside_case="3-4x.",
        downside_or_key_risks="Execution risk.", catalysts="Segment divestiture.",
        what_must_be_true="Segment losses must actually stop.",
        evidence_of_market_misunderstanding="Sell-side models consolidated losses as permanent.",
        known_unknowns="Exact divestiture timeline.",
    )
    fields.update(overrides)
    return idea_extraction.ExtractedIdea(**fields)


def make_prediction(**overrides) -> shadow.ShadowPrediction:
    fields = dict(
        overall_prediction="WATCH", mispricing="Plausible", variant_perception="Plausible",
        upside="Potentially sufficient", business_quality="Plausible", downside="Acceptable",
        key_reasons=["Hidden earnings power."], key_concerns=["Execution risk."],
        critical_questions=["When does the segment breakeven?"], confidence="MEDIUM",
    )
    fields.update(overrides)
    return shadow.ShadowPrediction(**fields)


def patch_shadow_pipeline(monkeypatch, *, extracted=None, prediction=None, extraction_calls=None, screening_calls=None):
    extracted = extracted if extracted is not None else make_extracted_idea()
    prediction = prediction if prediction is not None else make_prediction()

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
    return extracted, prediction


def setup_taste_and_one_holdout_idea(tmp_path, monkeypatch, **holdout_kwargs):
    config = make_config(tmp_path)
    write_screen_rules(config)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()
    latest_taste = build_taste_v1(config, monkeypatch)

    conn = db.connect(config.database_path)
    feedback_id = make_holdout_idea(conn, "msg_hold_1", **holdout_kwargs)
    conn.close()
    return config, latest_taste, feedback_id


# --- basic pipeline: isolate -> extract -> screen ----------------------------


def test_shadow_score_creates_idea_record_and_prediction_for_scorable_holdout_idea(tmp_path, monkeypatch):
    config, latest_taste, feedback_id = setup_taste_and_one_holdout_idea(tmp_path, monkeypatch)
    patch_shadow_pipeline(monkeypatch)

    exit_code = cli.cmd_shadow_score(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    idea_record = db.get_idea_record_by_message_id(conn, "msg_hold_1")
    assert idea_record["source_status"] == "ISOLATED"
    assert idea_record["extraction_status"] == "EXTRACTED"
    assert idea_record["company"] == "HOLD Corp"

    prediction = db.get_latest_shadow_prediction_for_idea(conn, idea_record["idea_id"])
    assert prediction["overall_prediction"] == "WATCH"
    conn.close()


def test_shadow_score_normalizes_blank_company_and_ticker_to_none_for_persistence(tmp_path, monkeypatch):
    """ExtractedIdea (Stage 5.3) represents "not stated" as an empty
    string, never null -- cli.py's extraction call site normalizes that
    to None before writing to idea_records, since the DB/API's existing
    semantics expect None for "no company/ticker known", not "".
    """
    config, latest_taste, feedback_id = setup_taste_and_one_holdout_idea(tmp_path, monkeypatch)
    patch_shadow_pipeline(monkeypatch, extracted=make_extracted_idea(company="", ticker=""))

    exit_code = cli.cmd_shadow_score(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    idea_record = db.get_idea_record_by_message_id(conn, "msg_hold_1")
    conn.close()

    assert idea_record["company"] is None
    assert idea_record["ticker"] is None


def test_shadow_score_marks_unscorable_source_and_skips_extraction_and_screening(tmp_path, monkeypatch):
    config, latest_taste, feedback_id = setup_taste_and_one_holdout_idea(
        tmp_path, monkeypatch, body_raw=UNSCORABLE_BODY
    )
    extraction_calls, screening_calls = [], []
    patch_shadow_pipeline(monkeypatch, extraction_calls=extraction_calls, screening_calls=screening_calls)

    exit_code = cli.cmd_shadow_score(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    idea_record = db.get_idea_record_by_message_id(conn, "msg_hold_1")
    assert idea_record["source_status"] == "UNSCORABLE_SOURCE"
    assert idea_record["unscorable_reason"]
    assert idea_record["extraction_status"] == "PENDING"
    assert db.get_idea_ids_with_shadow_predictions(conn) == []
    conn.close()

    assert extraction_calls == []
    assert screening_calls == []


# --- idempotency --------------------------------------------------------------


def test_shadow_score_is_idempotent_no_duplicate_idea_record_or_prediction_on_rerun(tmp_path, monkeypatch):
    config, latest_taste, feedback_id = setup_taste_and_one_holdout_idea(tmp_path, monkeypatch)
    patch_shadow_pipeline(monkeypatch)

    cli.cmd_shadow_score(config)
    cli.cmd_shadow_score(config)  # rerun -- everything already done

    conn = db.connect(config.database_path)
    idea_records = conn.execute("SELECT COUNT(*) FROM idea_records").fetchone()[0]
    predictions = conn.execute("SELECT COUNT(*) FROM shadow_predictions").fetchone()[0]
    conn.close()

    assert idea_records == 1
    assert predictions == 1


def test_extraction_failure_leaves_pending_and_is_retried_on_next_run(tmp_path, monkeypatch, capsys):
    config, latest_taste, feedback_id = setup_taste_and_one_holdout_idea(tmp_path, monkeypatch)

    def failing_extract(client, model, source_text, logger=None):
        raise structured_llm.StructuredGenerationError("simulated extraction failure")

    monkeypatch.setattr(idea_extraction, "build_client", lambda api_key: object())
    monkeypatch.setattr(idea_extraction, "extract_idea", failing_extract)
    monkeypatch.setattr(shadow, "build_client", lambda api_key: object())

    cli.cmd_shadow_score(config)

    conn = db.connect(config.database_path)
    idea_record = db.get_idea_record_by_message_id(conn, "msg_hold_1")
    assert idea_record["source_status"] == "ISOLATED"
    assert idea_record["extraction_status"] == "PENDING"  # not a terminal error state
    conn.close()

    # Fix the failure and rerun -- must succeed without needing a manual requeue.
    patch_shadow_pipeline(monkeypatch)
    cli.cmd_shadow_score(config)

    conn = db.connect(config.database_path)
    idea_record = db.get_idea_record_by_message_id(conn, "msg_hold_1")
    assert idea_record["extraction_status"] == "EXTRACTED"
    conn.close()


# --- retrying historical UNSCORABLE_SOURCE rows (Stage 5.13) -----------------


def test_old_unscorable_becomes_isolated_after_isolator_improvement(tmp_path, monkeypatch):
    """The row was marked UNSCORABLE_SOURCE by an OLDER isolator that
    couldn't recognize the inline-pasted-source format; the CURRENT
    isolator (with the Stage 5.13 fallback) can. A later shadow-score run
    must pick it up without any manual requeue.
    """
    inline_body = (
        "Maybe\n\n"
        "Interesting but risky.\n\n"
        "PRN is a company with hidden earnings power due to a temporary "
        "segment loss that the market has not yet priced in.\n"
    )
    config, latest_taste, feedback_id = setup_taste_and_one_holdout_idea(
        tmp_path, monkeypatch, body_raw=inline_body, user_comment="Interesting but risky."
    )

    conn = db.connect(config.database_path)
    # Simulate the historical failure directly (an older isolator that
    # didn't have the inline-paste fallback would have produced exactly
    # this outcome for this body).
    db.insert_idea_record_unscorable(
        conn,
        message_id="msg_hold_1",
        unscorable_reason="no recognized forwarded-message, quoted-reply, or explicit SOURCE: boundary found",
        source_isolated_at="2026-01-01T00:00:00+00:00",
    )
    conn.close()

    extraction_calls, screening_calls = [], []
    patch_shadow_pipeline(monkeypatch, extraction_calls=extraction_calls, screening_calls=screening_calls)
    exit_code = cli.cmd_shadow_score(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    idea_record = db.get_idea_record_by_message_id(conn, "msg_hold_1")
    conn.close()

    assert idea_record["source_status"] == "ISOLATED"
    assert idea_record["source_type"] == "inline_pasted_source"
    assert idea_record["unscorable_reason"] is None
    assert idea_record["extraction_status"] == "EXTRACTED"  # normal extraction proceeded afterward
    assert len(extraction_calls) == 1
    assert len(screening_calls) == 1


def test_still_ambiguous_source_remains_unscorable_after_retry(tmp_path, monkeypatch, capsys):
    config, latest_taste, feedback_id = setup_taste_and_one_holdout_idea(
        tmp_path, monkeypatch, body_raw=UNSCORABLE_BODY
    )

    conn = db.connect(config.database_path)
    db.insert_idea_record_unscorable(
        conn, message_id="msg_hold_1", unscorable_reason="some prior reason",
        source_isolated_at="2026-01-01T00:00:00+00:00",
    )
    idea_id_before = db.get_idea_record_by_message_id(conn, "msg_hold_1")["idea_id"]
    conn.close()

    patch_shadow_pipeline(monkeypatch)
    exit_code = cli.cmd_shadow_score(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    idea_record = db.get_idea_record_by_message_id(conn, "msg_hold_1")
    conn.close()

    assert idea_record["idea_id"] == idea_id_before  # same row, not a new one
    assert idea_record["source_status"] == "UNSCORABLE_SOURCE"
    assert idea_record["source_text"] is None
    assert idea_record["extraction_status"] == "PENDING"

    output = capsys.readouterr().out
    assert "Previously-unscorable sources retried this run: 1" in output
    assert "Previously-unscorable sources now isolated: 0" in output


def test_retry_does_not_create_a_second_idea_record_row(tmp_path, monkeypatch):
    config, latest_taste, feedback_id = setup_taste_and_one_holdout_idea(
        tmp_path, monkeypatch, body_raw=UNSCORABLE_BODY
    )
    conn = db.connect(config.database_path)
    db.insert_idea_record_unscorable(
        conn, message_id="msg_hold_1", unscorable_reason="some prior reason",
        source_isolated_at="2026-01-01T00:00:00+00:00",
    )
    conn.close()

    patch_shadow_pipeline(monkeypatch)
    cli.cmd_shadow_score(config)

    conn = db.connect(config.database_path)
    count = conn.execute("SELECT COUNT(*) FROM idea_records WHERE message_id = ?", ("msg_hold_1",)).fetchone()[0]
    conn.close()
    assert count == 1  # never a junk duplicate row


def test_existing_isolated_and_extracted_records_are_never_reset_by_retry_logic(tmp_path, monkeypatch):
    """The retry path must only ever touch a row whose CURRENT
    source_status is UNSCORABLE_SOURCE -- an already-ISOLATED/EXTRACTED
    row (a completely different message here) is never re-examined,
    re-isolated, or reset.
    """
    config = make_config(tmp_path)
    write_screen_rules(config)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()
    build_taste_v1(config, monkeypatch)

    conn = db.connect(config.database_path)
    make_holdout_idea(conn, "msg_good", body_raw=GOOD_SOURCE_BODY, user_comment="Interesting but risky.")
    make_holdout_idea(
        conn, "msg_bad", body_raw="Maybe\n\nNot enough remainder.\n\n", user_comment="Not enough remainder."
    )
    conn.close()

    extraction_calls, screening_calls = [], []
    patch_shadow_pipeline(monkeypatch, extraction_calls=extraction_calls, screening_calls=screening_calls)
    cli.cmd_shadow_score(config)

    conn = db.connect(config.database_path)
    good_record_before = dict(db.get_idea_record_by_message_id(conn, "msg_good"))
    bad_record_before = dict(db.get_idea_record_by_message_id(conn, "msg_bad"))
    conn.close()
    assert good_record_before["source_status"] == "ISOLATED"
    assert good_record_before["extraction_status"] == "EXTRACTED"
    assert bad_record_before["source_status"] == "UNSCORABLE_SOURCE"

    # Rerun: the good record must be completely untouched; the bad one
    # stays unscorable (still no substantial remainder) with no writes.
    cli.cmd_shadow_score(config)

    conn = db.connect(config.database_path)
    good_record_after = dict(db.get_idea_record_by_message_id(conn, "msg_good"))
    bad_record_after = dict(db.get_idea_record_by_message_id(conn, "msg_bad"))
    conn.close()

    assert good_record_after == good_record_before
    assert bad_record_after == bad_record_before
    assert len(extraction_calls) == 1  # not re-extracted
    assert len(screening_calls) == 1


def test_completed_shadow_prediction_is_never_replaced_by_retry_logic(tmp_path, monkeypatch):
    """A prediction that already exists for a message must survive the
    retry logic untouched -- retrying isolation for OTHER unscorable rows
    must never regenerate or replace an existing prediction.
    """
    config = make_config(tmp_path)
    write_screen_rules(config)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()
    build_taste_v1(config, monkeypatch)

    conn = db.connect(config.database_path)
    make_holdout_idea(conn, "msg_good", body_raw=GOOD_SOURCE_BODY, user_comment="Interesting but risky.")
    make_holdout_idea(conn, "msg_bad", body_raw=UNSCORABLE_BODY, user_comment="Interesting but risky.")
    conn.close()

    patch_shadow_pipeline(monkeypatch)
    cli.cmd_shadow_score(config)

    conn = db.connect(config.database_path)
    idea_record = db.get_idea_record_by_message_id(conn, "msg_good")
    prediction_before = dict(db.get_latest_shadow_prediction_for_idea(conn, idea_record["idea_id"]))
    conn.close()

    # Rerun with a DIFFERENT prediction configured -- if the retry logic
    # ever touched the completed prediction, this would reveal it.
    patch_shadow_pipeline(monkeypatch, prediction=make_prediction(overall_prediction="INVESTIGATE_NOW"))
    cli.cmd_shadow_score(config)

    conn = db.connect(config.database_path)
    all_predictions = conn.execute(
        "SELECT * FROM shadow_predictions WHERE idea_id = ?", (idea_record["idea_id"],)
    ).fetchall()
    conn.close()

    assert len(all_predictions) == 1  # never replaced, never duplicated
    assert dict(all_predictions[0]) == prediction_before


# --- no-leakage at the orchestration level ------------------------------------


def test_shadow_score_never_passes_feedback_content_into_extraction_or_screening(tmp_path, monkeypatch):
    config, latest_taste, feedback_id = setup_taste_and_one_holdout_idea(
        tmp_path, monkeypatch, user_comment="I secretly love this one, STRONG_LIKE material."
    )
    extraction_calls, screening_calls = [], []
    patch_shadow_pipeline(monkeypatch, extraction_calls=extraction_calls, screening_calls=screening_calls)

    cli.cmd_shadow_score(config)

    assert len(extraction_calls) == 1
    assert "secretly love" not in extraction_calls[0]
    assert "STRONG_LIKE" not in extraction_calls[0]
    assert "MAYBE" not in extraction_calls[0]  # Brad's own verdict text, stripped by isolation

    assert len(screening_calls) == 1
    idea_record_dict = screening_calls[0]
    assert "verdict" not in idea_record_dict
    assert "user_comment" not in idea_record_dict
    assert "positive_reasons" not in idea_record_dict
    assert "concerns" not in idea_record_dict


def test_excluded_and_needs_review_feedback_never_get_idea_records(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()
    build_taste_v1(config, monkeypatch)

    conn = db.connect(config.database_path)
    make_excluded_message(conn, "msg_excluded_after", ticker="EXC")
    make_needs_review_message(conn, "msg_review_after", ticker="REV")
    conn.close()

    patch_shadow_pipeline(monkeypatch)
    cli.cmd_shadow_score(config)

    conn = db.connect(config.database_path)
    assert db.get_idea_record_by_message_id(conn, "msg_excluded_after") is None
    assert db.get_idea_record_by_message_id(conn, "msg_review_after") is None
    conn.close()


# --- immutability / reproducibility -------------------------------------------


def test_prediction_records_exact_taste_version_and_hash(tmp_path, monkeypatch):
    config, latest_taste, feedback_id = setup_taste_and_one_holdout_idea(tmp_path, monkeypatch)
    patch_shadow_pipeline(monkeypatch)
    cli.cmd_shadow_score(config)

    conn = db.connect(config.database_path)
    idea_record = db.get_idea_record_by_message_id(conn, "msg_hold_1")
    prediction = db.get_latest_shadow_prediction_for_idea(conn, idea_record["idea_id"])
    conn.close()

    assert prediction["taste_version"] == latest_taste["version_number"]
    assert prediction["taste_sha256"] == latest_taste["idea_taste_sha256"]


def test_prediction_records_exact_screen_rules_hash_and_path(tmp_path, monkeypatch):
    config, latest_taste, feedback_id = setup_taste_and_one_holdout_idea(tmp_path, monkeypatch)
    rules_content = config.screen_rules_path.read_text(encoding="utf-8")
    expected_hash = taste.compute_content_sha256(rules_content)
    patch_shadow_pipeline(monkeypatch)
    cli.cmd_shadow_score(config)

    conn = db.connect(config.database_path)
    idea_record = db.get_idea_record_by_message_id(conn, "msg_hold_1")
    prediction = db.get_latest_shadow_prediction_for_idea(conn, idea_record["idea_id"])
    conn.close()

    assert prediction["screen_rules_sha256"] == expected_hash
    assert prediction["screen_rules_path"] == str(config.screen_rules_path)


def test_prediction_and_idea_record_share_the_same_source_hash(tmp_path, monkeypatch):
    config, latest_taste, feedback_id = setup_taste_and_one_holdout_idea(tmp_path, monkeypatch)
    patch_shadow_pipeline(monkeypatch)
    cli.cmd_shadow_score(config)

    conn = db.connect(config.database_path)
    idea_record = db.get_idea_record_by_message_id(conn, "msg_hold_1")
    prediction = db.get_latest_shadow_prediction_for_idea(conn, idea_record["idea_id"])
    conn.close()

    assert idea_record["source_hash"]
    assert prediction["source_hash"] == idea_record["source_hash"]


def test_predictions_have_no_update_function_only_insert():
    """Structural guarantee of immutability: there is deliberately no
    function in db.py that updates an existing shadow_predictions row.
    """
    assert not hasattr(db, "update_shadow_prediction")


def test_changed_screen_rules_creates_a_new_coexisting_prediction_not_an_overwrite(tmp_path, monkeypatch):
    config, latest_taste, feedback_id = setup_taste_and_one_holdout_idea(tmp_path, monkeypatch)
    patch_shadow_pipeline(monkeypatch)
    cli.cmd_shadow_score(config)

    conn = db.connect(config.database_path)
    idea_record = db.get_idea_record_by_message_id(conn, "msg_hold_1")
    first_prediction = db.get_latest_shadow_prediction_for_idea(conn, idea_record["idea_id"])
    conn.close()

    # The rules file changes -- a new, different screen_rules_sha256.
    write_screen_rules(config, content="1. Mispricing\n   A DIFFERENT rules file entirely.\n")
    patch_shadow_pipeline(monkeypatch, prediction=make_prediction(overall_prediction="INVESTIGATE_NOW"))
    cli.cmd_shadow_score(config)

    conn = db.connect(config.database_path)
    all_predictions = conn.execute(
        "SELECT * FROM shadow_predictions WHERE idea_id = ? ORDER BY prediction_id", (idea_record["idea_id"],)
    ).fetchall()
    conn.close()

    assert len(all_predictions) == 2
    assert all_predictions[0]["prediction_id"] == first_prediction["prediction_id"]
    assert all_predictions[0]["overall_prediction"] == "WATCH"  # old row never touched
    assert all_predictions[1]["overall_prediction"] == "INVESTIGATE_NOW"
    assert all_predictions[0]["screen_rules_sha256"] != all_predictions[1]["screen_rules_sha256"]
    assert all_predictions[0]["taste_version"] == all_predictions[1]["taste_version"]


# --- reveal gating (show-shadow-results) --------------------------------------


def test_show_shadow_results_never_reveals_unjudged_prediction(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    conn = db.connect(config.database_path)
    # A message with source material but NO feedback row at all -- i.e.
    # Brad has not judged it yet. Constructed directly at the DB level
    # since the real shadow-score pipeline (holdout-only, in this stage)
    # can never actually reach an unjudged idea -- this proves the reveal
    # gate itself, defensively, independent of today's pipeline shape.
    insert_raw_message(conn, "msg_unjudged", body_raw=GOOD_SOURCE_BODY)
    idea_id = db.insert_idea_record_isolated(
        conn, message_id="msg_unjudged", source_type="forwarded_email",
        source_text="HOLD Corp trades at 5x normalized earnings.", source_hash="deadbeef",
        source_isolated_at="2026-01-01T00:00:00+00:00",
    )
    db.update_idea_record_extracted(
        conn, idea_id=idea_id, company="HOLD Corp", ticker="HOLD", source_title=None, source_date=None,
        business_summary="x", core_thesis="x", why_mispriced="x", future_earnings_change="x",
        upside_case="x", downside_or_key_risks="x", catalysts="x", what_must_be_true="x",
        evidence_of_market_misunderstanding="x", known_unknowns="x",
        extraction_model_name="fake-model", extracted_at="2026-01-01T00:00:00+00:00",
    )
    db.insert_shadow_prediction(
        conn, idea_id=idea_id, created_at="2026-01-01T00:00:00+00:00", taste_version=1, taste_sha256="abc",
        screen_rules_path=str(config.screen_rules_path), screen_rules_sha256="def", source_hash="deadbeef",
        model_name="fake-model", overall_prediction="WATCH", mispricing="Plausible",
        variant_perception="Plausible", upside="Potentially sufficient", business_quality="Plausible",
        downside="Acceptable", key_reasons_json="[]", key_concerns_json="[]", critical_questions_json="[]",
        confidence="MEDIUM",
    )
    conn.close()

    exit_code = cli.cmd_show_shadow_results(config)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "HOLD Corp" not in output
    assert "WATCH" not in output
    assert "No judged" in output


def test_show_shadow_results_reveals_judged_prediction(tmp_path, monkeypatch, capsys):
    config, latest_taste, feedback_id = setup_taste_and_one_holdout_idea(tmp_path, monkeypatch)
    patch_shadow_pipeline(monkeypatch)
    cli.cmd_shadow_score(config)

    exit_code = cli.cmd_show_shadow_results(config)
    assert exit_code == 0
    output = capsys.readouterr().out

    assert "HOLD Corp" in output
    assert "Shadow prediction: WATCH" in output
    assert "Brad's actual verdict: MAYBE" in output
    assert "Interesting but risky." in output  # Brad's actual comment


# --- holdout integrity ---------------------------------------------------------


def test_holdout_count_unaffected_by_shadow_score(tmp_path, monkeypatch):
    config, latest_taste, feedback_id = setup_taste_and_one_holdout_idea(tmp_path, monkeypatch)

    conn = db.connect(config.database_path)
    before = len(db.get_eligible_feedback_after(conn, latest_taste["checkpoint_feedback_id"]))
    conn.close()

    patch_shadow_pipeline(monkeypatch)
    cli.cmd_shadow_score(config)

    conn = db.connect(config.database_path)
    after = len(db.get_eligible_feedback_after(conn, latest_taste["checkpoint_feedback_id"]))
    conn.close()

    assert before == after == 1


def test_unscorable_source_still_counts_toward_holdout(tmp_path, monkeypatch):
    config, latest_taste, feedback_id = setup_taste_and_one_holdout_idea(
        tmp_path, monkeypatch, body_raw=UNSCORABLE_BODY
    )
    patch_shadow_pipeline(monkeypatch)
    cli.cmd_shadow_score(config)

    conn = db.connect(config.database_path)
    holdout = db.get_eligible_feedback_after(conn, latest_taste["checkpoint_feedback_id"])
    conn.close()

    assert len(holdout) == 1  # the UNSCORABLE_SOURCE judgment is still a real holdout judgment
    assert holdout[0]["feedback_id"] == feedback_id


def test_taste_status_holdout_wording_and_count_identical_with_or_without_shadow_score(tmp_path, monkeypatch, capsys):
    config, latest_taste, feedback_id = setup_taste_and_one_holdout_idea(tmp_path, monkeypatch)

    cli.cmd_taste_status(config)
    before_output = capsys.readouterr().out

    patch_shadow_pipeline(monkeypatch)
    cli.cmd_shadow_score(config)

    cli.cmd_taste_status(config)
    after_output = capsys.readouterr().out

    def holdout_line(output):
        return next(line for line in output.splitlines() if line.startswith("Holdout judgments collected"))

    assert holdout_line(before_output) == holdout_line(after_output) == f"Holdout judgments collected: 1 / {taste.HOLDOUT_SIZE}"


# --- fail-clearly / preconditions ----------------------------------------------


def test_shadow_score_fails_when_no_taste_version_exists(tmp_path, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)

    exit_code = cli.cmd_shadow_score(config)
    assert exit_code == 1
    assert "No taste model" in capsys.readouterr().out


def test_shadow_score_fails_clearly_when_screen_rules_file_missing(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    # Deliberately never write config.screen_rules_path.
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()
    build_taste_v1(config, monkeypatch)

    exit_code = cli.cmd_shadow_score(config)
    assert exit_code == 1
    output = capsys.readouterr().out
    assert "not found" in output
    assert str(config.screen_rules_path) in output


def test_shadow_score_fails_loudly_on_taste_integrity_mismatch(tmp_path, monkeypatch, capsys):
    config, latest_taste, feedback_id = setup_taste_and_one_holdout_idea(tmp_path, monkeypatch)

    from pathlib import Path
    Path(latest_taste["idea_taste_path"]).write_text("tampered", encoding="utf-8")

    patch_shadow_pipeline(monkeypatch)
    exit_code = cli.cmd_shadow_score(config)
    assert exit_code == 1
    assert "integrity" in capsys.readouterr().out.lower()

    conn = db.connect(config.database_path)
    assert db.get_idea_record_by_message_id(conn, "msg_hold_1") is None  # never even attempted
    conn.close()


def test_shadow_score_refuses_when_production_database_missing(tmp_path, monkeypatch):
    config = Config(
        agentmail_api_key=None, agentmail_inbox_id=None,
        database_path=tmp_path / "ideas.db",  # deliberately never created
        log_path=tmp_path / "app.log", anthropic_api_key="fake-anthropic-key",
        screen_rules_path=tmp_path / "IDEA_SCREEN_RULES.md",
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("the LLM must never be contacted when the DB is missing")

    monkeypatch.setattr(idea_extraction, "build_client", fail_if_called)
    monkeypatch.setattr(shadow, "build_client", fail_if_called)

    with pytest.raises(db.ProductionDatabaseMissingError):
        cli.cmd_shadow_score(config)


# --- shadow-status: aggregate only, never an individual prediction ------------


def test_shadow_status_no_taste_yet(tmp_path, capsys):
    config = make_config(tmp_path)
    exit_code = cli.cmd_shadow_status(config)
    assert exit_code == 0
    assert "No taste model" in capsys.readouterr().out


def test_shadow_status_reports_aggregate_counts_only(tmp_path, monkeypatch, capsys):
    config, latest_taste, feedback_id = setup_taste_and_one_holdout_idea(tmp_path, monkeypatch)
    patch_shadow_pipeline(monkeypatch)
    cli.cmd_shadow_score(config)

    exit_code = cli.cmd_shadow_status(config)
    assert exit_code == 0
    output = capsys.readouterr().out

    assert f"Taste version: {latest_taste['version_label']}" in output
    assert f"Holdout judgments collected: 1 / {taste.HOLDOUT_SIZE}" in output
    assert "Sources isolated: 1" in output
    assert "Unscorable sources: 0" in output
    assert "Shadow predictions generated: 1" in output
    # Never an individual idea's identity or prediction.
    assert "HOLD Corp" not in output
    assert "WATCH" not in output


def test_shadow_status_counts_unscorable_separately(tmp_path, monkeypatch, capsys):
    config, latest_taste, feedback_id = setup_taste_and_one_holdout_idea(
        tmp_path, monkeypatch, body_raw=UNSCORABLE_BODY
    )
    patch_shadow_pipeline(monkeypatch)
    cli.cmd_shadow_score(config)

    cli.cmd_shadow_status(config)
    output = capsys.readouterr().out
    assert "Sources isolated: 0" in output
    assert "Unscorable sources: 1" in output
    assert "Shadow predictions generated: 0" in output


# --- production-state / non-modification safety --------------------------------


def test_shadow_score_never_modifies_idea_screen_rules_file(tmp_path, monkeypatch):
    config, latest_taste, feedback_id = setup_taste_and_one_holdout_idea(tmp_path, monkeypatch)
    original_content = config.screen_rules_path.read_text(encoding="utf-8")
    original_mtime = config.screen_rules_path.stat().st_mtime

    patch_shadow_pipeline(monkeypatch)
    cli.cmd_shadow_score(config)

    assert config.screen_rules_path.read_text(encoding="utf-8") == original_content
    assert config.screen_rules_path.stat().st_mtime == original_mtime


def test_shadow_score_never_modifies_or_regenerates_taste_v1(tmp_path, monkeypatch):
    config, latest_taste, feedback_id = setup_taste_and_one_holdout_idea(tmp_path, monkeypatch)
    from pathlib import Path

    original_content = Path(latest_taste["idea_taste_path"]).read_text(encoding="utf-8")

    patch_shadow_pipeline(monkeypatch)
    cli.cmd_shadow_score(config)

    conn = db.connect(config.database_path)
    all_taste_versions = conn.execute("SELECT * FROM taste_versions").fetchall()
    conn.close()

    assert len(all_taste_versions) == 1
    assert dict(all_taste_versions[0]) == dict(latest_taste)
    assert Path(latest_taste["idea_taste_path"]).read_text(encoding="utf-8") == original_content


def test_shadow_generated_data_never_lands_under_real_production_state(tmp_path, monkeypatch):
    from pathlib import Path

    config, latest_taste, feedback_id = setup_taste_and_one_holdout_idea(tmp_path, monkeypatch)
    patch_shadow_pipeline(monkeypatch)
    cli.cmd_shadow_score(config)

    real_idea_scout_local = Path.home() / "IdeaScoutLocal"
    real_investment_brain = Path.home() / "Repos" / "InvestmentBrain"

    assert config.database_path.is_relative_to(tmp_path)
    assert config.screen_rules_path.is_relative_to(tmp_path)
    assert real_idea_scout_local not in config.database_path.parents
    assert real_investment_brain not in config.screen_rules_path.parents

    conn = db.connect(config.database_path)
    idea_record = db.get_idea_record_by_message_id(conn, "msg_hold_1")
    conn.close()
    assert Path(idea_record["source_isolated_at"] or "") != real_idea_scout_local  # sanity: no accidental path leak


def test_default_screen_rules_path_is_never_touched_by_make_config(tmp_path):
    """Guards the guard: make_config() must never accidentally fall back
    to Config's real default (Brad's actual InvestmentBrain repo).
    """
    from ideascout import config as config_module

    config = make_config(tmp_path)
    assert config.screen_rules_path != config_module.DEFAULT_SCREEN_RULES_PATH
    assert config.screen_rules_path.is_relative_to(tmp_path)
