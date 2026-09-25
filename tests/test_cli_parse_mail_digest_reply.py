"""Tests for digest-reply feedback (Stage 9).

A reply to a send-digest email is routed through the deterministic,
no-LLM ideascout/digest_reply.py parser instead of the LLM-based
direct-feedback parser in cmd_parse_mail. Everything it produces is
LEARNING ELIGIBLE (feedback_origin='DIGEST_REPLY', excluded_from_learning
stays 0) but structurally HOLDOUT INELIGIBLE (holdout_eligible=0) -- Brad
has already seen IdeaScout's own screen result before replying, so this
can never be a clean blind holdout judgment.

No real LLM call, no real AgentMail call, no production database --
everything lives under pytest's tmp_path, exactly like every other cli
test module.
"""

from __future__ import annotations

from ideascout import cli, db, parser, taste
from tests.test_cli_build_taste import insert_n_eligible, patch_taste
from tests.test_cli_parse_mail import get_status, make_config


def insert_source(conn, *, external_id: str, ticker: str | None, company: str | None) -> int:
    return db.insert_collected_source(
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


def insert_reply_message(
    conn,
    message_id: str,
    *,
    thread_id: str,
    subject: str = "Re: IdeaScout — New Ideas Worth Attention (2026-01-01)",
    body_raw: str = "LIKE\ngood idea",
    sender: str = "brad@example.com",
    sender_authenticated: str | None = "AUTHENTICATED",
) -> None:
    db.insert_message(
        conn,
        message_id=message_id,
        inbox_id="inbox_abc",
        thread_id=thread_id,
        received_at="2026-01-02T12:00:00+00:00",
        sender=sender,
        recipients="ideas@yourdomain.agentmail.to",
        subject=subject,
        body_raw=body_raw,
        body_format="text",
        size_bytes=100,
        stored_at="2026-01-02T12:00:05+00:00",
        sender_authenticated=sender_authenticated,
    )


def make_delivery(conn, *, thread_id: str, source_ids: list[int], provider_message_id="msg_sent_1") -> int:
    return db.record_digest_delivery(
        conn,
        provider_message_id=provider_message_id,
        provider_thread_id=thread_id,
        idempotency_key=f"idem-{thread_id}",
        subject="IdeaScout — New Ideas Worth Attention (2026-01-01)",
        sent_at="2026-01-01T09:00:00+00:00",
        taste_version=1,
        source_ids=source_ids,
    )


# --- single-item digest reply -------------------------------------------------


def test_single_item_reply_creates_digest_reply_feedback(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = insert_source(conn, external_id="1", ticker="ABC", company="Abc Corp")
    delivery_id = make_delivery(conn, thread_id="thread_reply_1", source_ids=[source_id])
    insert_reply_message(
        conn, "msg_reply_1", thread_id="thread_reply_1", body_raw="LIKE\nGreat margins, worth tracking."
    )
    conn.close()

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_reply_1") == "PARSED"
    events = db.get_feedback_events_for_message(conn, "msg_reply_1")
    assert len(events) == 1
    event = events[0]
    assert event["verdict"] == "LIKE"
    assert event["user_comment"] == "Great margins, worth tracking."
    assert event["feedback_origin"] == "DIGEST_REPLY"
    assert event["holdout_eligible"] == 0
    assert event["source_id"] == source_id
    assert event["digest_delivery_id"] == delivery_id
    assert event["excluded_from_learning"] == 0
    conn.close()


def test_single_item_reply_strong_pass_case_insensitive(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = insert_source(conn, external_id="1", ticker="ABC", company="Abc Corp")
    make_delivery(conn, thread_id="thread_reply_1", source_ids=[source_id])
    insert_reply_message(conn, "msg_reply_1", thread_id="thread_reply_1", body_raw="strong pass!\nnot for us")
    conn.close()

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    events = db.get_feedback_events_for_message(conn, "msg_reply_1")
    assert events[0]["verdict"] == "STRONG_PASS"
    conn.close()


def test_single_item_reply_quoted_digest_content_never_leaks_into_comment(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = insert_source(conn, external_id="1", ticker="ABC", company="Abc Corp")
    make_delivery(conn, thread_id="thread_reply_1", source_ids=[source_id])
    body = (
        "MAYBE\n\nNeed more data on the pipeline.\n\n"
        "On Thu, Jan 1, 2026 at 9:00 AM IdeaScout <ideas@example.com> wrote:\n"
        "> DISTINCTIVE_DIGEST_ONLY_PHRASE screen result: WATCH, mispricing: plausible\n"
    )
    insert_reply_message(conn, "msg_reply_1", thread_id="thread_reply_1", body_raw=body)
    conn.close()

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    events = db.get_feedback_events_for_message(conn, "msg_reply_1")
    assert "DISTINCTIVE_DIGEST_ONLY_PHRASE" not in events[0]["user_comment"]
    assert events[0]["verdict"] == "MAYBE"
    conn.close()


def test_single_item_reply_ambiguous_body_fails_closed_to_needs_review(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = insert_source(conn, external_id="1", ticker="ABC", company="Abc Corp")
    make_delivery(conn, thread_id="thread_reply_1", source_ids=[source_id])
    insert_reply_message(
        conn, "msg_reply_1", thread_id="thread_reply_1", body_raw="Not sure yet, will look at this later."
    )
    conn.close()

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0  # a fail-closed NEEDS_REVIEW is not itself an error

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_reply_1") == "NEEDS_REVIEW"
    assert db.get_feedback_events_for_message(conn, "msg_reply_1") == []
    row = db.get_message(conn, "msg_reply_1")
    assert "DIGEST_REPLY_PARSE_FAILED" in row["error"]
    conn.close()


# --- multi-item digest reply ---------------------------------------------------


def test_multi_item_reply_creates_feedback_only_for_mentioned_ideas(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_ids = [
        insert_source(conn, external_id=str(i), ticker=t, company=f"{t} Inc")
        for i, t in enumerate(["AAA", "BBB", "CCC", "DDD", "EEE"])
    ]
    delivery_id = make_delivery(conn, thread_id="thread_reply_multi", source_ids=source_ids)
    body = "AAA - LIKE\nGood moat, revisit next quarter.\n\nCCC: PASS\nToo levered for our taste.\n"
    insert_reply_message(conn, "msg_reply_multi", thread_id="thread_reply_multi", body_raw=body)
    conn.close()

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_reply_multi") == "PARSED"
    events = db.get_feedback_events_for_message(conn, "msg_reply_multi")
    assert len(events) == 2
    by_ticker = {e["ticker"]: e for e in events}
    assert by_ticker["AAA"]["verdict"] == "LIKE"
    assert by_ticker["AAA"]["user_comment"] == "Good moat, revisit next quarter."
    assert by_ticker["AAA"]["source_id"] == source_ids[0]
    assert by_ticker["AAA"]["digest_delivery_id"] == delivery_id
    assert by_ticker["CCC"]["verdict"] == "PASS"
    assert by_ticker["CCC"]["source_id"] == source_ids[2]
    for e in events:
        assert e["feedback_origin"] == "DIGEST_REPLY"
        assert e["holdout_eligible"] == 0
    # BBB, DDD, EEE were never mentioned -- no feedback manufactured for them.
    assert "BBB" not in by_ticker
    assert "DDD" not in by_ticker
    assert "EEE" not in by_ticker
    conn.close()


def test_multi_item_reply_ambiguous_ticker_fails_whole_message_closed(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_ids = [
        insert_source(conn, external_id="0", ticker="AAA", company="Aaa Inc"),
        insert_source(conn, external_id="1", ticker="AAA", company="Aaa Two Inc"),
    ]
    make_delivery(conn, thread_id="thread_reply_multi", source_ids=source_ids)
    insert_reply_message(
        conn, "msg_reply_multi", thread_id="thread_reply_multi", body_raw="AAA - LIKE\nsome comment"
    )
    conn.close()

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_reply_multi") == "NEEDS_REVIEW"
    assert db.get_feedback_events_for_message(conn, "msg_reply_multi") == []
    conn.close()


def test_parse_mail_rerun_is_idempotent_for_digest_reply(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = insert_source(conn, external_id="1", ticker="ABC", company="Abc Corp")
    make_delivery(conn, thread_id="thread_reply_1", source_ids=[source_id])
    insert_reply_message(conn, "msg_reply_1", thread_id="thread_reply_1", body_raw="LIKE\ngood")
    conn.close()

    first = cli.cmd_parse_mail(config)
    second = cli.cmd_parse_mail(config)
    assert first == 0
    assert second == 0

    conn = db.connect(config.database_path)
    count = conn.execute("SELECT COUNT(*) FROM feedback WHERE message_id = 'msg_reply_1'").fetchone()[0]
    assert count == 1
    conn.close()


# --- safety: unmatched digest-looking reply ------------------------------------


def test_digest_looking_subject_with_no_delivery_match_fails_closed(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    # No digest_delivery row exists for this thread_id at all.
    insert_reply_message(
        conn,
        "msg_reply_unmatched",
        thread_id="thread_never_delivered",
        subject="Re: IdeaScout — New Ideas Worth Attention (2026-01-01)",
        body_raw="LIKE\ngood",
    )
    conn.close()

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_reply_unmatched") == "NEEDS_REVIEW"
    assert db.get_feedback_events_for_message(conn, "msg_reply_unmatched") == []
    row = db.get_message(conn, "msg_reply_unmatched")
    assert "UNMATCHED_DIGEST_REPLY" in row["error"]
    conn.close()


def test_unmatched_digest_reply_is_never_processed_as_ordinary_direct_feedback(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_reply_message(
        conn,
        "msg_reply_unmatched",
        thread_id="thread_never_delivered",
        subject="Re: IdeaScout — New Ideas Worth Attention (2026-01-01)",
        body_raw="LIKE\ngood",
    )
    conn.close()

    calls = []

    def fail_if_called(client, model, subject, body):
        calls.append(1)
        raise AssertionError("must never call the LLM for an unmatched digest-looking reply")

    monkeypatch.setattr(parser, "build_client", lambda api_key: object())
    monkeypatch.setattr(parser, "parse_message", fail_if_called)

    cli.cmd_parse_mail(config)
    assert calls == []


def test_non_brad_or_unauthenticated_digest_reply_thread_still_rejected(tmp_path):
    """Sender/authentication gates run before digest-reply routing --
    unchanged from the existing direct-feedback behavior.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = insert_source(conn, external_id="1", ticker="ABC", company="Abc Corp")
    make_delivery(conn, thread_id="thread_reply_1", source_ids=[source_id])
    insert_reply_message(
        conn,
        "msg_reply_1",
        thread_id="thread_reply_1",
        sender="brad@example.com",
        sender_authenticated="UNAUTHENTICATED",
        body_raw="LIKE\ngood",
    )
    conn.close()

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_reply_1") == "SKIPPED_UNAUTHENTICATED"
    assert db.get_feedback_events_for_message(conn, "msg_reply_1") == []
    conn.close()


# --- holdout interaction --------------------------------------------------------


def test_digest_reply_feedback_never_increases_holdout_count(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()
    patch_taste(monkeypatch)
    assert cli.cmd_build_taste(config) == 0

    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    assert latest["training_count"] == 15
    holdout_before = db.get_eligible_feedback_after(conn, latest["checkpoint_feedback_id"])
    assert len(holdout_before) == 0

    source_id = insert_source(conn, external_id="1", ticker="ABC", company="Abc Corp")
    make_delivery(conn, thread_id="thread_reply_1", source_ids=[source_id])
    insert_reply_message(conn, "msg_reply_1", thread_id="thread_reply_1", body_raw="LIKE\ngood")
    conn.close()

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    holdout_after = db.get_eligible_feedback_after(conn, latest["checkpoint_feedback_id"])
    assert len(holdout_after) == 0  # DIGEST_REPLY feedback never counts as holdout

    learning_eligible = db.get_feedback_eligible_for_learning(conn)
    digest_reply_ids = [row["feedback_id"] for row in learning_eligible if row["feedback_origin"] == "DIGEST_REPLY"]
    assert len(digest_reply_ids) == 1  # but it IS learning-eligible
    conn.close()


def test_direct_eligible_feedback_after_digest_reply_still_increments_holdout(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()
    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)

    source_id = insert_source(conn, external_id="1", ticker="ABC", company="Abc Corp")
    make_delivery(conn, thread_id="thread_reply_1", source_ids=[source_id])
    insert_reply_message(conn, "msg_reply_1", thread_id="thread_reply_1", body_raw="LIKE\ngood")
    conn.close()
    cli.cmd_parse_mail(config)

    # A genuine DIRECT judgment after the digest reply.
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 1, prefix="msg_direct_holdout")
    holdout = db.get_eligible_feedback_after(conn, latest["checkpoint_feedback_id"])
    conn.close()

    assert len(holdout) == 1  # only the DIRECT judgment counts


def test_shadow_score_ignores_digest_reply_feedback(tmp_path, monkeypatch):
    from ideascout import shadow

    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()
    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    source_id = insert_source(conn, external_id="1", ticker="ABC", company="Abc Corp")
    make_delivery(conn, thread_id="thread_reply_1", source_ids=[source_id])
    insert_reply_message(conn, "msg_reply_1", thread_id="thread_reply_1", body_raw="LIKE\ngood")
    conn.close()
    cli.cmd_parse_mail(config)

    calls = []
    monkeypatch.setattr(shadow, "build_client", lambda api_key: object())

    def fail_if_called(*args, **kwargs):
        calls.append(1)
        raise AssertionError("shadow-score must never process DIGEST_REPLY feedback as a holdout case")

    monkeypatch.setattr(source_isolation_module(), "isolate_source", fail_if_called)

    exit_code = cli.cmd_shadow_score(config)
    assert exit_code == 0
    assert calls == []


def source_isolation_module():
    from ideascout import source_isolation

    return source_isolation


def test_build_taste_after_holdout_completion_includes_digest_reply_feedback(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()
    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    conn = db.connect(config.database_path)
    source_id = insert_source(conn, external_id="1", ticker="ABC", company="Abc Corp")
    make_delivery(conn, thread_id="thread_reply_1", source_ids=[source_id])
    insert_reply_message(conn, "msg_reply_1", thread_id="thread_reply_1", body_raw="LIKE\ngood")
    conn.close()
    cli.cmd_parse_mail(config)

    conn = db.connect(config.database_path)
    insert_n_eligible(conn, taste.HOLDOUT_SIZE, prefix="msg_holdout")
    conn.close()

    exit_code = cli.cmd_build_taste(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    training_ids = __import__("json").loads(latest["training_feedback_ids_json"])
    digest_reply_rows = conn.execute(
        "SELECT feedback_id FROM feedback WHERE feedback_origin = 'DIGEST_REPLY'"
    ).fetchall()
    conn.close()

    assert len(digest_reply_rows) == 1
    assert digest_reply_rows[0]["feedback_id"] in training_ids


# --- show-feedback display ------------------------------------------------------


def test_show_feedback_displays_digest_reply_origin_and_holdout_ineligibility(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = insert_source(conn, external_id="1", ticker="ABC", company="Abc Corp")
    make_delivery(conn, thread_id="thread_reply_1", source_ids=[source_id])
    insert_reply_message(conn, "msg_reply_1", thread_id="thread_reply_1", body_raw="LIKE\nGood margins.")
    conn.close()

    cli.cmd_parse_mail(config)
    capsys.readouterr()

    exit_code = cli.cmd_show_feedback(config)
    assert exit_code == 0

    output = capsys.readouterr().out
    assert "Origin: DIGEST_REPLY" in output
    assert "Holdout eligible: NO" in output
    assert "yellowbrick/ABC" in output


def test_show_feedback_direct_feedback_shows_no_origin_line(tmp_path, capsys):
    """A normal DIRECT feedback record must NOT gain a new Origin/Holdout
    line -- that display is added only for DIGEST_REPLY rows.
    """
    from tests.test_cli_parse_mail import insert_raw_message as insert_direct_raw_message

    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_direct_raw_message(conn, "msg_direct_1")
    conn.close()

    conn = db.connect(config.database_path)
    db.insert_feedback(
        conn,
        message_id="msg_direct_1",
        event_type="FEEDBACK",
        verdict="LIKE",
        ticker="XYZ",
        company=None,
        novelty="UNKNOWN",
        user_comment="Looks interesting.",
        parsed_json="{}",
        parser_version=parser.PARSER_VERSION,
        model_name="claude-haiku-4-5",
        confidence=0.9,
        created_at="2026-01-01T00:00:00+00:00",
    )
    conn.close()

    exit_code = cli.cmd_show_feedback(config)
    assert exit_code == 0

    output = capsys.readouterr().out
    assert "Origin: DIGEST_REPLY" not in output


# --- Stage 9 regression: current v2-style holdout state must stay clean -------


def test_taste_status_unaffected_by_any_number_of_digest_replies(tmp_path, monkeypatch, capsys):
    """Direct statement of task requirement 9: after ingesting ANY number
    of DIGEST_REPLY feedback records, taste-status must report an
    IDENTICAL state to before -- until Brad supplies genuine clean DIRECT
    judgments.
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
    assert "Holdout judgments collected: 0 / 20" in before
    assert "Taste frozen: YES" in before

    conn = db.connect(config.database_path)
    for i in range(5):
        source_id = insert_source(conn, external_id=str(i), ticker=f"DR{i}", company=f"DR{i} Inc")
        make_delivery(
            conn, thread_id=f"thread_reply_{i}", source_ids=[source_id], provider_message_id=f"msg_sent_{i}"
        )
        insert_reply_message(conn, f"msg_reply_{i}", thread_id=f"thread_reply_{i}", body_raw="LIKE\ngood")
    conn.close()
    cli.cmd_parse_mail(config)

    capsys.readouterr()
    cli.cmd_taste_status(config)
    after = capsys.readouterr().out
    assert "Holdout judgments collected: 0 / 20" in after
    assert "Taste frozen: YES" in after
    assert "Training judgments: 15" in after
    assert "Digest-reply feedback records: 5" in after
