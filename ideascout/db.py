"""SQLite access for IdeaScout.

Everything about the schema lives here: how the database is created, how it
is safely upgraded later (migrations), and the handful of read/write
functions the rest of the app needs. Keeping all SQL in one file makes it
easy to see the entire data model at a glance and easy to change later.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
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
    # Migration 2 (Stage 2): derived structured feedback. This does NOT
    # touch messages_raw -- the raw email stays exactly as Stage 1 left it.
    # feedback.parsed_json keeps the complete LLM output so the row can be
    # regenerated later if the schema or parser improves; parser_version
    # and model_name record which parser produced it.
    """
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
    """,
    # Migration 3 (Stage 2 robustness pass): three changes.
    #
    # (a) A message can now produce zero, one, or several feedback events
    #     (e.g. one reply covering three separate ideas), so feedback's
    #     UNIQUE(message_id) becomes UNIQUE(message_id, event_index).
    #     SQLite can't drop/change a constraint in place, so the table is
    #     rebuilt: create the new shape, copy every existing row across as
    #     event_index 0 (nothing is lost), drop the old table, rename.
    #
    # (b) messages_raw.parse_status was Stage 2's only status column, but
    #     a future stage (source-document extraction) will need its own
    #     independent status on the same row. Splitting a feedback-specific
    #     feedback_parse_status off now avoids the two concepts colliding
    #     later. parse_status itself is untouched (still written by
    #     insert_message exactly as in Stage 1) -- its current values are
    #     just copied forward as the starting point for feedback_parse_status
    #     so already-parsed messages aren't silently re-queued and re-billed.
    #     attempt_count/last_attempt_at support automatic retry of
    #     transient (RETRYABLE_ERROR) failures.
    #
    # (c) No new column needed for the Brad-sender allowlist -- messages_raw
    #     already has `sender`.
    """
    ALTER TABLE messages_raw ADD COLUMN feedback_parse_status TEXT NOT NULL DEFAULT 'UNPARSED';
    ALTER TABLE messages_raw ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE messages_raw ADD COLUMN last_attempt_at TEXT;

    UPDATE messages_raw SET feedback_parse_status = parse_status;

    CREATE INDEX idx_messages_raw_feedback_parse_status ON messages_raw(feedback_parse_status);

    CREATE TABLE feedback_new (
        feedback_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id      TEXT NOT NULL REFERENCES messages_raw(message_id),
        event_index     INTEGER NOT NULL DEFAULT 0,
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
        created_at      TEXT NOT NULL,
        UNIQUE(message_id, event_index)
    );

    INSERT INTO feedback_new (
        feedback_id, message_id, event_index, event_type, verdict, ticker,
        company, novelty, user_comment, parsed_json, parser_version,
        model_name, confidence, created_at
    )
    SELECT
        feedback_id, message_id, 0, event_type, verdict, ticker,
        company, novelty, user_comment, parsed_json, parser_version,
        model_name, confidence, created_at
    FROM feedback;

    DROP TABLE feedback;
    ALTER TABLE feedback_new RENAME TO feedback;

    CREATE INDEX idx_feedback_event_type ON feedback(event_type);
    CREATE INDEX idx_feedback_message_id ON feedback(message_id);
    """,
    # Migration 4: repair a real-world data bug introduced by migration 3's
    # backfill.
    #
    # Migration 3 bootstrapped feedback_parse_status with
    # "UPDATE messages_raw SET feedback_parse_status = parse_status",
    # trusting the legacy parse_status column to still accurately describe
    # feedback-parsing state. In practice, a message could carry
    # parse_status = 'PARSED' (set by an earlier, pre-split build of
    # parse-mail, from back when Stage 1's parse_status and Stage 2's
    # feedback status were still the same column) without a matching row
    # ever existing in `feedback` -- for example if `feedback` was cleared
    # or rebuilt independently of messages_raw at some point. The
    # migration 3 backfill then carried that stale 'PARSED' forward into
    # feedback_parse_status, producing an impossible state (PARSED with
    # zero feedback rows) that parse-mail can never self-heal: PARSED
    # messages are never reconsidered, so the message was stuck forever
    # even though it had no actual feedback to show for it.
    #
    # This migration is a one-time, forward-only repair, not a new
    # ongoing rule: it resets feedback_parse_status back to UNPARSED for
    # any message currently claiming PARSED or NEEDS_REVIEW that has no
    # feedback rows to back that claim up, so it becomes eligible for a
    # genuine parse-mail attempt again. NEEDS_REVIEW is included on the
    # same logic as PARSED -- both statuses are only ever supposed to be
    # set immediately after that message's feedback row(s) are committed,
    # so zero feedback rows is just as impossible for one as the other.
    #
    # A message with one or more feedback rows is completely untouched no
    # matter what its status is -- this is what guarantees a legitimately
    # parsed message (or a message going through a real NEEDS_REVIEW) is
    # never reset. ERROR, RETRYABLE_ERROR, UNPARSED, and SKIPPED_NOT_BRAD
    # are also left alone regardless of feedback row count, since having
    # zero feedback rows is normal and expected for all four of those.
    # Nothing here (or anywhere else in Stage 2) ever touches messages_raw's
    # raw email fields (subject/body_raw/sender/received_at/...).
    #
    # feedback_parse_status is not derived from parse_status again after
    # this: parse_status is Stage 1's own column, kept only for backward
    # compatibility, and no Stage 2 code reads it -- migration 3's backfill
    # was a one-time historical bootstrap, not a mechanism this app relies
    # on afterward.
    """
    UPDATE messages_raw
    SET feedback_parse_status = 'UNPARSED',
        error = NULL
    WHERE feedback_parse_status IN ('PARSED', 'NEEDS_REVIEW')
      AND message_id NOT IN (SELECT DISTINCT message_id FROM feedback);
    """,
    # Migration 5 (Stage 2 security pass): record whether AgentMail
    # reported each message as authenticated (passed SPF/DKIM/DMARC) at
    # the time it was captured, so parse-mail can require both an approved
    # Brad sender AND authentication before treating anything as Brad's
    # own feedback.
    #
    # No default value is given, so every existing row gets NULL --
    # "unknown" -- rather than a guessed TRUE/FALSE. That is deliberate:
    # authentication state for messages captured before this column
    # existed is genuinely unknown, and inventing a value for them (in
    # either direction) would be worse than admitting we don't know.
    # cli.py treats NULL the same as an explicit failure (not
    # authenticated) when deciding whether to run the feedback parser --
    # i.e. "unknown" never gets the benefit of the doubt.
    """
    ALTER TABLE messages_raw ADD COLUMN sender_authenticated TEXT;
    """,
    # Migration 6: repair a second real-world state-invariant bug.
    #
    # parse-mail used to mark a message PARSED whenever the LLM call
    # succeeded, even if the parser returned zero feedback events (e.g. a
    # purely informational email from Brad with no explicit verdict/new
    # idea/missed idea in it). That violates the same rule migration 4
    # already enforces retroactively: PARSED must mean at least one
    # feedback row was actually committed. cli.py now marks this case
    # NO_FEEDBACK instead (see cmd_parse_mail) -- this migration is the
    # one-time repair for messages already wrongly marked PARSED by the
    # old behavior before that fix shipped.
    #
    # Unlike migration 4's repair (which resets to UNPARSED, because that
    # bug meant the message was never genuinely processed), this resets to
    # NO_FEEDBACK, not UNPARSED: these messages WERE genuinely, correctly
    # parsed -- the parser looked and found nothing. Resetting them to
    # UNPARSED would just waste an LLM call re-discovering the same
    # (correct) answer. A message with one or more feedback rows is
    # completely untouched no matter what -- this only ever touches the
    # exact impossible combination (PARSED + zero feedback rows), never a
    # legitimately parsed message.
    """
    UPDATE messages_raw
    SET feedback_parse_status = 'NO_FEEDBACK',
        error = NULL
    WHERE feedback_parse_status = 'PARSED'
      AND message_id NOT IN (SELECT DISTINCT message_id FROM feedback);
    """,
    # Migration 7: feedback housekeeping. Some feedback rows are known to
    # be artificial (smoke-test records, accidental duplicates a human
    # wants set aside, etc.) and should be kept for audit/history but
    # never used when a later stage learns Brad's preferences from this
    # table. excluded_from_learning defaults to 0 (false) for every
    # existing and future row -- nothing is excluded unless a human
    # explicitly marks it via `exclude-feedback`.
    """
    ALTER TABLE feedback ADD COLUMN excluded_from_learning INTEGER NOT NULL DEFAULT 0;
    """,
    # Migration 8 (Stage 3): Far View Taste versions. Each row is one
    # generated idea-taste.md / candidate-permanent-rules.md pair, with
    # exactly which feedback rows trained it (training_feedback_ids_json)
    # and where its time-forward holdout window starts
    # (checkpoint_feedback_id -- the max feedback_id among that training
    # set). The generated .md files themselves live under IdeaScoutLocal,
    # never in this repo and never in this table; this table is only the
    # reproducible record of how they were produced.
    """
    CREATE TABLE taste_versions (
        id                          INTEGER PRIMARY KEY AUTOINCREMENT,
        version_number              INTEGER NOT NULL UNIQUE,
        version_label               TEXT NOT NULL,
        generated_at                TEXT NOT NULL,
        model_name                  TEXT NOT NULL,
        training_count              INTEGER NOT NULL,
        training_feedback_ids_json  TEXT NOT NULL,
        checkpoint_feedback_id      INTEGER NOT NULL,
        idea_taste_path             TEXT NOT NULL,
        idea_taste_sha256           TEXT NOT NULL,
        candidate_rules_path        TEXT NOT NULL,
        created_at                  TEXT NOT NULL
    );

    CREATE INDEX idx_taste_versions_version_number ON taste_versions(version_number);
    """,
]


class ProductionDatabaseMissingError(RuntimeError):
    """Raised by open_production_database when the database a normal
    command needs is missing, empty, or holds fewer messages than it
    provably used to. Deliberately not auto-recoverable: the whole point
    is that nothing silently papers over this by creating a fresh replacement.
    """


class DatabaseAlreadyInitializedError(RuntimeError):
    """Raised by initialize_new_database when a file already exists at the
    target path -- `init` refuses to overwrite anything, ever.
    """


_SENTINEL_FILENAME = ".ideascout_sentinel.json"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sentinel_path(database_path: Path) -> Path:
    return database_path.parent / _SENTINEL_FILENAME


def _read_sentinel(database_path: Path) -> dict:
    """The sentinel is a small sidecar JSON file recording the last known
    message count for this database, used only to detect "this used to
    have data and now it doesn't." A missing or corrupt sentinel is
    treated as "no evidence either way" rather than an error -- it is a
    safety net, not a thing that should itself be able to break normal
    operation.
    """
    path = _sentinel_path(database_path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_sentinel(database_path: Path, state: dict) -> None:
    path = _sentinel_path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


def _backups_dir(database_path: Path) -> Path:
    return database_path.parent / "backups"


def list_backup_files(database_path: Path) -> list[Path]:
    """Every backup file for this database, newest first. Filenames are
    timestamp-prefixed, so lexicographic sort is also chronological sort.
    """
    backups_dir = _backups_dir(database_path)
    if not backups_dir.exists():
        return []
    return sorted(backups_dir.glob(f"{database_path.stem}_*.db"), reverse=True)


def _create_backup(conn: sqlite3.Connection, database_path: Path, label: str) -> Path:
    """Back up the database using SQLite's own online backup API
    (Connection.backup) rather than a filesystem copy. A plain file copy
    of an open database file can capture a half-written page if a
    transaction happens to be mid-flight; the backup API is the
    SQLite-documented way to safely copy a live database.
    """
    backups_dir = _backups_dir(database_path)
    backups_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backups_dir / f"{database_path.stem}_{timestamp}_{label}.db"
    backup_conn = sqlite3.connect(str(backup_path))
    try:
        conn.backup(backup_conn)
    finally:
        backup_conn.close()
    return backup_path


def _prune_routine_backups(database_path: Path, keep: int = 14) -> None:
    """Keep only the most recent `keep` routine backups. Pre-migration
    backups use a different filename suffix and are never touched here --
    they are kept indefinitely, since they're the ones that matter most
    for recovering from a bad migration.
    """
    backups_dir = _backups_dir(database_path)
    routine_backups = sorted(backups_dir.glob(f"{database_path.stem}_*_routine.db"))
    excess = len(routine_backups) - keep
    if excess > 0:
        for old_backup in routine_backups[:excess]:
            old_backup.unlink(missing_ok=True)


def maybe_create_routine_backup(conn: sqlite3.Connection, database_path: Path) -> Path | None:
    """Create a rolling operational backup, rate-limited to at most once
    per calendar day (UTC). Called after check-mail/parse-mail -- the two
    commands that do meaningful writes -- so routine backups accumulate
    without needing a backup on every single run. Returns the new backup's
    path, or None if today's backup already exists.
    """
    state = _read_sentinel(database_path)
    today = date.today().isoformat()
    if state.get("last_routine_backup_date") == today:
        return None
    backup_path = _create_backup(conn, database_path, "routine")
    state["last_routine_backup_date"] = today
    _write_sentinel(database_path, state)
    _prune_routine_backups(database_path)
    return backup_path


def connect(database_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the database and bring its schema up to
    date. This is the low-level primitive and WILL create a missing file
    -- tests use this directly ("tests may create temporary databases
    normally"), and `init` uses it internally after confirming no database
    already exists. Every normal CLI command instead goes through
    open_production_database, which never creates a missing file.
    """
    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(database_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate(conn, database_path)
    return conn


def _migrate(conn: sqlite3.Connection, database_path: Path) -> None:
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if 0 < current_version < len(MIGRATIONS):
        # An already-initialized database is about to have its schema
        # changed -- back it up first. A brand-new database
        # (current_version == 0, getting its very first migrations) has
        # nothing to lose yet, so no backup is made for that case.
        _create_backup(conn, database_path, "premigration")
    for index in range(current_version, len(MIGRATIONS)):
        conn.executescript(MIGRATIONS[index])
        conn.execute(f"PRAGMA user_version = {index + 1}")
        conn.commit()


def initialize_new_database(database_path: Path) -> dict:
    """Create a brand-new production database. Used ONLY by `python run.py
    init`. Refuses if a file already exists at database_path -- regardless
    of its size or contents -- which is what makes `init` impossible to
    use to accidentally wipe an existing database. Returns a small summary
    dict for the CLI to report back to the user.
    """
    if database_path.exists():
        raise DatabaseAlreadyInitializedError(
            f"A database already exists at {database_path}. Refusing to "
            "initialize over it. If you genuinely want a fresh database, "
            "move or rename the existing file yourself first."
        )

    state_dir_existed = database_path.parent.exists()
    backups_dir_existed = _backups_dir(database_path).exists()
    _backups_dir(database_path).mkdir(parents=True, exist_ok=True)

    conn = connect(database_path)  # creates the file and runs every migration
    total_messages = count_total_messages(conn)
    _write_sentinel(database_path, {"message_count": total_messages, "checked_at": _utcnow_iso()})
    conn.close()

    return {
        "state_dir_created": not state_dir_existed,
        "backups_dir_created": not backups_dir_existed,
        "schema_version": len(MIGRATIONS),
    }


def _data_loss_message(database_path: Path, previous_count: int, situation: str) -> str:
    backups = list_backup_files(database_path)
    backups_dir = _backups_dir(database_path)
    if backups:
        backup_lines = "\n".join(f"  - {p.name}" for p in backups[:20])
        backup_section = (
            f"Found {len(backups)} backup file(s) in {backups_dir}:\n{backup_lines}\n"
            "(No backup has been restored automatically.)"
        )
    else:
        backup_section = f"No backup files were found in {backups_dir}."

    return (
        "POSSIBLE DATA LOSS DETECTED.\n"
        f"The production database at {database_path} previously contained "
        f"{previous_count} message(s), but {situation}.\n"
        "Refusing to continue automatically -- this command has not run.\n\n"
        f"{backup_section}\n\n"
        "Do NOT run 'init' -- that would create a new, empty database and "
        "could make recovery harder. Investigate before proceeding."
    )


def open_production_database(database_path: Path) -> sqlite3.Connection:
    """The connection path every normal command (check-mail, parse-mail,
    status, show-feedback, requeue-feedback, exclude/include-feedback)
    must use. Unlike connect(), this never creates a missing database --
    it raises ProductionDatabaseMissingError instead:

    - if there is no sentinel evidence this database ever held data, that
      is treated as a genuinely new installation, and the message points
      to `python run.py init`;
    - if the sentinel says the database previously held N > 0 messages but
      the file is now missing, or now holds fewer messages than that, this
      is treated as possible data loss: refuse to continue, and list any
      backup files found (never auto-restore one).

    On success, updates the sentinel with the freshly observed count.
    """
    state = _read_sentinel(database_path)
    previous_count = state.get("message_count")

    if not database_path.exists():
        if previous_count:
            raise ProductionDatabaseMissingError(
                _data_loss_message(
                    database_path, previous_count, "the database file is now missing entirely"
                )
            )
        raise ProductionDatabaseMissingError(
            f"No production database found at {database_path}.\n"
            "If this is a new installation, run:\n"
            "    python run.py init\n"
            "to create one."
        )

    conn = connect(database_path)
    current_count = count_total_messages(conn)

    if previous_count and current_count < previous_count:
        conn.close()
        raise ProductionDatabaseMissingError(
            _data_loss_message(
                database_path,
                previous_count,
                f"it currently contains only {current_count} message(s)",
            )
        )

    _write_sentinel(
        database_path, {**state, "message_count": current_count, "checked_at": _utcnow_iso()}
    )
    return conn


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
    sender_authenticated: str | None = None,
) -> bool:
    """Insert one raw message. Returns True if a new row was written.

    sender_authenticated should be "AUTHENTICATED" or "UNAUTHENTICATED"
    (from AgentMail's own authentication signal, captured once at
    check-mail time -- see agentmail_client.fetch_authenticated_message_ids)
    or left as None when that isn't known (e.g. a message stored before
    this column existed).

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
            stored_at, parse_status, error, sender_authenticated
        ) VALUES (
            :message_id, :inbox_id, :thread_id, :received_at,
            :sender, :recipients, :subject,
            :body_raw, :body_format, :size_bytes,
            :stored_at, 'UNPARSED', NULL, :sender_authenticated
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
            "sender_authenticated": sender_authenticated,
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


# --- Stage 2: parsing raw messages into structured feedback -----------------
#
# NOTE on two status columns: messages_raw.parse_status is Stage 1's
# original general-purpose column and is left exactly as Stage 1 defined
# it (insert_message above still initializes it, nothing here ever writes
# to it again). All Stage 2 feedback-parsing state lives in the separate
# feedback_parse_status column instead, so a later stage (e.g. parsing
# source documents) can track its own status on the same row without the
# two concepts colliding.
#
# feedback_parse_status values used by this app:
#   UNPARSED          -- not yet attempted
#   PARSED            -- feedback parser ran; any events it found are saved
#   NEEDS_REVIEW      -- parsed, but the result (or lack of one) needs a
#                        human look; never retried automatically
#   RETRYABLE_ERROR   -- a transient failure (timeout/connection/429/5xx);
#                        eligible for another attempt on the next run
#   ERROR             -- a non-transient failure (e.g. bad credentials);
#                        NOT retried automatically -- retrying won't help
#                        until a human fixes the underlying problem
#   SKIPPED_NOT_BRAD  -- sender isn't in BRAD_ALLOWED_SENDERS; the feedback
#                        parser is never run on it


def get_messages_pending_feedback_parse(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Messages eligible for the feedback parser, oldest first: never
    attempted, or a previous attempt failed for a transient reason.
    """
    return conn.execute(
        "SELECT message_id, subject, body_raw, sender, sender_authenticated FROM messages_raw "
        "WHERE feedback_parse_status IN ('UNPARSED', 'RETRYABLE_ERROR') "
        "ORDER BY id"
    ).fetchall()


