"""Tests for BLIND_REVIEW feedback (Stage 11): submitting judgments on
already-assigned blind-review items via the "IdeaScout Blind Review"
email route, and the resulting holdout-count/regression behavior.

Unlike DIGEST_REPLY/SCREEN_REVIEW, a successfully parsed BLIND_REVIEW
judgment IS meant to be a clean holdout judgment (holdout_eligible=1),
counting toward db.get_eligible_feedback_after -- this file's HOLDOUT
section is the explicit regression the task requires.

No real LLM call, no real AgentMail call, no production database --
everything lives under pytest's tmp_path.
"""

from __future__ import annotations

import dataclasses

from ideascout import blind_review, cli, db, parser, taste
from tests.test_cli_build_taste import make_config as _base_make_config
from tests.test_cli_build_taste import insert_n_eligible, patch_taste
from tests.test_cli_parse_mail import get_status


def make_config(tmp_path):
    config = _base_make_config(tmp_path)
    return dataclasses.replace(config, brad_allowed_senders=frozenset({"brad@example.com"}))


def insert_extracted_source(conn, *, external_id: str, ticker: str = "ABC", company: str = "Abc Corp") -> int:
    source_id = db.insert_collected_source(
        conn,
        source_name="yellowbrick",
        external_id=external_id,
        canonical_url=f"https://www.joinyellowbrick.com/sp/{external_id}",
        discovered_at="2026-01-01T00:00:00+00:00",
        discovery_title=company,
        source_date=None,
        source_title=company,
        author=None,
        ticker=ticker,
        company=company,
        source_type="stock_pitch",
        content_hash=f"hash-{external_id}",
        raw_html_path=f"/x/{external_id}.html",
        metadata_json="{}",
        created_at="2026-01-01T00:00:00+00:00",
    )
    db.update_collected_source_extracted(
        conn,
        source_id=source_id,
        company=company,
        ticker=ticker,
        source_title=company,
        source_date=None,
        business_summary="x",
        core_thesis="x",
        why_mispriced="x",
        future_earnings_change="x",
        upside_case="x",
        downside_or_key_risks="x",
        catalysts="x",
        what_must_be_true="x",
        evidence_of_market_misunderstanding="x",
        known_unknowns="x",
        extraction_model_name="fake-model",
        extracted_at="2026-01-01T00:00:00+00:00",
    )
    return source_id


def assign_blind_review(conn, source_id: int, taste_version_at_assignment: int | None = None) -> int:
    return db.record_blind_review_assignment(
        conn,
        source_id=source_id,
        assigned_at="2026-01-01T00:00:00+00:00",
        taste_version_at_assignment=taste_version_at_assignment,
    )


def insert_blind_review_message(
    conn,
    message_id: str,
    *,
    subject: str = "IdeaScout Blind Review",
    body_raw: str = "BR-1 - LIKE\ngood",
    sender: str = "brad@example.com",
    sender_authenticated: str | None = "AUTHENTICATED",
    received_at: str = "2026-01-02T12:00:00+00:00",
) -> None:
    db.insert_message(
        conn,
        message_id=message_id,
        inbox_id="inbox_abc",
        thread_id="thread_blind",
        received_at=received_at,
        sender=sender,
        recipients="ideas@yourdomain.agentmail.to",
        subject=subject,
        body_raw=body_raw,
        body_format="text",
        size_bytes=100,
        stored_at="2026-01-02T12:00:05+00:00",
        sender_authenticated=sender_authenticated,
    )


# --- routing / single-item judgment ---------------------------------------------


