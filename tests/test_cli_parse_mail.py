"""Tests for the parse-mail and show-feedback command logic (Stage 2).

Like tests/test_cli_check_mail.py, these never call a real LLM API. They
monkeypatch ideascout.parser (build_client / parse_message) so cli.py runs
against fake, deterministic ParserResult values. That's enough to prove the
important behaviors: each feedback vocabulary word lands in the right
column, one email can produce multiple feedback events, ambiguous content
is flagged instead of guessed at, transient failures are retried
automatically while review cases are not, non-Brad senders are never
treated as feedback, and none of this ever creates duplicates or destroys
the raw email on failure.
"""

import json

import pytest

from ideascout import cli, db, parser
from ideascout.config import Config

DEFAULT_ALLOWED_SENDERS = frozenset({"brad@example.com"})


def make_config(tmp_path, brad_allowed_senders=DEFAULT_ALLOWED_SENDERS) -> Config:
    config = Config(
        agentmail_api_key=None,
        agentmail_inbox_id=None,
        database_path=tmp_path / "ideas.db",
        log_path=tmp_path / "app.log",
        anthropic_api_key="fake-anthropic-key",
        parser_model_name="claude-haiku-4-5",
        brad_allowed_senders=brad_allowed_senders,
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
    """Defaults to AUTHENTICATED so tests that aren't specifically about
    the authentication gate don't need to think about it -- only the
    dedicated authentication tests pass something else.
    """
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


def make_result(
    event_type: str,
    verdict: str | None = None,
    confidence: float = 0.9,
    ticker: str | None = "XYZ",
    company: str | None = None,
    novelty: str | None = "UNKNOWN",
    user_comment: str = "Looks interesting.",
) -> parser.ParserResult:
    """Build a ParserResult the way the real parser would, including running
    it through the same determine_review_reason() logic production uses --
    so these tests exercise the real review-routing rule, not a re-typed
    copy of it.
    """
    review_reason = parser.determine_review_reason(event_type, verdict, confidence)
    return parser.ParserResult(
        event_type=event_type,
        verdict=verdict,
        ticker=ticker,
        company=company,
        novelty=novelty,
        user_comment=user_comment,
        confidence=confidence,
        parsed_json=json.dumps(
            {"event_type": event_type, "verdict": verdict, "confidence": confidence}
        ),
        needs_review=review_reason is not None,
        review_reason=review_reason,
    )


def make_events(*results: parser.ParserResult) -> list[parser.ParserResult]:
    return list(results)


def patch_parser(monkeypatch, events_or_fn) -> None:
    """Monkeypatch the LLM boundary. Accepts a single ParserResult, a list
    of them (parse_message's real return shape), or a callable taking
    (client, model, subject, body) for scenarios that need call-count
    tracking or per-call behavior (e.g. flaky-then-succeeds).
    """
    monkeypatch.setattr(parser, "build_client", lambda api_key: object())
    if callable(events_or_fn) and not isinstance(events_or_fn, (parser.ParserResult, list)):
        monkeypatch.setattr(parser, "parse_message", events_or_fn)
        return
    events = [events_or_fn] if isinstance(events_or_fn, parser.ParserResult) else list(events_or_fn)
    monkeypatch.setattr(parser, "parse_message", lambda client, model, subject, body: events)


def get_status(conn, message_id: str) -> str:
    row = conn.execute(
        "SELECT feedback_parse_status FROM messages_raw WHERE message_id = ?", (message_id,)
    ).fetchone()
    return row["feedback_parse_status"]


# --- 1-5: each feedback vocabulary word lands correctly ---------------------


@pytest.mark.parametrize(
    "event_type, verdict",
    [
        ("FEEDBACK", "LIKE"),
        ("FEEDBACK", "STRONG_PASS"),
        ("FEEDBACK", "MAYBE"),
        ("NEW_IDEA", None),
        ("MISSED_IDEA", None),
    ],
)
def test_parse_mail_stores_each_vocabulary_word_correctly(tmp_path, monkeypatch, event_type, verdict):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1")
    conn.close()

    patch_parser(monkeypatch, make_result(event_type, verdict))

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "PARSED"

    events = db.get_feedback_events_for_message(conn, "msg_1")
    assert len(events) == 1
    assert events[0]["event_type"] == event_type
    assert events[0]["verdict"] == verdict
    assert events[0]["parser_version"] == parser.PARSER_VERSION
    assert events[0]["model_name"] == "claude-haiku-4-5"
    conn.close()


# --- 6: ambiguous message becomes NEEDS_REVIEW ------------------------------


def test_parse_mail_unclear_result_becomes_needs_review(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1", body_raw="Not sure what Brad meant here.")
    conn.close()

    patch_parser(monkeypatch, make_result("UNCLEAR", verdict=None, confidence=0.2))

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0  # UNCLEAR is a successful parse, not an error

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "NEEDS_REVIEW"

    events = db.get_feedback_events_for_message(conn, "msg_1")
    assert len(events) == 1
    assert events[0]["event_type"] == "UNCLEAR"
    assert events[0]["verdict"] is None
    conn.close()


# --- 7: duplicate parse does not create duplicate feedback ------------------


def test_parse_mail_is_idempotent_across_runs(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1")
    conn.close()

    calls = []

    def fake_parse_message(client, model, subject, body):
        calls.append(1)
        return make_events(make_result("FEEDBACK", "LIKE"))

    patch_parser(monkeypatch, fake_parse_message)

    first = cli.cmd_parse_mail(config)
    second = cli.cmd_parse_mail(config)

    assert first == 0
    assert second == 0
    # The second run must not call the LLM again for a message that
    # already has a feedback record.
    assert len(calls) == 1

    conn = db.connect(config.database_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM feedback WHERE message_id = 'msg_1'"
    ).fetchone()[0]
    assert count == 1
    conn.close()


# --- 8: parser failure preserves the raw message ----------------------------


def test_parse_mail_permanent_llm_failure_preserves_raw_message(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1", subject="Original subject", body_raw="Original body, verbatim.")
    conn.close()

    def failing_parse(client, model, subject, body):
        raise parser.ParserError("simulated non-transient failure (e.g. bad credentials)")

    patch_parser(monkeypatch, failing_parse)

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 1

    conn = db.connect(config.database_path)
    row = conn.execute("SELECT * FROM messages_raw WHERE message_id = 'msg_1'").fetchone()
    # Raw content is untouched.
    assert row["subject"] == "Original subject"
    assert row["body_raw"] == "Original body, verbatim."
    assert row["feedback_parse_status"] == "ERROR"
    assert row["error"] is not None

    assert db.get_feedback_events_for_message(conn, "msg_1") == []
    assert db.get_meta(conn, "last_error") is not None
    conn.close()


# --- 9: database-write failure does not mark the message PARSED ------------


def test_parse_mail_db_write_failure_does_not_mark_parsed(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1")
    conn.close()

    patch_parser(monkeypatch, make_result("FEEDBACK", "LIKE"))

    def failing_insert(conn, **kwargs):
        raise RuntimeError("simulated disk-full error")

    monkeypatch.setattr(db, "insert_feedback_events", failing_insert)

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 1

    conn = db.connect(config.database_path)
    status = get_status(conn, "msg_1")
    assert status != "PARSED"
    assert status == "UNPARSED"  # untouched, safe to retry on the next run

    count = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    assert count == 0
    conn.close()


# --- 10: quoted/forwarded text is not automatically treated as Brad's opinion


def test_parse_mail_quoted_forwarded_content_is_not_treated_as_opinion(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    forwarded_body = (
        "---------- Forwarded message ---------\n"
        "From: Some Newsletter <news@example.com>\n"
        "Subject: This stock is a screaming buy\n\n"
        "> Our analysts strongly recommend buying XYZ immediately.\n"
        "> This is a can't-miss opportunity.\n"
    )
    insert_raw_message(conn, "msg_1", subject="Fwd: This stock is a screaming buy", body_raw=forwarded_body)
    conn.close()

    # A correctly-behaving parser recognizes there is no comment from Brad
    # himself here (it's entirely forwarded newsletter content) and refuses
    # to guess -- this simulates that correct behavior.
    patch_parser(
        monkeypatch,
        make_result("UNCLEAR", verdict=None, confidence=0.15, ticker=None, user_comment=""),
    )

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "NEEDS_REVIEW"

    events = db.get_feedback_events_for_message(conn, "msg_1")
    assert len(events) == 1
    # Critically: no verdict was fabricated from the forwarded content.
    assert events[0]["verdict"] is None
    assert events[0]["event_type"] == "UNCLEAR"
    conn.close()


# --- New in this revision: multiple feedback events per email --------------


def test_parse_mail_one_email_creates_three_feedback_events(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(
        conn,
        "msg_1",
        subject="Re: Digest",
        body_raw="1 LIKE - hidden asset\n2 PASS - not enough upside\n3 MAYBE - need to understand management",
    )
    conn.close()

    three_events = make_events(
        make_result("FEEDBACK", "LIKE", ticker="AAA", user_comment="hidden asset is interesting"),
        make_result("FEEDBACK", "PASS", ticker="BBB", user_comment="not enough upside"),
        make_result("FEEDBACK", "MAYBE", ticker="CCC", user_comment="need to understand management"),
    )
    patch_parser(monkeypatch, three_events)

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "PARSED"

    events = db.get_feedback_events_for_message(conn, "msg_1")
    assert [e["event_index"] for e in events] == [0, 1, 2]
    assert [e["verdict"] for e in events] == ["LIKE", "PASS", "MAYBE"]
    assert [e["ticker"] for e in events] == ["AAA", "BBB", "CCC"]
    conn.close()


def test_parse_mail_multi_event_rerun_does_not_duplicate_any_event(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1")
    conn.close()

    calls = []

    def fake_parse(client, model, subject, body):
        calls.append(1)
        return make_events(
            make_result("FEEDBACK", "LIKE"),
            make_result("FEEDBACK", "PASS"),
            make_result("FEEDBACK", "MAYBE"),
        )

    patch_parser(monkeypatch, fake_parse)

    first = cli.cmd_parse_mail(config)
    second = cli.cmd_parse_mail(config)

    assert first == 0
    assert second == 0
    assert len(calls) == 1  # never re-parsed once it has feedback events

    conn = db.connect(config.database_path)
    count = conn.execute("SELECT COUNT(*) FROM feedback WHERE message_id = 'msg_1'").fetchone()[0]
    assert count == 3
    conn.close()


# --- New in this revision: automatic retry of transient failures -----------


def test_parse_mail_transient_failure_is_retried_and_succeeds_later(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1")
    conn.close()

    call_count = {"n": 0}

    def flaky_parse(client, model, subject, body):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise parser.RetryableParserError("simulated timeout")
        return make_events(make_result("FEEDBACK", "LIKE"))

    patch_parser(monkeypatch, flaky_parse)

    first_exit = cli.cmd_parse_mail(config)
    assert first_exit == 1  # a retryable error still surfaces as non-zero

    conn = db.connect(config.database_path)
    row = conn.execute(
        "SELECT feedback_parse_status, attempt_count FROM messages_raw WHERE message_id = 'msg_1'"
    ).fetchone()
    assert row["feedback_parse_status"] == "RETRYABLE_ERROR"
    assert row["attempt_count"] == 1
    assert db.get_feedback_events_for_message(conn, "msg_1") == []
    conn.close()

    # A later parse-mail run must pick this message back up automatically --
    # RETRYABLE_ERROR messages are eligible, without any manual intervention.
    second_exit = cli.cmd_parse_mail(config)
    assert second_exit == 0

    conn = db.connect(config.database_path)
    row = conn.execute(
        "SELECT feedback_parse_status, attempt_count FROM messages_raw WHERE message_id = 'msg_1'"
    ).fetchone()
    assert row["feedback_parse_status"] == "PARSED"
    assert row["attempt_count"] == 2
    events = db.get_feedback_events_for_message(conn, "msg_1")
    assert len(events) == 1
    assert events[0]["verdict"] == "LIKE"
    conn.close()

    assert call_count["n"] == 2


def test_parse_mail_needs_review_is_not_retried_automatically(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1", body_raw="Not sure what Brad meant.")
    conn.close()

    calls = []

    def fake_parse(client, model, subject, body):
        calls.append(1)
        return make_events(make_result("UNCLEAR", None, confidence=0.1))

    patch_parser(monkeypatch, fake_parse)

    first_exit = cli.cmd_parse_mail(config)
    second_exit = cli.cmd_parse_mail(config)

    assert first_exit == 0
    assert second_exit == 0
    # Unlike RETRYABLE_ERROR, NEEDS_REVIEW is excluded from the pending
    # query -- so the LLM must be called exactly once, not twice.
    assert len(calls) == 1

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "NEEDS_REVIEW"
    conn.close()


# --- New in this revision: only Brad-authored email is parsed as feedback --


def test_parse_mail_allowed_brad_sender_is_parsed(tmp_path, monkeypatch):
    config = make_config(tmp_path, brad_allowed_senders=frozenset({"brad@example.com"}))
    conn = db.connect(config.database_path)
    # Display-name form, exercising the email.utils.parseaddr extraction.
    insert_raw_message(conn, "msg_1", sender="Brad Smith <brad@example.com>")
    conn.close()

    patch_parser(monkeypatch, make_result("FEEDBACK", "LIKE"))

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "PARSED"
    assert db.count_feedback_records(conn) == 1
    conn.close()


def test_parse_mail_non_brad_sender_is_preserved_but_not_parsed_as_feedback(tmp_path, monkeypatch):
    config = make_config(tmp_path, brad_allowed_senders=frozenset({"brad@example.com"}))
    conn = db.connect(config.database_path)
    insert_raw_message(
        conn,
        "msg_1",
        sender="newsletter@example.com",
        subject="This week's top picks",
        body_raw="Buy XYZ now, huge upside!",
    )
    conn.close()

    calls = []

    def fake_parse(client, model, subject, body):
        calls.append(1)
        return make_events(make_result("FEEDBACK", "STRONG_LIKE"))

    patch_parser(monkeypatch, fake_parse)

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0
    assert len(calls) == 0  # the LLM must never be called for a non-Brad sender

    conn = db.connect(config.database_path)
    row = conn.execute("SELECT * FROM messages_raw WHERE message_id = 'msg_1'").fetchone()
    # Raw email preserved exactly, and NOT treated as successfully parsed feedback.
    assert row["sender"] == "newsletter@example.com"
    assert row["subject"] == "This week's top picks"
    assert row["body_raw"] == "Buy XYZ now, huge upside!"
    assert row["feedback_parse_status"] == "SKIPPED_NOT_BRAD"

    assert db.count_feedback_records(conn) == 0
    conn.close()


# --- New in this revision: authentication is required alongside sender -----


def test_parse_mail_allowed_sender_and_authenticated_is_parsed(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1", sender="brad@example.com", sender_authenticated="AUTHENTICATED")
    conn.close()

    patch_parser(monkeypatch, make_result("FEEDBACK", "LIKE"))

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "PARSED"
    assert db.count_feedback_records(conn) == 1
    conn.close()


def test_parse_mail_allowed_sender_but_unauthenticated_is_skipped(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(
        conn,
        "msg_1",
        subject="Original subject",
        body_raw="Original body, verbatim.",
        sender="brad@example.com",
        sender_authenticated="UNAUTHENTICATED",
    )
    conn.close()

    calls = []

    def fake_parse(client, model, subject, body):
        calls.append(1)
        return make_events(make_result("FEEDBACK", "STRONG_LIKE"))

    patch_parser(monkeypatch, fake_parse)

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0
    assert len(calls) == 0  # never sent to the LLM -- authentication failed, not the parser

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "SKIPPED_UNAUTHENTICATED"
    assert db.count_feedback_records(conn) == 0

    # 5: raw message content remains unchanged.
    row = db.get_message(conn, "msg_1")
    assert row["subject"] == "Original subject"
    assert row["body_raw"] == "Original body, verbatim."
    assert row["sender"] == "brad@example.com"
    conn.close()


def test_parse_mail_allowed_sender_but_unknown_authentication_is_not_parsed(tmp_path, monkeypatch):
    """A message stored before the authentication column existed (or for
    any other reason with unknown status) must never get the benefit of
    the doubt -- unknown is treated the same as a confirmed failure.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1", sender="brad@example.com", sender_authenticated=None)
    conn.close()

    calls = []

    def fake_parse(client, model, subject, body):
        calls.append(1)
        return make_events(make_result("FEEDBACK", "LIKE"))

    patch_parser(monkeypatch, fake_parse)

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0
    assert len(calls) == 0

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "SKIPPED_UNAUTHENTICATED"
    conn.close()


def test_parse_mail_non_brad_sender_skip_takes_precedence_over_authentication(tmp_path, monkeypatch):
    """Sender is checked first: a non-Brad sender is SKIPPED_NOT_BRAD even
    when authenticated, never SKIPPED_UNAUTHENTICATED.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(
        conn, "msg_1", sender="newsletter@example.com", sender_authenticated="AUTHENTICATED"
    )
    conn.close()

    calls = []

    def fake_parse(client, model, subject, body):
        calls.append(1)
        return make_events(make_result("FEEDBACK", "STRONG_LIKE"))

    patch_parser(monkeypatch, fake_parse)

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0
    assert len(calls) == 0

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "SKIPPED_NOT_BRAD"
    conn.close()


def test_parse_mail_unauthenticated_skip_is_idempotent_across_runs(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1", sender="brad@example.com", sender_authenticated="UNAUTHENTICATED")
    conn.close()

    calls = []

    def fake_parse(client, model, subject, body):
        calls.append(1)
        return make_events(make_result("FEEDBACK", "LIKE"))

    patch_parser(monkeypatch, fake_parse)

    first_exit = cli.cmd_parse_mail(config)
    second_exit = cli.cmd_parse_mail(config)

    assert first_exit == 0
    assert second_exit == 0
    assert len(calls) == 0  # never called on either run

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "SKIPPED_UNAUTHENTICATED"
    assert db.count_feedback_records(conn) == 0
    conn.close()


# --- New in this revision: NO_FEEDBACK terminal status ----------------------


def test_parse_mail_zero_events_becomes_no_feedback_not_parsed(tmp_path, monkeypatch):
    """The exact live bug this revision fixes: a successful parse that
    finds zero feedback events must never be marked PARSED.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1", subject="Original subject", body_raw="Original body, verbatim.")
    conn.close()

    calls = []

    def fake_parse(client, model, subject, body):
        calls.append(1)
        return []  # parser ran successfully, found nothing to report

    patch_parser(monkeypatch, fake_parse)

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "NO_FEEDBACK"
    # 2: no feedback row is created.
    assert db.get_feedback_events_for_message(conn, "msg_1") == []
    assert db.count_feedback_records(conn) == 0
    # 7: raw email content remains unchanged.
    row = db.get_message(conn, "msg_1")
    assert row["subject"] == "Original subject"
    assert row["body_raw"] == "Original body, verbatim."
    conn.close()

    # 3: the message is not re-parsed on a later run -- NO_FEEDBACK is
    # terminal, just like SKIPPED_NOT_BRAD/SKIPPED_UNAUTHENTICATED.
    second_exit = cli.cmd_parse_mail(config)
    assert second_exit == 0
    assert len(calls) == 1

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "NO_FEEDBACK"
    conn.close()


def test_parse_mail_never_marks_parsed_without_at_least_one_feedback_row(tmp_path, monkeypatch):
    """Direct statement of the core invariant: PARSED and NO_FEEDBACK are
    the two possible outcomes of a successful parse, and which one a
    message gets depends solely on whether the parser returned any events.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_empty", subject="informational-only")
    insert_raw_message(conn, "msg_full", subject="has-feedback")
    conn.close()

    def fake_parse(client, model, subject, body):
        if subject == "has-feedback":
            return make_events(make_result("FEEDBACK", "LIKE"))
        return []

    patch_parser(monkeypatch, fake_parse)

    cli.cmd_parse_mail(config)

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_empty") == "NO_FEEDBACK"
    assert db.get_feedback_events_for_message(conn, "msg_empty") == []

    assert get_status(conn, "msg_full") == "PARSED"
    assert len(db.get_feedback_events_for_message(conn, "msg_full")) >= 1
    conn.close()


# --- status counts -----------------------------------------------------------


def test_status_counts_reflect_parse_state(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_unparsed")
    insert_raw_message(conn, "msg_parsed")
    insert_raw_message(conn, "msg_needs_review")
    insert_raw_message(conn, "msg_retryable")
    insert_raw_message(conn, "msg_error")
    insert_raw_message(conn, "msg_skipped", sender="newsletter@example.com")
    insert_raw_message(conn, "msg_unauthenticated", sender_authenticated="UNAUTHENTICATED")
    insert_raw_message(conn, "msg_no_feedback")
    conn.close()

    # Set up DB state directly (one message per feedback_parse_status)
    # rather than running cmd_parse_mail, since this test is only about
    # whether status counts and reports each state correctly.
    conn = db.connect(config.database_path)
    db.set_feedback_parse_status(conn, "msg_parsed", "PARSED")
    db.insert_feedback(
        conn,
        message_id="msg_parsed",
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
    db.set_feedback_parse_status(conn, "msg_needs_review", "NEEDS_REVIEW")
    db.insert_feedback(
        conn,
        message_id="msg_needs_review",
        event_type="UNCLEAR",
        verdict=None,
        ticker=None,
        company=None,
        novelty=None,
        user_comment="",
        parsed_json="{}",
        parser_version=parser.PARSER_VERSION,
        model_name="claude-haiku-4-5",
        confidence=0.1,
        created_at="2026-01-01T00:00:00+00:00",
    )
    db.set_feedback_parse_status(conn, "msg_retryable", "RETRYABLE_ERROR", error="simulated timeout")
    db.set_feedback_parse_status(conn, "msg_error", "ERROR", error="simulated auth failure")
    db.set_feedback_parse_status(conn, "msg_skipped", "SKIPPED_NOT_BRAD")
    db.set_feedback_parse_status(conn, "msg_unauthenticated", "SKIPPED_UNAUTHENTICATED")
    db.set_feedback_parse_status(conn, "msg_no_feedback", "NO_FEEDBACK")
    db.set_meta(conn, "last_check_at", "2026-01-01T00:00:00+00:00")
    db.set_meta(conn, "last_parse_at", "2026-01-01T00:05:00+00:00")
    conn.close()

    exit_code = cli.cmd_status(config)
    assert exit_code == 0

    output = capsys.readouterr().out
    assert "Total raw messages: 8" in output
    assert "Unparsed messages: 1" in output
    assert "Parsed messages: 1" in output
    assert "No-feedback messages: 1" in output
    assert "Needs review: 1" in output
    assert "Retryable errors: 1" in output
    assert "Error messages: 1" in output
    assert "Skipped (not a Brad sender): 1" in output
    assert "Skipped (unauthenticated): 1" in output
    assert "Feedback records: 2" in output
    assert "Last successful inbox check: 2026-01-01T00:00:00+00:00" in output
    assert "Last successful parse run: 2026-01-01T00:05:00+00:00" in output
    assert "Last error: none" in output


# --- show-feedback -----------------------------------------------------------


def test_show_feedback_prints_recent_records(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1", subject="Idea about XYZ")
    db.insert_feedback(
        conn,
        message_id="msg_1",
        event_type="FEEDBACK",
        verdict="LIKE",
        ticker="XYZ",
        company=None,
        novelty="UNKNOWN",
        user_comment="Hidden asset is interesting.",
        parsed_json="{}",
        parser_version=parser.PARSER_VERSION,
        model_name="claude-haiku-4-5",
        confidence=0.95,
        created_at="2026-01-01T00:00:00+00:00",
    )
    conn.close()

    exit_code = cli.cmd_show_feedback(config, limit=10)
    assert exit_code == 0

    output = capsys.readouterr().out
    assert "XYZ" in output
    assert "FEEDBACK" in output
    assert "LIKE" in output
    assert "Hidden asset is interesting." in output
    assert "0.95" in output


def test_show_feedback_with_no_records_is_not_an_error(tmp_path, capsys):
    config = make_config(tmp_path)
    exit_code = cli.cmd_show_feedback(config)
    assert exit_code == 0
    assert "No feedback records yet" in capsys.readouterr().out