def get_message(conn: sqlite3.Connection, message_id: str) -> sqlite3.Row | None:
    """The full raw message row, or None if no such message_id exists."""
    return conn.execute(
        "SELECT * FROM messages_raw WHERE message_id = ?", (message_id,)
    ).fetchone()


def get_message_ids_by_feedback_status(conn: sqlite3.Connection, status: str) -> list[str]:
    """message_ids currently in one specific feedback_parse_status, oldest
    first. Used by `requeue-feedback --all-errors` to find every ERROR
    message.
    """
    rows = conn.execute(
        "SELECT message_id FROM messages_raw WHERE feedback_parse_status = ? ORDER BY id",
        (status,),
    ).fetchall()
    return [row["message_id"] for row in rows]


def get_messages_by_feedback_status(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    """Full raw-message rows currently in one specific feedback_parse_status,
    oldest first. Used by `show-review` to display NEEDS_REVIEW messages
    with enough context (subject, sender, received_at) for a human to
    recognize which email each one is.
    """
    return conn.execute(
        "SELECT * FROM messages_raw WHERE feedback_parse_status = ? ORDER BY id",
        (status,),
    ).fetchall()


def get_feedback_eligible_for_learning(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Feedback rows a future taste-learning stage is allowed to use.

    A row is eligible only if BOTH:
      - it has not been manually excluded (excluded_from_learning = 0), and
      - the message it came from has been fully approved
        (feedback_parse_status = 'PARSED').

    Critically, a feedback row belonging to a NEEDS_REVIEW message is
    NEVER eligible, no matter how confident-looking its content is --
    only `approve-review` (which flips the message to PARSED without
    touching the feedback row itself) can make it eligible. This is the
    one canonical query any future learning stage should use; do not
    reimplement this filter elsewhere.
    """
    return conn.execute(
        """
        SELECT feedback.*
        FROM feedback
        JOIN messages_raw ON messages_raw.message_id = feedback.message_id
        WHERE feedback.excluded_from_learning = 0
          AND messages_raw.feedback_parse_status = 'PARSED'
        ORDER BY feedback.feedback_id
        """
    ).fetchall()


def record_parse_attempt(conn: sqlite3.Connection, message_id: str, attempted_at: str) -> None:
    """Record that a feedback-parse attempt (an LLM call) happened, before
    knowing the outcome. Used for troubleshooting (how many times has this
    message been tried?) -- separate from set_feedback_parse_status so
    status-only updates (crash recovery, sender skip) don't inflate it.
    """
    conn.execute(
        "UPDATE messages_raw SET attempt_count = attempt_count + 1, last_attempt_at = ? "
        "WHERE message_id = ?",
        (attempted_at, message_id),
    )
    conn.commit()


def set_feedback_parse_status(
    conn: sqlite3.Connection, message_id: str, status: str, error: str | None = None
) -> None:
    """Update only the feedback-parsing state of a message. Never touches
    the raw email fields (subject, body_raw, sender, ...) or the legacy
    parse_status column -- those are permanent once stored.
    """
    conn.execute(
        "UPDATE messages_raw SET feedback_parse_status = ?, error = ? WHERE message_id = ?",
        (status, error, message_id),
    )
    conn.commit()


def get_feedback_message_ids(conn: sqlite3.Connection) -> set[str]:
    """message_ids that already have at least one feedback event saved."""
    rows = conn.execute("SELECT DISTINCT message_id FROM feedback").fetchall()
    return {row["message_id"] for row in rows}


def get_feedback_events_for_message(conn: sqlite3.Connection, message_id: str) -> list[sqlite3.Row]:
    """All feedback events for one message, in extraction order."""
    return conn.execute(
        "SELECT * FROM feedback WHERE message_id = ? ORDER BY event_index",
        (message_id,),
    ).fetchall()


def delete_feedback_for_message(conn: sqlite3.Connection, message_id: str) -> int:
    """Delete every feedback row for one message. Returns how many rows
    were removed.

    This is intentionally the only place in the whole app that deletes
    from `feedback`. It exists solely for cli.py's requeue-feedback on a
    NEEDS_REVIEW message: those rows are the ambiguous/low-confidence
    derived output that caused the review in the first place, not
    authoritative Brad feedback, so superseding them with a fresh parse is
    safe. Callers must ensure the message is not PARSED before calling
    this -- it has no such guard itself, since messages_raw is the source
    of truth for that decision, not this function.
    """
    cursor = conn.execute("DELETE FROM feedback WHERE message_id = ?", (message_id,))
    conn.commit()
    return cursor.rowcount


def insert_feedback(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    event_type: str,
    verdict: str | None,
    ticker: str | None,
    company: str | None,
    novelty: str | None,
    user_comment: str | None,
    parsed_json: str,
    parser_version: str,
    model_name: str,
    confidence: float | None,
    created_at: str,
    event_index: int = 0,
) -> bool:
    """Insert one feedback event. Returns True if a new row was written.

    Same INSERT OR IGNORE + explicit-recheck pattern as insert_message
    (see the comment there): UNIQUE(message_id, event_index) is what
    actually guarantees this exact event is never duplicated even if this
    function is called twice for it (e.g. a crashed run retried), and any
    other constraint failure is surfaced loudly instead of being swallowed
    by OR IGNORE.

    For inserting *all* of one message's events together, prefer
    insert_feedback_events below -- it commits them as a single
    all-or-nothing unit, which this single-row function does not.
    """
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO feedback (
            message_id, event_index, event_type, verdict, ticker, company,
            novelty, user_comment, parsed_json, parser_version, model_name,
            confidence, created_at
        ) VALUES (
            :message_id, :event_index, :event_type, :verdict, :ticker, :company,
            :novelty, :user_comment, :parsed_json, :parser_version, :model_name,
            :confidence, :created_at
        )
        """,
        {
            "message_id": message_id,
            "event_index": event_index,
            "event_type": event_type,
            "verdict": verdict,
            "ticker": ticker,
            "company": company,
            "novelty": novelty,
            "user_comment": user_comment,
            "parsed_json": parsed_json,
            "parser_version": parser_version,
            "model_name": model_name,
            "confidence": confidence,
            "created_at": created_at,
        },
    )
    conn.commit()

    if cursor.rowcount > 0:
        return True

    already_present = conn.execute(
        "SELECT 1 FROM feedback WHERE message_id = ? AND event_index = ?",
        (message_id, event_index),
    ).fetchone()
    if already_present:
        return False

    raise sqlite3.IntegrityError(
        f"Failed to insert feedback for message_id={message_id!r} "
        f"event_index={event_index}: a required field was missing or "
        "invalid, and no row exists for it."
    )


def insert_feedback_events(
    conn: sqlite3.Connection, *, message_id: str, events: list[dict]
) -> None:
    """Insert every feedback event extracted from one message as a single
    all-or-nothing unit (event_index 0, 1, 2, ... in list order).

    This matters because cli.py only calls this after confirming
    message_id has no feedback rows yet at all. If one event's insert
    failed midway through a naive per-row loop, the message would be left
    with only *some* of its events saved -- and since a later run's
    crash-recovery check only asks "does this message_id have any feedback
    rows yet", it would wrongly conclude the message was already fully
    handled and never retry the LLM call for the rest. Wrapping the whole
    batch in one transaction means a failure rolls back to zero rows for
    this message, so it is correctly retried in full next time.

    Each dict in `events` must supply the same fields as insert_feedback's
    keyword arguments, excluding message_id/event_index (filled in here).
    """
    with conn:
        for index, event in enumerate(events):
            conn.execute(
                """
                INSERT INTO feedback (
                    message_id, event_index, event_type, verdict, ticker,
                    company, novelty, user_comment, parsed_json,
                    parser_version, model_name, confidence, created_at
                ) VALUES (
                    :message_id, :event_index, :event_type, :verdict, :ticker,
                    :company, :novelty, :user_comment, :parsed_json,
                    :parser_version, :model_name, :confidence, :created_at
                )
                """,
                {"message_id": message_id, "event_index": index, **event},
            )


def count_feedback_pending_messages(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM messages_raw WHERE feedback_parse_status = 'UNPARSED'"
    ).fetchone()[0]


def count_feedback_parsed_messages(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM messages_raw WHERE feedback_parse_status = 'PARSED'"
    ).fetchone()[0]


def count_feedback_no_feedback_messages(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM messages_raw WHERE feedback_parse_status = 'NO_FEEDBACK'"
    ).fetchone()[0]


def count_feedback_needs_review_messages(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM messages_raw WHERE feedback_parse_status = 'NEEDS_REVIEW'"
    ).fetchone()[0]


def count_feedback_retryable_error_messages(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM messages_raw WHERE feedback_parse_status = 'RETRYABLE_ERROR'"
    ).fetchone()[0]


def count_feedback_error_messages(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM messages_raw WHERE feedback_parse_status = 'ERROR'"
    ).fetchone()[0]


def count_feedback_skipped_not_brad_messages(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM messages_raw WHERE feedback_parse_status = 'SKIPPED_NOT_BRAD'"
    ).fetchone()[0]


def count_feedback_skipped_unauthenticated_messages(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM messages_raw WHERE feedback_parse_status = 'SKIPPED_UNAUTHENTICATED'"
    ).fetchone()[0]


def count_feedback_records(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]


def get_latest_feedback(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    """Most recent feedback events, joined with the originating email's
    subject/date/sender for display in `show-feedback`.
    """
    return conn.execute(
        """
        SELECT
            feedback.feedback_id, feedback.event_index, feedback.event_type,
            feedback.verdict, feedback.ticker, feedback.company, feedback.novelty,
            feedback.user_comment, feedback.confidence, feedback.created_at,
            feedback.excluded_from_learning,
            messages_raw.received_at, messages_raw.sender, messages_raw.subject
        FROM feedback
        JOIN messages_raw ON messages_raw.message_id = feedback.message_id
        ORDER BY feedback.feedback_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def get_feedback_by_ticker_or_company(conn: sqlite3.Connection, value: str) -> list[sqlite3.Row]:
    """Feedback events whose ticker or company matches `value`
    case-insensitively. Used by exclude-feedback/include-feedback -- a
    single argument is matched against both columns since not every
    feedback event has a ticker filled in (e.g. some NEW_IDEA/MISSED_IDEA
    events only have a company name).
    """
    return conn.execute(
        """
        SELECT * FROM feedback
        WHERE LOWER(ticker) = LOWER(?) OR LOWER(company) = LOWER(?)
        ORDER BY feedback_id
        """,
        (value, value),
    ).fetchall()


def set_feedback_excluded_from_learning(
    conn: sqlite3.Connection, feedback_id: int, excluded: bool
) -> None:
    """Flip one feedback row's excluded_from_learning flag. Never deletes
    or otherwise modifies the row -- the raw email and the feedback record
    itself (event_type, verdict, user_comment, ...) are untouched; this
    only ever changes whether a later learning stage should use it.
    """
    conn.execute(
        "UPDATE feedback SET excluded_from_learning = ? WHERE feedback_id = ?",
        (1 if excluded else 0, feedback_id),
    )
    conn.commit()


def find_feedback_invariant_violations(conn: sqlite3.Connection) -> list[str]:
    """Look for messages_raw/feedback combinations that should be
    impossible under this app's own rules -- PARSED or NEEDS_REVIEW are
    only ever supposed to be set immediately after that message's
    feedback row(s) are committed, so zero feedback rows is impossible for
    either one, and no other status (including NO_FEEDBACK, which is
    *defined* as "parsed successfully, zero events found" and so must
    always have zero feedback rows) should ever have feedback rows at all.

    Returns a human-readable description of each violation found; an
    empty list means the database is healthy. Intended to be checked on
    every `status` run so a bug like the migration-3 one (a message stuck
    claiming PARSED with no feedback to show for it) is surfaced
    immediately instead of silently persisting.
    """
    violations: list[str] = []

    missing_feedback = conn.execute(
        """
        SELECT message_id, feedback_parse_status FROM messages_raw
        WHERE feedback_parse_status IN ('PARSED', 'NEEDS_REVIEW')
          AND message_id NOT IN (SELECT DISTINCT message_id FROM feedback)
        ORDER BY id
        """
    ).fetchall()
    for row in missing_feedback:
        violations.append(
            f"message_id={row['message_id']!r} has feedback_parse_status="
            f"{row['feedback_parse_status']!r} but zero feedback rows exist for it"
        )

    unexpected_feedback = conn.execute(
        """
        SELECT DISTINCT messages_raw.message_id, messages_raw.feedback_parse_status
        FROM messages_raw
        JOIN feedback ON feedback.message_id = messages_raw.message_id
        WHERE messages_raw.feedback_parse_status NOT IN ('PARSED', 'NEEDS_REVIEW')
        ORDER BY messages_raw.message_id
        """
    ).fetchall()
    for row in unexpected_feedback:
        violations.append(
            f"message_id={row['message_id']!r} has one or more feedback rows but "
            f"feedback_parse_status={row['feedback_parse_status']!r} "
            "(expected PARSED or NEEDS_REVIEW)"
        )

    return violations


# --- Stage 3: Far View Taste versions ---------------------------------------


def get_eligible_feedback_after(conn: sqlite3.Connection, checkpoint_feedback_id: int) -> list[sqlite3.Row]:
    """Eligible feedback rows (via the one canonical
    get_feedback_eligible_for_learning query) with feedback_id strictly
    greater than checkpoint_feedback_id -- i.e. judgments that showed up
    after a taste version's training checkpoint. This is how the holdout
    count is computed; it deliberately filters the canonical query's own
    result rather than writing a second, separate eligibility query, so
    the two can never drift apart.
    """
    eligible = get_feedback_eligible_for_learning(conn)
    return [row for row in eligible if row["feedback_id"] > checkpoint_feedback_id]


def get_latest_taste_version(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM taste_versions ORDER BY version_number DESC LIMIT 1"
    ).fetchone()


def update_taste_version_sha256(
    conn: sqlite3.Connection, *, version_number: int, idea_taste_sha256: str
) -> None:
    """Update the stored hash for an existing taste_versions row after an
    editorial correction to its idea-taste.md file on disk. Does not touch
    version_number, training_feedback_ids_json, checkpoint_feedback_id, or
    any other column -- this is purely "the file's contents changed, so the
    recorded fingerprint of it must change too," not a new generation.
    """
    conn.execute(
        "UPDATE taste_versions SET idea_taste_sha256 = :idea_taste_sha256 "
        "WHERE version_number = :version_number",
        {"idea_taste_sha256": idea_taste_sha256, "version_number": version_number},
    )
    conn.commit()


def insert_taste_version(
    conn: sqlite3.Connection,
    *,
    version_number: int,
    version_label: str,
    generated_at: str,
    model_name: str,
    training_count: int,
    training_feedback_ids_json: str,
    checkpoint_feedback_id: int,
    idea_taste_path: str,
    idea_taste_sha256: str,
    candidate_rules_path: str,
    created_at: str,
) -> None:
    """Record one generated taste version. version_number must be unique
    and increasing (1, 2, 3, ...) -- the UNIQUE constraint on it is what
    prevents two concurrent build-taste runs from ever producing two
    conflicting "v2"s.
    """
    conn.execute(
        """
        INSERT INTO taste_versions (
            version_number, version_label, generated_at, model_name,
            training_count, training_feedback_ids_json, checkpoint_feedback_id,
            idea_taste_path, idea_taste_sha256, candidate_rules_path, created_at
        ) VALUES (
            :version_number, :version_label, :generated_at, :model_name,
            :training_count, :training_feedback_ids_json, :checkpoint_feedback_id,
            :idea_taste_path, :idea_taste_sha256, :candidate_rules_path, :created_at
        )
        """,
        {
            "version_number": version_number,
            "version_label": version_label,
            "generated_at": generated_at,
            "model_name": model_name,
            "training_count": training_count,
            "training_feedback_ids_json": training_feedback_ids_json,
            "checkpoint_feedback_id": checkpoint_feedback_id,
            "idea_taste_path": idea_taste_path,
            "idea_taste_sha256": idea_taste_sha256,
            "candidate_rules_path": candidate_rules_path,
            "created_at": created_at,
        },
    )
    conn.commit()