def test_exact_subject_routes_to_blind_review_parser(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = insert_extracted_source(conn, external_id="1")
    assign_blind_review(conn, source_id)
    insert_blind_review_message(conn, "msg_1", body_raw="BR-1 - LIKE\ngood margins")
    conn.close()

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "PARSED"
    events = db.get_feedback_events_for_message(conn, "msg_1")
    assert len(events) == 1
    assert events[0]["feedback_origin"] == "BLIND_REVIEW"
    conn.close()


def test_re_and_fwd_subject_prefixes_are_accepted(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = insert_extracted_source(conn, external_id="1")
    assign_blind_review(conn, source_id)
    insert_blind_review_message(conn, "msg_1", subject="Re: IdeaScout Blind Review", body_raw="BR-1 - LIKE\ngood")
    conn.close()

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "PARSED"
    conn.close()


def test_llm_is_never_called_for_blind_review_message(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = insert_extracted_source(conn, external_id="1")
    assign_blind_review(conn, source_id)
    insert_blind_review_message(conn, "msg_1", body_raw="BR-1 - LIKE\ngood")
    conn.close()

    calls = []

    def fail_if_called(client, model, subject, body):
        calls.append(1)
        raise AssertionError("must never call the LLM for a blind-review message")

    monkeypatch.setattr(parser, "build_client", lambda api_key: object())
    monkeypatch.setattr(parser, "parse_message", fail_if_called)

    cli.cmd_parse_mail(config)
    assert calls == []


def test_prediction_never_revealed_even_on_parse_failure(tmp_path, monkeypatch, capsys):
    """CRITICAL: not just the assignment-display path, but the parse-mail
    path too, must never print/log v2's prediction for an assignment,
    even when parsing fails.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = insert_extracted_source(conn, external_id="1")
    assign_blind_review(conn, source_id)
    db.insert_source_screening(
        conn, source_id=source_id, created_at="2026-01-01T00:00:00+00:00", taste_version=2,
        taste_sha256="t", screen_rules_path="IDEA_SCREEN_RULES.md", screen_rules_sha256="r",
        content_hash="hash-1", model_name="fake-model", overall_prediction="INVESTIGATE_NOW",
        mispricing="Plausible", variant_perception="Plausible", upside="Potentially sufficient",
        business_quality="Plausible", downside="Acceptable", key_reasons_json="[]",
        key_concerns_json="[]", critical_questions_json="[]", confidence="HIGH",
    )
    insert_blind_review_message(conn, "msg_1", body_raw="not a valid header at all")
    conn.close()

    cli.cmd_parse_mail(config)
    output = capsys.readouterr().out
    assert "INVESTIGATE_NOW" not in output


# --- multi-item / subset ---------------------------------------------------------


def test_multi_item_response_creates_feedback_only_for_judged_items(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    assignment_ids = []
    for i in range(5):
        source_id = insert_extracted_source(conn, external_id=str(i), ticker=f"T{i}")
        assignment_ids.append(assign_blind_review(conn, source_id))
    ref_a = blind_review.format_review_ref(assignment_ids[0])
    ref_c = blind_review.format_review_ref(assignment_ids[2])
    insert_blind_review_message(
        conn, "msg_1", body_raw=f"{ref_a} — LIKE\nGood setup.\n\n{ref_c} — PASS\nNo mispricing.\n"
    )
    conn.close()

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    events = db.get_feedback_events_for_message(conn, "msg_1")
    assert len(events) == 2
    by_assignment = {e["blind_review_assignment_id"]: e for e in events}
    assert by_assignment[assignment_ids[0]]["verdict"] == "LIKE"
    assert by_assignment[assignment_ids[0]]["user_comment"] == "Good setup."
    assert by_assignment[assignment_ids[2]]["verdict"] == "PASS"
    assert by_assignment[assignment_ids[2]]["user_comment"] == "No mispricing."
    assert assignment_ids[1] not in by_assignment  # omitted -- no feedback manufactured
    conn.close()


def test_comment_preserved_verbatim(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = insert_extracted_source(conn, external_id="1")
    assign_blind_review(conn, source_id)
    body = "BR-1 — MAYBE\nLine one.\nLine two, with punctuation!\n\nLine four after a blank line."
    insert_blind_review_message(conn, "msg_1", body_raw=body)
    conn.close()

    cli.cmd_parse_mail(config)

    conn = db.connect(config.database_path)
    events = db.get_feedback_events_for_message(conn, "msg_1")
    assert events[0]["user_comment"] == "Line one.\nLine two, with punctuation!\n\nLine four after a blank line."
    conn.close()


# --- fail-closed cases ------------------------------------------------------------


def test_unknown_ref_fails_closed_to_needs_review(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_blind_review_message(conn, "msg_1", body_raw="BR-999999 - LIKE\nunknown\n")
    conn.close()

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "NEEDS_REVIEW"
    assert db.get_feedback_events_for_message(conn, "msg_1") == []
    row = db.get_message(conn, "msg_1")
    assert "BLIND_REVIEW_PARSE_FAILED" in row["error"]
    conn.close()


def test_duplicate_judgment_fails_closed(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = insert_extracted_source(conn, external_id="1")
    assign_blind_review(conn, source_id)
    insert_blind_review_message(conn, "msg_1", body_raw="BR-1 - LIKE\nfirst opinion")
    conn.close()
    cli.cmd_parse_mail(config)

    conn = db.connect(config.database_path)
    insert_blind_review_message(conn, "msg_2", body_raw="BR-1 - PASS\nsecond, independent opinion")
    conn.close()
    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "PARSED"
    assert get_status(conn, "msg_2") == "NEEDS_REVIEW"
    row = db.get_message(conn, "msg_2")
    assert "DUPLICATE_BLIND_REVIEW" in row["error"]
    count = conn.execute(
        "SELECT COUNT(*) FROM feedback WHERE blind_review_assignment_id = 1"
    ).fetchone()[0]
    assert count == 1
    conn.close()


def test_parse_mail_rerun_is_idempotent(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = insert_extracted_source(conn, external_id="1")
    assign_blind_review(conn, source_id)
    insert_blind_review_message(conn, "msg_1", body_raw="BR-1 - LIKE\ngood")
    conn.close()

    first = cli.cmd_parse_mail(config)
    second = cli.cmd_parse_mail(config)
    assert first == 0
    assert second == 0

    conn = db.connect(config.database_path)
    count = conn.execute("SELECT COUNT(*) FROM feedback WHERE message_id = 'msg_1'").fetchone()[0]
    assert count == 1
    conn.close()


# --- security / auth --------------------------------------------------------------


def test_non_brad_sender_never_processed(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = insert_extracted_source(conn, external_id="1")
    assign_blind_review(conn, source_id)
    insert_blind_review_message(conn, "msg_1", sender="newsletter@example.com", body_raw="BR-1 - LIKE\ngood")
    conn.close()

    calls = []
    monkeypatch.setattr(parser, "build_client", lambda api_key: object())
    monkeypatch.setattr(parser, "parse_message", lambda *a, **k: calls.append(1))

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0
    assert calls == []

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "SKIPPED_NOT_BRAD"
    assert db.get_feedback_events_for_message(conn, "msg_1") == []
    conn.close()


def test_unauthenticated_sender_never_processed(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = insert_extracted_source(conn, external_id="1")
    assign_blind_review(conn, source_id)
    insert_blind_review_message(
        conn, "msg_1", sender_authenticated="UNAUTHENTICATED", body_raw="BR-1 - LIKE\ngood"
    )
    conn.close()

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "SKIPPED_UNAUTHENTICATED"
    assert db.get_feedback_events_for_message(conn, "msg_1") == []
    conn.close()


# --- feedback semantics: learning + holdout eligible ------------------------------


def test_blind_review_feedback_is_learning_and_holdout_eligible(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = insert_extracted_source(conn, external_id="1", ticker="ABC", company="Abc Corp")
    assignment_id = assign_blind_review(conn, source_id)
    insert_blind_review_message(conn, "msg_1", body_raw="BR-1 - LIKE\ngood margins")
    conn.close()

    cli.cmd_parse_mail(config)

    conn = db.connect(config.database_path)
    events = db.get_feedback_events_for_message(conn, "msg_1")
    event = events[0]
    assert event["feedback_origin"] == "BLIND_REVIEW"
    assert event["holdout_eligible"] == 1
    assert event["excluded_from_learning"] == 0
    assert event["source_id"] == source_id
    assert event["blind_review_assignment_id"] == assignment_id
    assert event["ticker"] == "ABC"
    assert event["company"] == "Abc Corp"

    eligible = db.get_feedback_eligible_for_learning(conn)
    assert any(r["feedback_id"] == event["feedback_id"] for r in eligible)
    conn.close()


# --- holdout count: THE explicit regression required by the task ------------------


def test_holdout_count_increments_by_exact_number_of_blind_review_judgments(tmp_path, monkeypatch, capsys):
    """Starting: Training judgments: 15, Holdout: 0/20, frozen: YES.
    After 4 successful BLIND_REVIEW judgments: Training judgments: 15,
    Holdout: 4/20, frozen: YES (still, since 4 < 20).
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()
    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    capsys.readouterr()
    cli.cmd_taste_status(config)
    before = capsys.readouterr().out
    assert "Training judgments: 15" in before
    assert "Holdout judgments collected: 0 / 20" in before
    assert "Taste frozen: YES" in before

    conn = db.connect(config.database_path)
    for i in range(4):
        source_id = insert_extracted_source(conn, external_id=f"br{i}", ticker=f"BR{i}")
        assignment_id = assign_blind_review(conn, source_id)
        ref = blind_review.format_review_ref(assignment_id)
        insert_blind_review_message(conn, f"msg_blind_{i}", body_raw=f"{ref} - LIKE\njudgment {i}")
    conn.close()
    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    capsys.readouterr()
    cli.cmd_taste_status(config)
    after = capsys.readouterr().out
    assert "Training judgments: 15" in after
    assert "Holdout judgments collected: 4 / 20" in after
    assert "Taste frozen: YES" in after


def test_holdout_count_reaches_full_but_taste_stays_frozen_and_protected(tmp_path, monkeypatch, capsys):
    """After 20 BLIND_REVIEW judgments: Holdout 20/20 and Holdout complete
    YES, but HOLDOUT COMPLETE is NOT the same condition as TASTE BUILD
    UNLOCKED (see db.py's Migration 14 comment) -- Taste v2 stays frozen
    and protected, build-taste still refuses to run, and nothing
    automatically regenerates it. Only an explicit future evaluation/
    unlock action (not built yet) can change that.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()
    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    conn = db.connect(config.database_path)
    latest_before = db.get_latest_taste_version(conn)
    for i in range(taste.HOLDOUT_SIZE):
        source_id = insert_extracted_source(conn, external_id=f"br{i}", ticker=f"BR{i}")
        assignment_id = assign_blind_review(conn, source_id)
        ref = blind_review.format_review_ref(assignment_id)
        insert_blind_review_message(conn, f"msg_blind_{i}", body_raw=f"{ref} - LIKE\njudgment {i}")
    conn.close()
    cli.cmd_parse_mail(config)

    capsys.readouterr()
    cli.cmd_taste_status(config)
    output = capsys.readouterr().out
    assert "Holdout judgments collected: 20 / 20" in output
    assert "Holdout complete: YES" in output
    assert "Taste frozen: YES" in output
    assert "Awaiting holdout evaluation: YES" in output

    # build-taste must still refuse -- 20/20 alone never unlocks it.
    exit_code = cli.cmd_build_taste(config)
    assert exit_code == 1

    conn = db.connect(config.database_path)
    latest_after = db.get_latest_taste_version(conn)
    conn.close()
    # Nothing regenerated it -- still the exact same version, same training count.
    assert latest_after["version_number"] == latest_before["version_number"]
    assert latest_after["training_count"] == latest_before["training_count"]


# --- regression: DIRECT / DIGEST_REPLY / SCREEN_REVIEW unaffected ----------------


def test_direct_feedback_parsing_unchanged(tmp_path, monkeypatch):
    from tests.test_cli_parse_mail import insert_raw_message as insert_direct_message
    from tests.test_cli_parse_mail import make_result, patch_parser

    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_direct_message(conn, "msg_direct_1")
    conn.close()

    patch_parser(monkeypatch, make_result("FEEDBACK", "LIKE"))
    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_direct_1") == "PARSED"
    events = db.get_feedback_events_for_message(conn, "msg_direct_1")
    assert events[0]["feedback_origin"] == "DIRECT"
    assert events[0]["holdout_eligible"] == 1
    conn.close()


def test_digest_reply_parsing_unchanged(tmp_path):
    from ideascout import digest_reply

    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = insert_extracted_source(conn, external_id="1", ticker="XYZ", company="Xyz Corp")
    delivery_id = db.record_digest_delivery(
        conn, provider_message_id="msg_sent_1", provider_thread_id="thread_digest",
        idempotency_key="idem-1", subject="IdeaScout — New Ideas Worth Attention",
        sent_at="2026-01-01T09:00:00+00:00", taste_version=1, source_ids=[source_id],
    )
    db.insert_message(
        conn, message_id="msg_reply_1", inbox_id="inbox_abc", thread_id="thread_digest",
        received_at="2026-01-02T00:00:00+00:00", sender="brad@example.com",
        recipients="ideas@yourdomain.agentmail.to",
        subject="Re: IdeaScout — New Ideas Worth Attention",
        body_raw="LIKE\ngood", body_format="text", size_bytes=10,
        stored_at="2026-01-02T00:00:05+00:00", sender_authenticated="AUTHENTICATED",
    )
    conn.close()

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    events = db.get_feedback_events_for_message(conn, "msg_reply_1")
    assert events[0]["feedback_origin"] == "DIGEST_REPLY"
    assert events[0]["holdout_eligible"] == 0
    assert events[0]["digest_delivery_id"] == delivery_id
    conn.close()


def test_screen_review_parsing_unchanged(tmp_path):
    from ideascout import screen_review

    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = insert_extracted_source(conn, external_id="1", ticker="ABC", company="Abc Corp")
    screening_id = db.insert_source_screening(
        conn, source_id=source_id, created_at="2026-01-01T00:00:00+00:00", taste_version=1,
        taste_sha256="t", screen_rules_path="IDEA_SCREEN_RULES.md", screen_rules_sha256="r",
        content_hash="hash-1", model_name="fake-model", overall_prediction="PASS",
        mispricing="Plausible", variant_perception="Plausible", upside="Potentially sufficient",
        business_quality="Plausible", downside="Acceptable", key_reasons_json="[]",
        key_concerns_json="[]", critical_questions_json="[]", confidence="MEDIUM",
    )
    ref = screen_review.format_review_ref(screening_id)
    db.insert_message(
        conn, message_id="msg_sr_1", inbox_id="inbox_abc", thread_id="thread_sr",
        received_at="2026-01-02T00:00:00+00:00", sender="brad@example.com",
        recipients="ideas@yourdomain.agentmail.to", subject="IdeaScout Screen Review",
        body_raw=f"{ref} - LIKE\ndisagree", body_format="text", size_bytes=10,
        stored_at="2026-01-02T00:00:05+00:00", sender_authenticated="AUTHENTICATED",
    )
    conn.close()

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    events = db.get_feedback_events_for_message(conn, "msg_sr_1")
    assert events[0]["feedback_origin"] == "SCREEN_REVIEW"
    assert events[0]["holdout_eligible"] == 0
    assert events[0]["screening_id"] == screening_id
    conn.close()
