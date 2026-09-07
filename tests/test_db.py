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
