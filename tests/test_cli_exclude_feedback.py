"""Tests for exclude-feedback / include-feedback (feedback housekeeping).

Both commands are pure database operations -- they only ever flip
feedback.excluded_from_learning on matching rows -- so these tests seed
messages_raw/feedback directly and check what the commands do to them.
Nothing here calls a real LLM or AgentMail.
"""

import pytest

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
    event_type: str = "FEEDBACK",
    verdict="LIKE",
    ticker=None,
    company=None,
) -> None:
    db.insert_feedback(
        conn,
        message_id=message_id,
        event_index=event_index,
        event_type=event_type,
        verdict=verdict,
        ticker=ticker,
        company=company,
        novelty="UNKNOWN",
        user_comment="Looks interesting.",
        parsed_json="{}",
        parser_version=parser.PARSER_VERSION,
        model_name="claude-haiku-4-5",
        confidence=0.9,
        created_at="2026-01-01T00:00:00+00:00",
    )


def get_feedback_row(conn, feedback_id: int):
    return conn.execute(
        "SELECT * FROM feedback WHERE feedback_id = ?", (feedback_id,)
    ).fetchone()


# --- exclusion ---------------------------------------------------------------


def test_exclude_feedback_marks_matching_record_by_ticker(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1")
    insert_feedback_row(conn, "msg_1", ticker="XYZ")
    conn.close()

    exit_code = cli.cmd_exclude_feedback(config, "XYZ")
    assert exit_code == 0

    output = capsys.readouterr().out
    assert "XYZ" in output
    assert "Excluding from learning" in output

    conn = db.connect(config.database_path)
    row = conn.execute("SELECT * FROM feedback WHERE message_id = 'msg_1'").fetchone()
    assert row["excluded_from_learning"] == 1
    conn.close()


def test_exclude_feedback_matches_by_company_name(tmp_path):
    """The two named smoke-test records (TESTCO, XYZ) confirm exclusion
    must work whether the value lives in `ticker` or `company` -- not
    every feedback event has a ticker filled in.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1")
    insert_feedback_row(conn, "msg_1", event_type="NEW_IDEA", verdict=None, ticker=None, company="TESTCO")
    conn.close()

    exit_code = cli.cmd_exclude_feedback(config, "TESTCO")
    assert exit_code == 0

    conn = db.connect(config.database_path)
    row = conn.execute("SELECT * FROM feedback WHERE message_id = 'msg_1'").fetchone()
    assert row["excluded_from_learning"] == 1
    conn.close()


def test_exclude_feedback_is_case_insensitive(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1")
    insert_feedback_row(conn, "msg_1", ticker="XYZ")
    conn.close()

    exit_code = cli.cmd_exclude_feedback(config, "xyz")
    assert exit_code == 0

    conn = db.connect(config.database_path)
    row = conn.execute("SELECT * FROM feedback WHERE message_id = 'msg_1'").fetchone()
    assert row["excluded_from_learning"] == 1
    conn.close()


def test_exclude_feedback_only_touches_matching_rows(tmp_path):
    """Excluding one ticker must not affect other feedback rows -- even
    other events belonging to the same email.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1")
    insert_feedback_row(conn, "msg_1", event_index=0, ticker="XYZ")
    insert_feedback_row(conn, "msg_1", event_index=1, ticker="ABC")
    insert_raw_message(conn, "msg_2")
    insert_feedback_row(conn, "msg_2", ticker="ABC")
    conn.close()

    exit_code = cli.cmd_exclude_feedback(config, "XYZ")
    assert exit_code == 0

    conn = db.connect(config.database_path)
    xyz_row = conn.execute(
        "SELECT * FROM feedback WHERE message_id = 'msg_1' AND event_index = 0"
    ).fetchone()
    abc_row_1 = conn.execute(
        "SELECT * FROM feedback WHERE message_id = 'msg_1' AND event_index = 1"
    ).fetchone()
    abc_row_2 = conn.execute("SELECT * FROM feedback WHERE message_id = 'msg_2'").fetchone()

    assert xyz_row["excluded_from_learning"] == 1
    assert abc_row_1["excluded_from_learning"] == 0
    assert abc_row_2["excluded_from_learning"] == 0
    conn.close()


def test_exclude_feedback_no_match_reports_clearly_and_changes_nothing(tmp_path, capsys):
    config = make_config(tmp_path)
    db.connect(config.database_path).close()  # empty database

    exit_code = cli.cmd_exclude_feedback(config, "NOPE")
    assert exit_code == 1
    assert "No feedback records found" in capsys.readouterr().out


# --- restoration (include-feedback) ------------------------------------------


def test_include_feedback_reverses_exclusion(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1")
    insert_feedback_row(conn, "msg_1", ticker="XYZ")
    conn.close()

    cli.cmd_exclude_feedback(config, "XYZ")

    conn = db.connect(config.database_path)
    assert conn.execute(
        "SELECT excluded_from_learning FROM feedback WHERE message_id = 'msg_1'"
    ).fetchone()["excluded_from_learning"] == 1
    conn.close()

    exit_code = cli.cmd_include_feedback(config, "XYZ")
    assert exit_code == 0
    assert "Re-including in learning" in capsys.readouterr().out

    conn = db.connect(config.database_path)
    row = conn.execute("SELECT * FROM feedback WHERE message_id = 'msg_1'").fetchone()
    assert row["excluded_from_learning"] == 0
    conn.close()


def test_include_feedback_no_match_reports_clearly(tmp_path, capsys):
    config = make_config(tmp_path)
    db.connect(config.database_path).close()

    exit_code = cli.cmd_include_feedback(config, "NOPE")
    assert exit_code == 1
    assert "No feedback records found" in capsys.readouterr().out


# --- persistence ---------------------------------------------------------------


def test_exclusion_persists_across_reconnects(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1")
    insert_feedback_row(conn, "msg_1", ticker="XYZ")
    conn.close()

    cli.cmd_exclude_feedback(config, "XYZ")

    # Fresh connections, as a real second CLI invocation would use.
    conn = db.connect(config.database_path)
    row = conn.execute("SELECT * FROM feedback WHERE message_id = 'msg_1'").fetchone()
    assert row["excluded_from_learning"] == 1
    conn.close()

    conn = db.connect(config.database_path)
    row = conn.execute("SELECT * FROM feedback WHERE message_id = 'msg_1'").fetchone()
    assert row["excluded_from_learning"] == 1
    conn.close()


# --- raw-data preservation ---------------------------------------------------


def test_exclude_feedback_never_deletes_raw_message_or_feedback_row(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(
        conn, "msg_1", subject="Original subject, verbatim", body_raw="Original body, verbatim."
    )
    insert_feedback_row(conn, "msg_1", ticker="XYZ", verdict="STRONG_LIKE")
    conn.close()

    cli.cmd_exclude_feedback(config, "XYZ")

    conn = db.connect(config.database_path)
    # Raw email untouched.
    message_row = db.get_message(conn, "msg_1")
    assert message_row is not None
    assert message_row["subject"] == "Original subject, verbatim"
    assert message_row["body_raw"] == "Original body, verbatim."

    # Feedback record itself untouched except for the one flag.
    feedback_row = conn.execute("SELECT * FROM feedback WHERE message_id = 'msg_1'").fetchone()
    assert feedback_row is not None
    assert feedback_row["ticker"] == "XYZ"
    assert feedback_row["verdict"] == "STRONG_LIKE"
    assert feedback_row["excluded_from_learning"] == 1

    assert db.count_feedback_records(conn) == 1  # nothing was deleted
    conn.close()


def test_include_feedback_never_deletes_anything_either(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1", subject="Keep me", body_raw="Keep me too.")
    insert_feedback_row(conn, "msg_1", ticker="XYZ")
    conn.close()

    cli.cmd_exclude_feedback(config, "XYZ")
    cli.cmd_include_feedback(config, "XYZ")

    conn = db.connect(config.database_path)
    message_row = db.get_message(conn, "msg_1")
    assert message_row["subject"] == "Keep me"
    assert message_row["body_raw"] == "Keep me too."
    assert db.count_feedback_records(conn) == 1
    conn.close()


# --- exact feedback_id targeting (duplicate/follow-up judgments) -----------


def test_exclude_feedback_by_id_excludes_only_that_row_same_ticker(tmp_path, capsys):
    """Reproduces the real BAR case: two feedback rows for the same
    ticker (an original judgment and a later duplicate/follow-up) --
    excluding by feedback_id must affect ONLY the targeted row.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_original")
    insert_feedback_row(conn, "msg_original", ticker="BAR", company="Barco")
    original_row = conn.execute(
        "SELECT feedback_id FROM feedback WHERE message_id = 'msg_original'"
    ).fetchone()
    insert_raw_message(conn, "msg_followup")
    insert_feedback_row(conn, "msg_followup", ticker="BAR", company="Barco")
    followup_row = conn.execute(
        "SELECT feedback_id FROM feedback WHERE message_id = 'msg_followup'"
    ).fetchone()
    conn.close()

    exit_code = cli.cmd_exclude_feedback(config, None, feedback_id=followup_row["feedback_id"])
    assert exit_code == 0

    output = capsys.readouterr().out
    assert "Feedback records excluded: 1" in output
    assert f"feedback_id: {followup_row['feedback_id']}" in output
    assert "ticker/company: BAR / Barco" in output

    conn = db.connect(config.database_path)
    original_after = conn.execute(
        "SELECT * FROM feedback WHERE feedback_id = ?", (original_row["feedback_id"],)
    ).fetchone()
    followup_after = conn.execute(
        "SELECT * FROM feedback WHERE feedback_id = ?", (followup_row["feedback_id"],)
    ).fetchone()
    conn.close()

    assert followup_after["excluded_from_learning"] == 1
    assert original_after["excluded_from_learning"] == 0  # the ORIGINAL judgment untouched


def test_include_feedback_by_id_reverses_only_that_row(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_original")
    insert_feedback_row(conn, "msg_original", ticker="BAR", company="Barco")
    original_row = conn.execute(
        "SELECT feedback_id FROM feedback WHERE message_id = 'msg_original'"
    ).fetchone()
    insert_raw_message(conn, "msg_followup")
    insert_feedback_row(conn, "msg_followup", ticker="BAR", company="Barco")
    followup_row = conn.execute(
        "SELECT feedback_id FROM feedback WHERE message_id = 'msg_followup'"
    ).fetchone()
    conn.close()

    cli.cmd_exclude_feedback(config, None, feedback_id=followup_row["feedback_id"])
    cli.cmd_exclude_feedback(config, None, feedback_id=original_row["feedback_id"])
    capsys.readouterr()

    exit_code = cli.cmd_include_feedback(config, None, feedback_id=followup_row["feedback_id"])
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Feedback records included: 1" in output
    assert f"feedback_id: {followup_row['feedback_id']}" in output

    conn = db.connect(config.database_path)
    original_after = conn.execute(
        "SELECT * FROM feedback WHERE feedback_id = ?", (original_row["feedback_id"],)
    ).fetchone()
    followup_after = conn.execute(
        "SELECT * FROM feedback WHERE feedback_id = ?", (followup_row["feedback_id"],)
    ).fetchone()
    conn.close()

    assert followup_after["excluded_from_learning"] == 0  # reversed
    assert original_after["excluded_from_learning"] == 1  # untouched by the include call


def test_exclude_feedback_by_id_nonexistent_fails_clearly_with_no_changes(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1")
    insert_feedback_row(conn, "msg_1", ticker="XYZ")
    conn.close()

    exit_code = cli.cmd_exclude_feedback(config, None, feedback_id=999999)
    assert exit_code != 0
    output = capsys.readouterr().out
    assert "No feedback record found" in output
    assert "999999" in output

    conn = db.connect(config.database_path)
    row = conn.execute("SELECT * FROM feedback WHERE message_id = 'msg_1'").fetchone()
    assert row["excluded_from_learning"] == 0  # untouched
    assert db.count_feedback_records(conn) == 1  # nothing created or deleted
    conn.close()


def test_include_feedback_by_id_nonexistent_fails_clearly_with_no_changes(tmp_path, capsys):
    config = make_config(tmp_path)
    db.connect(config.database_path).close()

    exit_code = cli.cmd_include_feedback(config, None, feedback_id=999999)
    assert exit_code != 0
    assert "No feedback record found" in capsys.readouterr().out


def test_exclude_feedback_by_id_is_idempotent(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1")
    insert_feedback_row(conn, "msg_1", ticker="BAR")
    feedback_id = conn.execute("SELECT feedback_id FROM feedback").fetchone()["feedback_id"]
    conn.close()

    exit_code_1 = cli.cmd_exclude_feedback(config, None, feedback_id=feedback_id)
    assert exit_code_1 == 0
    first_output = capsys.readouterr().out
    assert "(already set, no change)" not in first_output

    exit_code_2 = cli.cmd_exclude_feedback(config, None, feedback_id=feedback_id)
    assert exit_code_2 == 0
    second_output = capsys.readouterr().out
    assert "(already set, no change)" in second_output
    assert "Feedback records excluded: 1" in second_output

    conn = db.connect(config.database_path)
    row = conn.execute("SELECT * FROM feedback WHERE feedback_id = ?", (feedback_id,)).fetchone()
    assert row["excluded_from_learning"] == 1
    conn.close()


def test_include_feedback_by_id_is_idempotent(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1")
    insert_feedback_row(conn, "msg_1", ticker="BAR")
    feedback_id = conn.execute("SELECT feedback_id FROM feedback").fetchone()["feedback_id"]
    conn.close()

    # Already-included (default state) -- including it again must be safe.
    exit_code = cli.cmd_include_feedback(config, None, feedback_id=feedback_id)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "(already set, no change)" in output
    assert "Feedback records included: 1" in output

    conn = db.connect(config.database_path)
    row = conn.execute("SELECT * FROM feedback WHERE feedback_id = ?", (feedback_id,)).fetchone()
    assert row["excluded_from_learning"] == 0
    conn.close()


def test_exclude_feedback_by_id_never_deletes_raw_message_or_feedback_row(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_1", subject="Original subject, verbatim", body_raw="Original body, verbatim.")
    insert_feedback_row(conn, "msg_1", ticker="BAR", verdict="STRONG_LIKE")
    feedback_id = conn.execute("SELECT feedback_id FROM feedback").fetchone()["feedback_id"]
    conn.close()

    cli.cmd_exclude_feedback(config, None, feedback_id=feedback_id)

    conn = db.connect(config.database_path)
    message_row = db.get_message(conn, "msg_1")
    assert message_row["subject"] == "Original subject, verbatim"
    assert message_row["body_raw"] == "Original body, verbatim."

    feedback_row = conn.execute("SELECT * FROM feedback WHERE feedback_id = ?", (feedback_id,)).fetchone()
    assert feedback_row["ticker"] == "BAR"
    assert feedback_row["verdict"] == "STRONG_LIKE"
    assert feedback_row["excluded_from_learning"] == 1
    assert db.count_feedback_records(conn) == 1
    conn.close()


# --- CLI argument parsing: exactly-one targeting mode -----------------------


def test_exclude_feedback_cli_rejects_both_ticker_and_feedback_id():
    parser_obj = cli.build_parser()
    with pytest.raises(SystemExit):
        parser_obj.parse_args(["exclude-feedback", "BAR", "--feedback-id", "27"])


def test_exclude_feedback_cli_rejects_neither_ticker_nor_feedback_id():
    parser_obj = cli.build_parser()
    with pytest.raises(SystemExit):
        parser_obj.parse_args(["exclude-feedback"])


def test_include_feedback_cli_rejects_both_ticker_and_feedback_id():
    parser_obj = cli.build_parser()
    with pytest.raises(SystemExit):
        parser_obj.parse_args(["include-feedback", "BAR", "--feedback-id", "27"])


def test_include_feedback_cli_rejects_neither_ticker_nor_feedback_id():
    parser_obj = cli.build_parser()
    with pytest.raises(SystemExit):
        parser_obj.parse_args(["include-feedback"])


def test_exclude_feedback_cli_accepts_ticker_only():
    parser_obj = cli.build_parser()
    args = parser_obj.parse_args(["exclude-feedback", "BAR"])
    assert args.ticker == "BAR"
    assert args.feedback_id is None


def test_exclude_feedback_cli_accepts_feedback_id_only():
    parser_obj = cli.build_parser()
    args = parser_obj.parse_args(["exclude-feedback", "--feedback-id", "27"])
    assert args.ticker is None
    assert args.feedback_id == 27


# --- show-feedback indicates excluded records --------------------------------


def test_show_feedback_indicates_excluded_records(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_raw_message(conn, "msg_excluded", subject="Excluded idea")
    insert_feedback_row(conn, "msg_excluded", ticker="XYZ")
    insert_raw_message(conn, "msg_normal", subject="Normal idea")
    insert_feedback_row(conn, "msg_normal", ticker="ABC")
    conn.close()

    cli.cmd_exclude_feedback(config, "XYZ")
    capsys.readouterr()  # discard exclude-feedback's own output

    exit_code = cli.cmd_show_feedback(config, limit=10)
    assert exit_code == 0

    output = capsys.readouterr().out
    lines = output.splitlines()
    xyz_line = next(line for line in lines if "XYZ" in line)
    abc_line = next(line for line in lines if "ABC" in line)

    assert "[EXCLUDED FROM LEARNING]" in xyz_line
    assert "[EXCLUDED FROM LEARNING]" not in abc_line
