"""Tests for Migration 12 (Stage 9): digest delivery provenance
(digest_deliveries / digest_delivery_items) and feedback holdout-
eligibility provenance (feedback_origin / holdout_eligible / source_id /
digest_delivery_id).

Follows the same pattern as the existing migration-4/6 tests in
tests/test_db.py: build a database schema-identical to one that has had
only migrations 1-11 applied (by directly executing db.MIGRATIONS[:11]
and setting PRAGMA user_version = 11), insert data using that OLD schema,
then reconnect through db.connect() (the app's normal path) to prove
Migration 12 applies correctly, backfills existing rows to exactly
today's behavior, and is idempotent on repeated connects. Never touches
the real production database -- everything here is an in-memory or
tmp_path-only SQLite file.
"""

import sqlite3

from ideascout import db


def _build_pre_migration_12_db(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    for migration_sql in db.MIGRATIONS[:11]:
        conn.executescript(migration_sql)
    conn.execute("PRAGMA user_version = 11")
    conn.commit()
    return conn


def _insert_pre_migration_message_and_feedback(conn, message_id: str, feedback_id_hint: str) -> None:
    conn.execute(
        """
        INSERT INTO messages_raw (
            message_id, inbox_id, thread_id, received_at, sender, recipients,
            subject, body_raw, body_format, size_bytes, stored_at,
            feedback_parse_status
        ) VALUES (?, 'inbox_abc', 'thread_abc', '2026-01-01T00:00:00+00:00',
            'brad@example.com', 'ideas@yourdomain.agentmail.to', 'An idea',
            'LIKE.', 'text', 100, '2026-01-01T00:00:05+00:00', 'PARSED')
        """,
        (message_id,),
    )
    conn.execute(
        """
        INSERT INTO feedback (
            message_id, event_index, event_type, verdict, ticker, company,
            novelty, user_comment, parsed_json, parser_version, model_name,
            confidence, created_at, excluded_from_learning
        ) VALUES (?, 0, 'FEEDBACK', 'LIKE', ?, NULL, 'UNKNOWN', 'Looks interesting.',
            '{}', 'v1', 'claude-haiku-4-5', 0.9, '2026-01-01T00:00:00+00:00', 0)
        """,
        (message_id, feedback_id_hint),
    )
    conn.commit()


def test_migration_12_adds_new_tables_and_reaches_current_user_version(tmp_path):
    db_path = tmp_path / "ideas.db"
    conn = _build_pre_migration_12_db(db_path)
    conn.close()

    conn = db.connect(db_path)  # applies migration 12
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    assert "digest_deliveries" in tables
    assert "digest_delivery_items" in tables

    user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert user_version == len(db.MIGRATIONS)
    conn.close()


def test_migration_12_backfills_existing_feedback_rows_to_direct_and_holdout_eligible(tmp_path):
    db_path = tmp_path / "ideas.db"
    conn = _build_pre_migration_12_db(db_path)
    _insert_pre_migration_message_and_feedback(conn, "msg_pre_existing", "XYZ")
    conn.close()

    conn = db.connect(db_path)  # applies migration 12
    row = conn.execute("SELECT * FROM feedback WHERE message_id = 'msg_pre_existing'").fetchone()
    assert row["feedback_origin"] == "DIRECT"
    assert row["holdout_eligible"] == 1
    assert row["source_id"] is None
    assert row["digest_delivery_id"] is None
    # Nothing about the pre-existing row's own data was lost or rewritten.
    assert row["verdict"] == "LIKE"
    assert row["ticker"] == "XYZ"
    assert row["user_comment"] == "Looks interesting."
    conn.close()


def test_migration_12_is_idempotent_across_repeated_connects(tmp_path):
    db_path = tmp_path / "ideas.db"
    conn = _build_pre_migration_12_db(db_path)
    _insert_pre_migration_message_and_feedback(conn, "msg_pre_existing", "XYZ")
    conn.close()

    conn = db.connect(db_path)  # applies migration 12 the first time
    conn.close()
    conn = db.connect(db_path)  # must be a safe no-op
    conn.close()
    conn = db.connect(db_path)

    row = conn.execute("SELECT * FROM feedback WHERE message_id = 'msg_pre_existing'").fetchone()
    assert row["feedback_origin"] == "DIRECT"
    assert row["holdout_eligible"] == 1

    count = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    assert count == 1  # migration 12 never duplicates or re-inserts existing rows

    user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert user_version == len(db.MIGRATIONS)
    conn.close()


def test_migration_12_provider_message_id_unique_allows_multiple_nulls(tmp_path):
    """SQLite treats multiple NULLs in a UNIQUE column as distinct -- this
    is exactly what lets db.record_digest_delivery leave
    provider_message_id NULL for more than one delivery (e.g. an AgentMail
    response that omits it) without ever colliding.
    """
    db_path = tmp_path / "ideas.db"
    conn = db.connect(db_path)

    delivery_id_1 = db.record_digest_delivery(
        conn,
        provider_message_id=None,
        provider_thread_id="thread_1",
        idempotency_key="idem-1",
        subject="subject",
        sent_at="2026-01-01T00:00:00+00:00",
        taste_version=1,
        source_ids=[],
    )
    delivery_id_2 = db.record_digest_delivery(
        conn,
        provider_message_id=None,
        provider_thread_id="thread_2",
        idempotency_key="idem-2",
        subject="subject",
        sent_at="2026-01-01T00:00:00+00:00",
        taste_version=1,
        source_ids=[],
    )
    assert delivery_id_1 != delivery_id_2
    conn.close()


def test_migration_12_never_loses_existing_messages_raw_rows(tmp_path):
    db_path = tmp_path / "ideas.db"
    conn = _build_pre_migration_12_db(db_path)
    _insert_pre_migration_message_and_feedback(conn, "msg_a", "AAA")
    _insert_pre_migration_message_and_feedback(conn, "msg_b", "BBB")
    conn.close()

    conn = db.connect(db_path)
    message_count = conn.execute("SELECT COUNT(*) FROM messages_raw").fetchone()[0]
    feedback_count = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    assert message_count == 2
    assert feedback_count == 2
    conn.close()
