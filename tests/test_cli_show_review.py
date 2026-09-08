"""Tests for show-review / approve-review (human-review workflow).

Both commands are pure database operations, so these tests seed
messages_raw/feedback directly at whatever state is needed and check what
the commands do to them. Nothing here calls a real LLM or AgentMail.
"""

from ideascout import cli, db, parser
from ideascout.config import Config


def make_config(tmp_path) -> Config:
    config = Config(
        agentmail_api_key=None,
        agentmail_inbox_id=None,
        database_path=tmp_path / "ideas.db",
        log_path=tmp_path / "app.log",
    )
    db.connect(config.database_path).close()
    return config


def insert_raw_message(
    conn,
    message_id: str,
    subject: str = "An idea",
    body_raw: str = "MAYBE, not sure.",
    sender: str = "brad@example.com",
) -> None:
    db.insert_message(
        conn,
        message_id=message_id,
        inbox_id="inbox_abc",
        thread_id="thread_abc",
        received_at="2026-01-01T12:00:00+00:00",
        sender=sender,
        recipients="ideas@yourdomain.agentmail.to",
        subject=subject,
        body_raw=body_raw,
        body_format="text",
        size_bytes=100,
        stored_at="2026-01-01T12:00:05+00:00",
        sender_authenticated="AUTHENTICATED",
    )


def insert_feedback_row(
    conn,
    message_id: str,
    event_index: int = 0,
    event_type: str = "UNCLEAR",
    verdict=None,
    ticker=None,
    company=None,
    confidence: float = 0.2,
    user_comment: str = "Not sure what Brad meant.",
) -> None:
    db.insert_feedback(
        conn,
        message_id=message_id,
        event_index=event_index,
        event_type=event_type,
        verdict=verdict,
        ticker=ticker,
        company=company,
        novelty=None,
        user_comment=user_comment,
        parsed_json="{}",
        parser_version=parser.PARSER_VERSION,
        model_name="claude-haiku-4-5",
        confidence=confidence,
        created_at="2026-01-01T00:00:00+00:00",
    )


def make_needs_review_message(conn, message_id: str, **feedback_kwargs) -> None:
    insert_raw_message(conn, message_id)
    insert_feedback_row(conn, message_id, **feedback_kwargs)
    db.set_feedback_parse_status(conn, message_id, "NEEDS_REVIEW")


# --- show-review -------------------------------------------------------------


