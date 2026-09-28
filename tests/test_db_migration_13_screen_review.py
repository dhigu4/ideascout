"""Tests for Migration 13 (Stage 10): SCREEN_REVIEW feedback provenance
(feedback.screening_id, linking a feedback row back to the EXACT
source_screenings row it judges).

Same pattern as tests/test_db_migration_12_digest_reply.py: build a
database schema-identical to one that has had only migrations 1-12
applied, insert data using that OLD schema, then reconnect through
db.connect() (the app's normal path) to prove Migration 13 applies
correctly, leaves every existing row untouched, and is idempotent on
repeated connects. Never touches the real production database.
"""

import sqlite3

from ideascout import db


def _build_pre_migration_13_db(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    for migration_sql in db.MIGRATIONS[:12]:
        conn.executescript(migration_sql)
    conn.execute("PRAGMA user_version = 12")
    conn.commit()
    return conn


def _insert_pre_migration_message_and_feedback(
    conn, message_id: str, ticker: str, feedback_origin: str = "DIRECT", holdout_eligible: int = 1
) -> None:
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
            confidence, created_at, excluded_from_learning, feedback_origin,
            holdout_eligible
        ) VALUES (?, 0, 'FEEDBACK', 'LIKE', ?, NULL, 'UNKNOWN', 'Looks interesting.',
            '{}', 'v1', 'claude-haiku-4-5', 0.9, '2026-01-01T00:00:00+00:00', 0, ?, ?)
        """,
        (message_id, ticker, feedback_origin, holdout_eligible),
    )
    conn.commit()


def test_migration_13_adds_screening_id_column_and_reaches_current_user_version(tmp_path):
    db_path = tmp_path / "ideas.db"
    conn = _build_pre_migration_13_db(db_path)
    conn.close()

    conn = db.connect(db_path)  # applies migration 13
    columns = {row[1] for row in conn.execute("PRAGMA table_info(feedback)").fetchall()}
    assert "screening_id" in columns

    user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert user_version == len(db.MIGRATIONS)
    conn.close()


def test_migration_13_leaves_existing_direct_and_digest_reply_rows_untouched(tmp_path):
    db_path = tmp_path / "ideas.db"
    conn = _build_pre_migration_13_db(db_path)
    _insert_pre_migration_message_and_feedback(conn, "msg_direct", "XYZ", "DIRECT", 1)
    _insert_pre_migration_message_and_feedback(conn, "msg_digest_reply", "ABC", "DIGEST_REPLY", 0)
    conn.close()

    conn = db.connect(db_path)  # applies migration 13
    direct_row = conn.execute("SELECT * FROM feedback WHERE message_id = 'msg_direct'").fetchone()
    digest_row = conn.execute("SELECT * FROM feedback WHERE message_id = 'msg_digest_reply'").fetchone()

    assert direct_row["feedback_origin"] == "DIRECT"
    assert direct_row["holdout_eligible"] == 1
    assert direct_row["screening_id"] is None

    assert digest_row["feedback_origin"] == "DIGEST_REPLY"
    assert digest_row["holdout_eligible"] == 0
    assert digest_row["screening_id"] is None
    conn.close()


def test_migration_13_is_idempotent_across_repeated_connects(tmp_path):
    db_path = tmp_path / "ideas.db"
    conn = _build_pre_migration_13_db(db_path)
    _insert_pre_migration_message_and_feedback(conn, "msg_direct", "XYZ")
    conn.close()

    conn = db.connect(db_path)  # applies migration 13 the first time
    conn.close()
    conn = db.connect(db_path)  # must be a safe no-op
    conn.close()
    conn = db.connect(db_path)

    count = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    assert count == 1  # never duplicated or re-inserted

    user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert user_version == len(db.MIGRATIONS)
    conn.close()


def test_migration_13_never_loses_existing_rows(tmp_path):
    db_path = tmp_path / "ideas.db"
    conn = _build_pre_migration_13_db(db_path)
    _insert_pre_migration_message_and_feedback(conn, "msg_a", "AAA")
    _insert_pre_migration_message_and_feedback(conn, "msg_b", "BBB")
    conn.close()

    conn = db.connect(db_path)
    message_count = conn.execute("SELECT COUNT(*) FROM messages_raw").fetchone()[0]
    feedback_count = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    assert message_count == 2
    assert feedback_count == 2
    conn.close()


def test_migration_13_screening_id_links_to_source_screenings_after_migration(tmp_path):
    """A fresh (post-migration) SCREEN_REVIEW-style row can populate
    screening_id and have it correctly resolved via the new FK/index.
    """
    db_path = tmp_path / "ideas.db"
    conn = _build_pre_migration_13_db(db_path)
    conn.close()

    conn = db.connect(db_path)
    source_id = db.insert_collected_source(
        conn, source_name="yellowbrick", external_id="1",
        canonical_url="https://www.joinyellowbrick.com/sp/1",
        discovered_at="2026-01-01T00:00:00+00:00", discovery_title="Abc Corp", source_date=None,
        source_title="Abc Corp", author=None, ticker="ABC", company="Abc Corp",
        source_type="stock_pitch", content_hash="hash-1", raw_html_path="/x/1.html",
        metadata_json="{}", created_at="2026-01-01T00:00:00+00:00",
    )
    screening_id = db.insert_source_screening(
        conn, source_id=source_id, created_at="2026-01-01T00:00:00+00:00",
        taste_version=2, taste_sha256="tastehash", screen_rules_path="IDEA_SCREEN_RULES.md",
        screen_rules_sha256="ruleshash", content_hash="hash-1", model_name="fake-model",
        overall_prediction="PASS", mispricing="Plausible", variant_perception="Plausible",
        upside="Potentially sufficient", business_quality="Plausible", downside="Acceptable",
        key_reasons_json="[]", key_concerns_json="[]", critical_questions_json="[]",
        confidence="MEDIUM",
    )
    db.insert_message(
        conn, message_id="msg_review", inbox_id="inbox_abc", thread_id="thread_abc",
        received_at="2026-01-02T00:00:00+00:00", sender="brad@example.com",
        recipients="ideas@yourdomain.agentmail.to", subject="IdeaScout Screen Review",
        body_raw="SR-1 - PASS\ncorrect call", body_format="text", size_bytes=10,
        stored_at="2026-01-02T00:00:05+00:00", sender_authenticated="AUTHENTICATED",
    )
    db.insert_feedback(
        conn, message_id="msg_review", event_type="FEEDBACK", verdict="PASS", ticker="ABC",
        company="Abc Corp", novelty=None, user_comment="correct call", parsed_json="{}",
        parser_version="screen-review-v1", model_name="deterministic", confidence=None,
        created_at="2026-01-02T00:00:00+00:00", feedback_origin="SCREEN_REVIEW",
        holdout_eligible=False, source_id=source_id, screening_id=screening_id,
    )

    row = conn.execute("SELECT * FROM feedback WHERE message_id = 'msg_review'").fetchone()
    assert row["screening_id"] == screening_id
    assert row["feedback_origin"] == "SCREEN_REVIEW"
    assert row["holdout_eligible"] == 0

    resolved = db.get_source_screening_by_id(conn, row["screening_id"])
    assert resolved["overall_prediction"] == "PASS"
    assert resolved["ticker"] == "ABC"
    conn.close()
