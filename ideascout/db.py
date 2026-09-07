"""SQLite access for IdeaScout.

Everything about the schema lives here: how the database is created, how it
is safely upgraded later (migrations), and the handful of read/write
functions the rest of the app needs. Keeping all SQL in one file makes it
easy to see the entire data model at a glance and easy to change later.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Each entry is one migration, applied in order, exactly once per database.
# PRAGMA user_version tracks how many have been applied. To evolve the
# schema later, ADD a new string to the end of this list -- never edit or
# remove an existing one, or existing databases will get out of sync with
# what this code expects.
MIGRATIONS: list[str] = [
    # Migration 1: initial schema.
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

    CREATE TABLE app_meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    """,
]


def connect(database_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the database and bring its schema up to date."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(database_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    for index in range(current_version, len(MIGRATIONS)):
        conn.executescript(MIGRATIONS[index])
        conn.execute(f"PRAGMA user_version = {index + 1}")
        conn.commit()


def get_all_message_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT message_id FROM messages_raw").fetchall()
    return {row["message_id"] for row in rows}


def insert_message(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    inbox_id: str | None,
    thread_id: str | None,
    received_at: str,
    sender: str | None,
    recipients: str | None,
    subject: str | None,
    body_raw: str | None,
    body_format: str,
    size_bytes: int | None,
    stored_at: str,
) -> bool:
    """Insert one raw message. Returns True if a new row was written.

    Uses INSERT OR IGNORE against the UNIQUE(message_id) constraint so that
    duplicate inserts are always rejected at the database level -- this is
    what actually guarantees idempotency, not just the caller checking
    first. Returns False (no error) if the message was already present.

    IMPORTANT: SQLite's OR IGNORE suppresses *every* constraint violation,
    not just the UNIQUE one we want -- including NOT NULL. If a write is
    skipped for any other reason, that is a real bug (bad data from the
    caller) and must not be swallowed silently, so it is re-checked below
    and raised explicitly.
    """
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO messages_raw (
            message_id, inbox_id, thread_id, received_at,
            sender, recipients, subject,
            body_raw, body_format, size_bytes,
            stored_at, parse_status, error
        ) VALUES (
            :message_id, :inbox_id, :thread_id, :received_at,
            :sender, :recipients, :subject,
            :body_raw, :body_format, :size_bytes,
            :stored_at, 'UNPARSED', NULL
        )
        """,
        {
            "message_id": message_id,
            "inbox_id": inbox_id,
            "thread_id": thread_id,
            "received_at": received_at,
            "sender": sender,
            "recipients": recipients,
            "subject": subject,
            "body_raw": body_raw,
            "body_format": body_format,
            "size_bytes": size_bytes,
            "stored_at": stored_at,
        },
    )
    conn.commit()

    if cursor.rowcount > 0:
        return True

    already_present = conn.execute(
        "SELECT 1 FROM messages_raw WHERE message_id = ?", (message_id,)
    ).fetchone()
    if already_present:
        return False

    raise sqlite3.IntegrityError(
        f"Failed to insert message_id={message_id!r}: a required field was "
        "missing or invalid, and no row exists for it. Check for NULL "
        "values in fields marked NOT NULL in the messages_raw schema."
    )


def count_total_messages(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM messages_raw").fetchone()[0]


def count_unparsed_messages(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM messages_raw WHERE parse_status = 'UNPARSED'"
    ).fetchone()[0]


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
