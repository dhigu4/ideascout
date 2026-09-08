"""Tests for the SQLite layer (ideascout/db.py).

Every test uses a throwaway database file inside pytest's tmp_path fixture,
so nothing here ever touches the real data/ideas.db.
"""

import sqlite3

import pytest

from ideascout import db


def make_message_kwargs(message_id: str = "msg_1", **overrides) -> dict:
    kwargs = dict(
        message_id=message_id,
        inbox_id="inbox_abc",
        thread_id="thread_abc",
        received_at="2026-01-01T12:00:00+00:00",
        sender="alice@example.com",
        recipients="ideas@yourdomain.agentmail.to",
        subject="A great idea",
        body_raw="Here is my idea, verbatim.",
        body_format="text",
        size_bytes=1234,
        stored_at="2026-01-01T12:00:05+00:00",
    )
    kwargs.update(overrides)
    return kwargs


def test_connect_creates_schema(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")

    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert "messages_raw" in tables
    assert "app_meta" in tables

    user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert user_version == len(db.MIGRATIONS)

    conn.close()


def test_connect_is_safe_to_call_repeatedly(tmp_path):
    db_path = tmp_path / "ideas.db"

    conn1 = db.connect(db_path)
    db.insert_message(conn1, **make_message_kwargs())
    conn1.close()

    # Reconnecting (as every CLI invocation does) must not wipe existing data.
    conn2 = db.connect(db_path)
    assert db.count_total_messages(conn2) == 1
    conn2.close()


def test_insert_message_saves_all_fields(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")

    inserted = db.insert_message(conn, **make_message_kwargs())
    assert inserted is True

    row = conn.execute(
        "SELECT * FROM messages_raw WHERE message_id = ?", ("msg_1",)
    ).fetchone()
    assert row is not None
    assert row["sender"] == "alice@example.com"
    assert row["subject"] == "A great idea"
    assert row["body_raw"] == "Here is my idea, verbatim."
    assert row["parse_status"] == "UNPARSED"
    assert row["error"] is None

    conn.close()


def test_duplicate_message_id_is_ignored_not_duplicated(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")

    first = db.insert_message(conn, **make_message_kwargs(subject="Original"))
    second = db.insert_message(conn, **make_message_kwargs(subject="Resent copy"))

    assert first is True
    assert second is False
    assert db.count_total_messages(conn) == 1

    row = conn.execute(
        "SELECT subject FROM messages_raw WHERE message_id = ?", ("msg_1",)
    ).fetchone()
    # The original stored row must win; a "duplicate" delivery never overwrites it.
    assert row["subject"] == "Original"

    conn.close()


def test_insert_message_missing_required_field_fails_and_writes_nothing(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")

    bad_kwargs = make_message_kwargs()
    bad_kwargs["received_at"] = None  # NOT NULL column

    with pytest.raises(sqlite3.IntegrityError):
        db.insert_message(conn, **bad_kwargs)

    # A failed write must leave zero rows behind -- never a half-written message.
    assert db.count_total_messages(conn) == 0

    conn.close()


def test_unparsed_count_reflects_only_unparsed_messages(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")

    db.insert_message(conn, **make_message_kwargs(message_id="msg_1"))
    db.insert_message(conn, **make_message_kwargs(message_id="msg_2"))
    conn.execute(
        "UPDATE messages_raw SET parse_status = 'PARSED' WHERE message_id = 'msg_2'"
    )
    conn.commit()

    assert db.count_total_messages(conn) == 2
    assert db.count_unparsed_messages(conn) == 1

    conn.close()


def test_meta_get_set_roundtrip_and_default(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")

    assert db.get_meta(conn, "last_check_at") is None

    db.set_meta(conn, "last_check_at", "2026-01-01T00:00:00+00:00")
    assert db.get_meta(conn, "last_check_at") == "2026-01-01T00:00:00+00:00"

    db.set_meta(conn, "last_check_at", "2026-01-02T00:00:00+00:00")
    assert db.get_meta(conn, "last_check_at") == "2026-01-02T00:00:00+00:00"

    conn.close()


def test_migration_from_pre_multi_event_schema_preserves_existing_data(tmp_path):
    """Simulates a database created before the multi-event/retry/sender
    revision (migrations 1+2 only: feedback keyed by UNIQUE(message_id),
    no feedback_parse_status/attempt_count columns) and verifies migration
    3 upgrades it in place without losing the message, its parse_status,
    or its existing feedback row.
    """
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Migrations 1 and 2 exactly as they were before this revision.
    conn.executescript(
        """
        CREATE TABLE messages_raw (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id    TEXT NOT NULL UNIQUE,
            inbox_id      TEXT,
            thread_id     TEXT,
            received_at   TEXT NOT NULL,
            sender        TEXT,
            recipients    TEXT,
            subject       TEXT,
            body_raw      TEXT,
            body_format   TEXT NOT NULL DEFAULT 'none',
            size_bytes    INTEGER,
            stored_at     TEXT NOT NULL,
            parse_status  TEXT NOT NULL DEFAULT 'UNPARSED',
            error         TEXT
        );
        CREATE INDEX idx_messages_raw_parse_status ON messages_raw(parse_status);
        CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT);

        CREATE TABLE feedback (
            feedback_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id      TEXT NOT NULL UNIQUE REFERENCES messages_raw(message_id),
            event_type      TEXT NOT NULL,
            verdict         TEXT,
            ticker          TEXT,
            company         TEXT,
            novelty         TEXT,
            user_comment    TEXT,
            parsed_json     TEXT NOT NULL,
            parser_version  TEXT NOT NULL,
            model_name      TEXT NOT NULL,
            confidence      REAL,
            created_at      TEXT NOT NULL
        );
        CREATE INDEX idx_feedback_event_type ON feedback(event_type);
        """
    )
    conn.execute(
        "INSERT INTO messages_raw (message_id, received_at, sender, subject, body_raw, "
        "body_format, stored_at, parse_status) VALUES "
        "('legacy_msg', '2026-01-01T00:00:00+00:00', 'brad@example.com', 'Old idea', "
        "'LIKE, hidden asset', 'text', '2026-01-01T00:00:05+00:00', 'PARSED')"
    )
    conn.execute(
        "INSERT INTO feedback (message_id, event_type, verdict, parsed_json, "
        "parser_version, model_name, confidence, created_at) VALUES "
        "('legacy_msg', 'FEEDBACK', 'LIKE', '{}', 'feedback-v1', 'claude-haiku-4-5', "
        "0.9, '2026-01-01T00:00:10+00:00')"
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    # Reconnecting through the app's normal path applies migration 3.
    conn = db.connect(db_path)

    row = conn.execute("SELECT * FROM messages_raw WHERE message_id = 'legacy_msg'").fetchone()
    assert row["subject"] == "Old idea"
    assert row["body_raw"] == "LIKE, hidden asset"
    assert row["parse_status"] == "PARSED"  # legacy column untouched
    assert row["feedback_parse_status"] == "PARSED"  # backfilled from parse_status
    assert row["attempt_count"] == 0
    assert row["last_attempt_at"] is None

    events = db.get_feedback_events_for_message(conn, "legacy_msg")
    assert len(events) == 1
    assert events[0]["event_index"] == 0
    assert events[0]["verdict"] == "LIKE"

    # The new UNIQUE(message_id, event_index) constraint is in effect --
    # a second event for the same message at a new index is now allowed.
    inserted = db.insert_feedback(
        conn,
        message_id="legacy_msg",
        event_index=1,
        event_type="FEEDBACK",
        verdict="MAYBE",
        ticker=None,
        company=None,
        novelty=None,
        user_comment="second event",
        parsed_json="{}",
        parser_version="feedback-v1",
        model_name="claude-haiku-4-5",
        confidence=0.8,
        created_at="2026-01-02T00:00:00+00:00",
    )
    assert inserted is True
    assert len(db.get_feedback_events_for_message(conn, "legacy_msg")) == 2

    conn.close()


def _build_pre_migration_4_db(db_path) -> sqlite3.Connection:
    """Build a database schema-identical to one that has only had
    migrations 1-3 applied -- exactly the state in which the real-world
    "PARSED with zero feedback rows" bug could be introduced by migration
    3's now-corrected backfill. Built from the actual MIGRATIONS list
    (not hand-copied SQL) so this can never silently drift from the real
    schema as migrations 1-3 are never edited.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    for migration_sql in db.MIGRATIONS[:3]:
        conn.executescript(migration_sql)
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    return conn


def _insert_legacy_message(
    conn, message_id, feedback_parse_status, parse_status="UNPARSED", error=None
) -> None:
    conn.execute(
        "INSERT INTO messages_raw (message_id, received_at, sender, subject, body_raw, "
        "body_format, stored_at, parse_status, feedback_parse_status, error) VALUES "
        "(?, '2026-01-01T00:00:00+00:00', 'brad@example.com', 'An idea', 'LIKE.', "
        "'text', '2026-01-01T00:00:05+00:00', ?, ?, ?)",
        (message_id, parse_status, feedback_parse_status, error),
    )
    conn.commit()


def _insert_legacy_feedback_row(conn, message_id, event_type="FEEDBACK", verdict="LIKE") -> None:
    conn.execute(
        "INSERT INTO feedback (message_id, event_index, event_type, verdict, parsed_json, "
        "parser_version, model_name, confidence, created_at) VALUES "
        "(?, 0, ?, ?, '{}', 'feedback-v1', 'claude-haiku-4-5', 0.9, '2026-01-01T00:00:10+00:00')",
        (message_id, event_type, verdict),
    )
    conn.commit()


def test_migration_4_repairs_parsed_with_zero_feedback_rows(tmp_path):
    """The exact real-world bug this revision fixes: a message claiming
    PARSED with no feedback row to back that claim up must be reset to
    UNPARSED, with its stale error cleared.
    """
    db_path = tmp_path / "buggy.db"
    conn = _build_pre_migration_4_db(db_path)
    _insert_legacy_message(conn, "msg_broken", feedback_parse_status="PARSED", error="stale error text")
    conn.close()

    conn = db.connect(db_path)  # applies migration 4 (and 5)

    row = db.get_message(conn, "msg_broken")
    assert row["feedback_parse_status"] == "UNPARSED"
    assert row["error"] is None
    conn.close()


def test_migration_4_leaves_legitimate_parsed_with_feedback_untouched(tmp_path):
    """A message that is genuinely PARSED (it has a feedback row to prove
    it) must never be reset, and its feedback must never be touched.
    """
    db_path = tmp_path / "healthy.db"
    conn = _build_pre_migration_4_db(db_path)
    _insert_legacy_message(conn, "msg_good", feedback_parse_status="PARSED")
    _insert_legacy_feedback_row(conn, "msg_good", verdict="LIKE")
    conn.close()

    conn = db.connect(db_path)

    row = db.get_message(conn, "msg_good")
    assert row["feedback_parse_status"] == "PARSED"
    events = db.get_feedback_events_for_message(conn, "msg_good")
    assert len(events) == 1
    assert events[0]["verdict"] == "LIKE"
    conn.close()


def test_migration_4_ignores_legacy_parse_status_as_source_of_truth(tmp_path):
    """A message whose legacy Stage 1 parse_status happens to say 'PARSED'
    must have zero influence on feedback_parse_status. Here
    feedback_parse_status was already (correctly) UNPARSED before
    migration 4 runs; it must stay UNPARSED, proving migration 4 -- and
    everything downstream -- never derives Stage 2 status from Stage 1's
    column.
    """
    db_path = tmp_path / "mixed.db"
    conn = _build_pre_migration_4_db(db_path)
    _insert_legacy_message(conn, "msg_mixed", feedback_parse_status="UNPARSED", parse_status="PARSED")
    conn.close()

    conn = db.connect(db_path)

    row = db.get_message(conn, "msg_mixed")
    assert row["parse_status"] == "PARSED"  # legacy column, left exactly as it was
    assert row["feedback_parse_status"] == "UNPARSED"  # not derived from parse_status
    conn.close()


def test_migration_4_repaired_message_becomes_eligible_for_parse_mail(tmp_path):
    """After repair, the message must actually be considered by parse-mail
    -- not just have the right status value sitting unused in the table.
    """
    db_path = tmp_path / "repaired.db"
    conn = _build_pre_migration_4_db(db_path)
    _insert_legacy_message(conn, "msg_broken", feedback_parse_status="PARSED")
    conn.close()

    conn = db.connect(db_path)
    pending_ids = {row["message_id"] for row in db.get_messages_pending_feedback_parse(conn)}
    assert "msg_broken" in pending_ids
    conn.close()


def test_migration_4_repair_is_safe_to_rerun(tmp_path):
    """Rerunning migrations must be idempotent: re-executing migration 4's
    own SQL a second time changes nothing further, and reconnecting
    through the normal db.connect() path (which no-ops once user_version
    already reflects every migration) leaves everything exactly as it was.
    """
    db_path = tmp_path / "rerun.db"
    conn = _build_pre_migration_4_db(db_path)
    _insert_legacy_message(conn, "msg_broken", feedback_parse_status="PARSED")
    _insert_legacy_message(conn, "msg_good", feedback_parse_status="PARSED")
    _insert_legacy_feedback_row(conn, "msg_good")
    conn.close()

    conn = db.connect(db_path)  # applies migrations 4 and 5 for the first time

    # Re-running migration 4's exact SQL a second time must be a no-op.
    conn.executescript(db.MIGRATIONS[3])

    assert db.get_message(conn, "msg_broken")["feedback_parse_status"] == "UNPARSED"
    assert db.get_message(conn, "msg_good")["feedback_parse_status"] == "PARSED"
    assert len(db.get_feedback_events_for_message(conn, "msg_good")) == 1
    conn.close()

    # Reconnecting (the normal, real-world path) is also a safe no-op.
    conn = db.connect(db_path)
    assert db.get_message(conn, "msg_broken")["feedback_parse_status"] == "UNPARSED"
    assert db.get_message(conn, "msg_good")["feedback_parse_status"] == "PARSED"
    assert len(db.get_feedback_events_for_message(conn, "msg_good")) == 1
    conn.close()


def test_find_feedback_invariant_violations_detects_parsed_without_feedback(tmp_path):
    """The ongoing health check (used by `status`) must catch this class of
    bug even outside of the one-time migration repair -- e.g. if a future
    bug ever reintroduces the same impossible state on a fully-migrated
    database.
    """
    conn = db.connect(tmp_path / "ideas.db")
    conn.execute(
        "INSERT INTO messages_raw (message_id, received_at, sender, subject, body_raw, "
        "body_format, stored_at, feedback_parse_status) VALUES "
        "('msg_bad', '2026-01-01T00:00:00+00:00', 'brad@example.com', 'An idea', 'LIKE.', "
        "'text', '2026-01-01T00:00:05+00:00', 'PARSED')"
    )
    conn.commit()

    violations = db.find_feedback_invariant_violations(conn)
    assert len(violations) == 1
    assert "msg_bad" in violations[0]
    conn.close()


def test_find_feedback_invariant_violations_is_empty_for_a_healthy_database(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    db.insert_message(conn, **make_message_kwargs(message_id="msg_1"))
    assert db.find_feedback_invariant_violations(conn) == []
    conn.close()


def _build_pre_migration_6_db(db_path) -> sqlite3.Connection:
    """Build a database schema-identical to one that has had migrations
    1-5 applied but not migration 6 -- exactly the live state in which a
    message could be marked PARSED despite the parser having returned zero
    feedback events (the second real-world state-invariant bug).
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    for migration_sql in db.MIGRATIONS[:5]:
        conn.executescript(migration_sql)
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    return conn


def test_migration_6_repairs_parsed_with_zero_feedback_rows_to_no_feedback(tmp_path):
    """5: existing bad PARSED/0-feedback rows migrate to NO_FEEDBACK."""
    db_path = tmp_path / "buggy_no_feedback.db"
    conn = _build_pre_migration_6_db(db_path)
    _insert_legacy_message(conn, "msg_no_feedback", feedback_parse_status="PARSED")
    conn.close()

    conn = db.connect(db_path)  # applies migration 6

    row = db.get_message(conn, "msg_no_feedback")
    assert row["feedback_parse_status"] == "NO_FEEDBACK"
    assert row["error"] is None
    conn.close()


def test_migration_6_leaves_legitimate_parsed_with_feedback_untouched(tmp_path):
    db_path = tmp_path / "healthy_no_feedback.db"
    conn = _build_pre_migration_6_db(db_path)
    _insert_legacy_message(conn, "msg_good", feedback_parse_status="PARSED")
    _insert_legacy_feedback_row(conn, "msg_good", verdict="LIKE")
    conn.close()

    conn = db.connect(db_path)

    row = db.get_message(conn, "msg_good")
    assert row["feedback_parse_status"] == "PARSED"
    events = db.get_feedback_events_for_message(conn, "msg_good")
    assert len(events) == 1
    assert events[0]["verdict"] == "LIKE"
    conn.close()


def test_no_feedback_status_with_zero_rows_produces_no_integrity_warning(tmp_path):
    """6: NO_FEEDBACK with zero feedback rows is valid -- it's the
    definition of the status, not a bug.
    """
    conn = db.connect(tmp_path / "ideas.db")
    db.insert_message(conn, **make_message_kwargs(message_id="msg_1"))
    db.set_feedback_parse_status(conn, "msg_1", "NO_FEEDBACK")

    assert db.find_feedback_invariant_violations(conn) == []
    conn.close()


def test_no_feedback_status_with_feedback_rows_is_flagged_as_inconsistent(tmp_path):
    """6: NO_FEEDBACK with one or more feedback rows should never happen
    and must be flagged.
    """
    conn = db.connect(tmp_path / "ideas.db")
    db.insert_message(conn, **make_message_kwargs(message_id="msg_1"))
    db.insert_feedback(
        conn,
        message_id="msg_1",
        event_type="FEEDBACK",
        verdict="LIKE",
        ticker=None,
        company=None,
        novelty=None,
        user_comment="x",
        parsed_json="{}",
        parser_version="feedback-v1",
        model_name="claude-haiku-4-5",
        confidence=0.9,
        created_at="2026-01-01T00:00:00+00:00",
    )
    db.set_feedback_parse_status(conn, "msg_1", "NO_FEEDBACK")

    violations = db.find_feedback_invariant_violations(conn)
    assert len(violations) == 1
    assert "msg_1" in violations[0]
    conn.close()
