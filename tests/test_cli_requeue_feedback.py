"""Tests for the requeue-feedback command (Stage 2 usability improvement).

These never call a real LLM -- requeue-feedback is a pure database
operation (it only ever resets feedback_parse_status/error), so the
fixtures here just seed messages_raw/feedback directly at whatever state
is needed and check what requeue-feedback does to them.
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
    # Normal commands now refuse to silently create a missing production
    # database (see db.open_production_database) -- tests may still create
    # temporary databases normally, so do that explicitly here.
    db.connect(config.database_path).close()
    return config


def insert_raw_message(
    conn,
    message_id: str,
    subject: str = "An idea",
    body_raw: str = "LIKE.",
    sender: str = "brad@example.com",
    sender_authenticated: str | None = "AUTHENTICATED",
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
        sender_authenticated=sender_authenticated,
    )


def insert_feedback_row(conn, message_id: str, event_index: int = 0, event_type: str = "FEEDBACK", verdict="LIKE") -> None:
    db.insert_feedback(
        conn,
        message_id=message_id,
        event_index=event_index,
        event_type=event_type,
        verdict=verdict,
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


# --- 1: one ERROR message can be requeued -----------------------------------


def test_requeue_error_message_by_id(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1", subject="Original subject", body_raw="Original body.")
    db.set_feedback_parse_status(conn, "msg_1", "ERROR", error="simulated auth failure")
    conn.close()

    exit_code = cli.cmd_requeue_feedback(config, "msg_1", all_errors=False)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    row = db.get_message(conn, "msg_1")
    assert row["feedback_parse_status"] == "UNPARSED"
    assert row["error"] is None
    conn.close()


# --- 2: NEEDS_REVIEW can be explicitly requeued by message ID --------------


def test_requeue_needs_review_message_by_id(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1")
    insert_feedback_row(conn, "msg_1", event_type="UNCLEAR", verdict=None)
    db.set_feedback_parse_status(conn, "msg_1", "NEEDS_REVIEW", error=None)
    conn.close()

    exit_code = cli.cmd_requeue_feedback(config, "msg_1", all_errors=False)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    row = db.get_message(conn, "msg_1")
    assert row["feedback_parse_status"] == "UNPARSED"
    assert row["error"] is None
    # The superseded (unconfirmed) derived event is gone -- this is what
    # makes the next parse-mail run genuinely re-call the LLM instead of
    # resyncing status from stale output.
    assert db.get_feedback_events_for_message(conn, "msg_1") == []
    conn.close()


def test_requeue_refuses_parsed_message(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1")
    insert_feedback_row(conn, "msg_1")
    db.set_feedback_parse_status(conn, "msg_1", "PARSED")
    conn.close()

    exit_code = cli.cmd_requeue_feedback(config, "msg_1", all_errors=False)
    assert exit_code == 1

    conn = db.connect(config.database_path)
    row = db.get_message(conn, "msg_1")
    assert row["feedback_parse_status"] == "PARSED"  # unchanged
    # 6: a PARSED message's confirmed feedback is never touched -- the
    # eligibility check above refuses before any delete/update can happen.
    events = db.get_feedback_events_for_message(conn, "msg_1")
    assert len(events) == 1
    assert events[0]["verdict"] == "LIKE"
    conn.close()


def test_requeue_refuses_skipped_not_brad_message(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1", sender="newsletter@example.com")
    db.set_feedback_parse_status(conn, "msg_1", "SKIPPED_NOT_BRAD")
    conn.close()

    exit_code = cli.cmd_requeue_feedback(config, "msg_1", all_errors=False)
    assert exit_code == 1

    conn = db.connect(config.database_path)
    assert db.get_message(conn, "msg_1")["feedback_parse_status"] == "SKIPPED_NOT_BRAD"
    conn.close()


def test_requeue_missing_message_id_reports_clearly(tmp_path):
    config = make_config(tmp_path)
    db.connect(config.database_path).close()  # create an empty database

    exit_code = cli.cmd_requeue_feedback(config, "does-not-exist", all_errors=False)
    assert exit_code == 1


# --- 3: --all-errors requeues only ERROR messages ---------------------------


def test_requeue_all_errors_only_touches_error_messages(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)

    insert_raw_message(conn, "msg_error_1")
    db.set_feedback_parse_status(conn, "msg_error_1", "ERROR", error="boom 1")

    insert_raw_message(conn, "msg_error_2")
    db.set_feedback_parse_status(conn, "msg_error_2", "ERROR", error="boom 2")

    insert_raw_message(conn, "msg_needs_review")
    insert_feedback_row(conn, "msg_needs_review", event_type="UNCLEAR", verdict=None)
    db.set_feedback_parse_status(conn, "msg_needs_review", "NEEDS_REVIEW")

    insert_raw_message(conn, "msg_skipped", sender="newsletter@example.com")
    db.set_feedback_parse_status(conn, "msg_skipped", "SKIPPED_NOT_BRAD")

    insert_raw_message(conn, "msg_unparsed")  # stays UNPARSED (the default)

    conn.close()

    exit_code = cli.cmd_requeue_feedback(config, None, all_errors=True)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    assert db.get_message(conn, "msg_error_1")["feedback_parse_status"] == "UNPARSED"
    assert db.get_message(conn, "msg_error_2")["feedback_parse_status"] == "UNPARSED"
    # Untouched:
    assert db.get_message(conn, "msg_needs_review")["feedback_parse_status"] == "NEEDS_REVIEW"
    assert db.get_message(conn, "msg_skipped")["feedback_parse_status"] == "SKIPPED_NOT_BRAD"
    assert db.get_message(conn, "msg_unparsed")["feedback_parse_status"] == "UNPARSED"
    conn.close()


def test_requeue_all_errors_with_nothing_to_do(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1")  # UNPARSED, no errors at all
    conn.close()

    exit_code = cli.cmd_requeue_feedback(config, None, all_errors=True)
    assert exit_code == 0
    assert "Nothing to requeue" in capsys.readouterr().out


# --- 4: requeuing NEEDS_REVIEW triggers a genuine fresh LLM call without ----
# --- duplicating the old ambiguous result, and never touches the raw email -


def make_parse_config(base_config: Config) -> Config:
    return Config(
        agentmail_api_key=None,
        agentmail_inbox_id=None,
        database_path=base_config.database_path,
        log_path=base_config.log_path,
        anthropic_api_key="fake-key",
        parser_model_name="claude-haiku-4-5",
        brad_allowed_senders=frozenset({"brad@example.com"}),
    )


def test_requeue_needs_review_causes_genuine_reparse_without_duplicating(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    original_subject = "Re: Digest"
    original_body = "MAYBE - not sure about this one, need more info on management."
    insert_raw_message(conn, "msg_1", subject=original_subject, body_raw=original_body)

    # 1: the NEEDS_REVIEW message already has a derived (ambiguous) event.
    insert_feedback_row(conn, "msg_1", event_index=0, event_type="UNCLEAR", verdict=None)
    db.set_feedback_parse_status(conn, "msg_1", "NEEDS_REVIEW", error=None)
    conn.close()

    # 2: requeue-feedback by message ID.
    exit_code = cli.cmd_requeue_feedback(config, "msg_1", all_errors=False)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    row = db.get_message(conn, "msg_1")
    assert row["feedback_parse_status"] == "UNPARSED"
    # The old ambiguous event is gone -- this is what makes the message
    # eligible for a genuine new LLM call rather than being silently
    # resynced from stale output.
    assert db.get_feedback_events_for_message(conn, "msg_1") == []
    conn.close()

    # 3: the next parse-mail run must actually call the parser again.
    calls = []

    def fake_parse_message(client, model, subject, body):
        calls.append((subject, body))
        return [
            parser.ParserResult(
                event_type="FEEDBACK",
                verdict="MAYBE",
                ticker=None,
                company=None,
                novelty="UNKNOWN",
                user_comment="Need to understand management better.",
                confidence=0.9,
                parsed_json="{}",
                needs_review=False,
                review_reason=None,
            )
        ]

    monkeypatch.setattr(parser, "build_client", lambda api_key: object())
    monkeypatch.setattr(parser, "parse_message", fake_parse_message)

    exit_code = cli.cmd_parse_mail(make_parse_config(config))
    assert exit_code == 0
    assert len(calls) == 1  # the LLM genuinely ran again
    assert calls[0] == (original_subject, original_body)

    # 4: exactly one (new) event -- the old UNCLEAR one was not duplicated
    # alongside it, and nothing lingers from before.
    conn = db.connect(config.database_path)
    events = db.get_feedback_events_for_message(conn, "msg_1")
    assert len(events) == 1
    assert events[0]["event_type"] == "FEEDBACK"
    assert events[0]["verdict"] == "MAYBE"
    assert db.get_message(conn, "msg_1")["feedback_parse_status"] == "PARSED"

    # 5: the raw email is byte-for-byte unchanged throughout.
    row = db.get_message(conn, "msg_1")
    assert row["subject"] == original_subject
    assert row["body_raw"] == original_body
    conn.close()


def test_requeue_all_errors_never_touches_needs_review(tmp_path):
    """--all-errors must not affect NEEDS_REVIEW messages at all -- not
    their status, and not their existing (derived) feedback rows.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_needs_review")
    insert_feedback_row(conn, "msg_needs_review", event_type="UNCLEAR", verdict=None)
    db.set_feedback_parse_status(conn, "msg_needs_review", "NEEDS_REVIEW")
    conn.close()

    exit_code = cli.cmd_requeue_feedback(config, None, all_errors=True)
    assert exit_code == 0  # nothing in ERROR, but that's not a failure

    conn = db.connect(config.database_path)
    assert db.get_message(conn, "msg_needs_review")["feedback_parse_status"] == "NEEDS_REVIEW"
    assert len(db.get_feedback_events_for_message(conn, "msg_needs_review")) == 1
    conn.close()


# --- 5: raw message content is unchanged ------------------------------------


def test_requeue_never_touches_raw_email_fields(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(
        conn,
        "msg_1",
        subject="Original subject, verbatim",
        body_raw="Original body, verbatim.",
        sender="brad@example.com",
    )
    db.set_feedback_parse_status(conn, "msg_1", "ERROR", error="simulated failure")
    conn.close()

    cli.cmd_requeue_feedback(config, "msg_1", all_errors=False)

    conn = db.connect(config.database_path)
    row = db.get_message(conn, "msg_1")
    assert row["subject"] == "Original subject, verbatim"
    assert row["body_raw"] == "Original body, verbatim."
    assert row["sender"] == "brad@example.com"
    conn.close()