def test_show_review_displays_needs_review_messages_with_events(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    make_needs_review_message(
        conn,
        "msg_1",
        ticker="XYZ",
        confidence=0.3,
        user_comment="Hidden asset is interesting but I'm not sure about the verdict.",
    )
    conn.close()

    exit_code = cli.cmd_show_review(config)
    assert exit_code == 0

    output = capsys.readouterr().out
    assert "msg_1" in output
    assert "XYZ" in output
    assert "UNCLEAR" in output
    assert "0.30" in output
    assert "Hidden asset is interesting" in output
    assert "NOT YET APPROVED" in output


def test_show_review_shows_multiple_events_for_one_message(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1")
    insert_feedback_row(conn, "msg_1", event_index=0, ticker="AAA")
    insert_feedback_row(conn, "msg_1", event_index=1, ticker="BBB")
    db.set_feedback_parse_status(conn, "msg_1", "NEEDS_REVIEW")
    conn.close()

    exit_code = cli.cmd_show_review(config)
    assert exit_code == 0

    output = capsys.readouterr().out
    assert "AAA" in output
    assert "BBB" in output


def test_show_review_ignores_other_statuses(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    make_needs_review_message(conn, "msg_review")
    insert_raw_message(conn, "msg_parsed")
    insert_feedback_row(conn, "msg_parsed", event_type="FEEDBACK", verdict="LIKE", ticker="ZZZ")
    db.set_feedback_parse_status(conn, "msg_parsed", "PARSED")
    conn.close()

    exit_code = cli.cmd_show_review(config)
    assert exit_code == 0

    output = capsys.readouterr().out
    assert "msg_review" in output
    assert "msg_parsed" not in output
    assert "ZZZ" not in output


def test_show_review_with_nothing_pending_is_not_an_error(tmp_path, capsys):
    config = make_config(tmp_path)
    exit_code = cli.cmd_show_review(config)
    assert exit_code == 0
    assert "No messages currently need review" in capsys.readouterr().out


# --- successful approval -----------------------------------------------------


def test_approve_review_changes_status_to_parsed(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    make_needs_review_message(conn, "msg_1")
    conn.close()

    exit_code = cli.cmd_approve_review(config, "msg_1")
    assert exit_code == 0

    conn = db.connect(config.database_path)
    row = db.get_message(conn, "msg_1")
    assert row["feedback_parse_status"] == "PARSED"
    conn.close()


def test_approve_review_prints_exactly_what_was_approved(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    make_needs_review_message(
        conn, "msg_1", ticker="XYZ", user_comment="Interesting but unclear verdict."
    )
    conn.close()

    exit_code = cli.cmd_approve_review(config, "msg_1")
    assert exit_code == 0

    output = capsys.readouterr().out
    assert "Approved message_id='msg_1'" in output
    assert "NEEDS_REVIEW -> PARSED" in output
    assert "XYZ" in output
    assert "Interesting but unclear verdict." in output


# --- preserving feedback text exactly / preserving raw email ----------------


def test_approve_review_preserves_feedback_row_exactly(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    make_needs_review_message(
        conn,
        "msg_1",
        event_type="UNCLEAR",
        verdict=None,
        ticker="XYZ",
        company=None,
        confidence=0.35,
        user_comment="Verbatim comment, must not change.",
    )
    conn.close()

    conn = db.connect(config.database_path)
    before = db.get_feedback_events_for_message(conn, "msg_1")[0]
    before_values = dict(before)
    conn.close()

    cli.cmd_approve_review(config, "msg_1")

    conn = db.connect(config.database_path)
    after = db.get_feedback_events_for_message(conn, "msg_1")[0]
    after_values = dict(after)
    conn.close()

    # Every field on the feedback row is byte-for-byte identical.
    assert after_values == before_values
    assert after_values["user_comment"] == "Verbatim comment, must not change."
    assert after_values["event_type"] == "UNCLEAR"
    assert after_values["verdict"] is None
    assert after_values["confidence"] == 0.35


def test_approve_review_preserves_raw_email_exactly(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(
        conn, "msg_1", subject="Original subject, verbatim", body_raw="Original body, verbatim."
    )
    insert_feedback_row(conn, "msg_1")
    db.set_feedback_parse_status(conn, "msg_1", "NEEDS_REVIEW")
    conn.close()

    cli.cmd_approve_review(config, "msg_1")

    conn = db.connect(config.database_path)
    row = db.get_message(conn, "msg_1")
    assert row["subject"] == "Original subject, verbatim"
    assert row["body_raw"] == "Original body, verbatim."
    conn.close()


# --- refusing approval of non-NEEDS_REVIEW messages -------------------------


def test_approve_review_refuses_parsed_message(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1")
    insert_feedback_row(conn, "msg_1", event_type="FEEDBACK", verdict="LIKE", ticker="XYZ")
    db.set_feedback_parse_status(conn, "msg_1", "PARSED")
    conn.close()

    exit_code = cli.cmd_approve_review(config, "msg_1")
    assert exit_code == 1

    conn = db.connect(config.database_path)
    assert db.get_message(conn, "msg_1")["feedback_parse_status"] == "PARSED"  # unchanged
    conn.close()


def test_approve_review_refuses_unparsed_message(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1")  # stays UNPARSED
    conn.close()

    exit_code = cli.cmd_approve_review(config, "msg_1")
    assert exit_code == 1

    conn = db.connect(config.database_path)
    assert db.get_message(conn, "msg_1")["feedback_parse_status"] == "UNPARSED"
    conn.close()


def test_approve_review_refuses_unknown_message_id(tmp_path, capsys):
    config = make_config(tmp_path)
    exit_code = cli.cmd_approve_review(config, "does-not-exist")
    assert exit_code == 1
    assert "No message found" in capsys.readouterr().out


# --- learning eligibility ----------------------------------------------------


def test_approved_feedback_becomes_eligible_for_learning(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    make_needs_review_message(conn, "msg_1", ticker="XYZ")
    conn.close()

    # Before approval: not eligible.
    conn = db.connect(config.database_path)
    assert db.get_feedback_eligible_for_learning(conn) == []
    conn.close()

    cli.cmd_approve_review(config, "msg_1")

    conn = db.connect(config.database_path)
    eligible = db.get_feedback_eligible_for_learning(conn)
    assert len(eligible) == 1
    assert eligible[0]["ticker"] == "XYZ"
    conn.close()


def test_unapproved_needs_review_feedback_remains_ineligible_for_learning(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    make_needs_review_message(conn, "msg_review", ticker="XYZ")
    # A separate, genuinely PARSED message is eligible for comparison.
    insert_raw_message(conn, "msg_parsed")
    insert_feedback_row(conn, "msg_parsed", event_type="FEEDBACK", verdict="LIKE", ticker="ABC")
    db.set_feedback_parse_status(conn, "msg_parsed", "PARSED")
    conn.close()

    conn = db.connect(config.database_path)
    eligible = db.get_feedback_eligible_for_learning(conn)
    tickers = {row["ticker"] for row in eligible}

    assert "ABC" in tickers  # PARSED -> eligible
    assert "XYZ" not in tickers  # NEEDS_REVIEW, never approved -> not eligible
    conn.close()


def test_excluded_feedback_stays_ineligible_even_after_approval(tmp_path):
    """excluded_from_learning and feedback_parse_status are independent
    gates -- approval alone must not override an explicit exclusion.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    make_needs_review_message(conn, "msg_1", ticker="XYZ")
    feedback_id = db.get_feedback_events_for_message(conn, "msg_1")[0]["feedback_id"]
    db.set_feedback_excluded_from_learning(conn, feedback_id, True)
    conn.close()

    cli.cmd_approve_review(config, "msg_1")

    conn = db.connect(config.database_path)
    eligible = db.get_feedback_eligible_for_learning(conn)
    assert eligible == []
    conn.close()
