"""Tests for the production-database safety mechanisms: `init`,
open_production_database's fail-loud behavior, and automatic backups.

All of this exists because of a real incident: earlier development reused
the production database's default path for manual smoke-testing, and a
routine "clean up my test files" step ended up deleting real production
data with no warning. These tests prove the replacement design actually
prevents that class of mistake.
"""

from __future__ import annotations

import sqlite3

from ideascout import cli, db
from ideascout.config import Config


def make_config(tmp_path) -> Config:
    """Deliberately does NOT pre-create the database -- these tests are
    specifically about what happens when it's missing.
    """
    return Config(
        agentmail_api_key="fake-key",
        agentmail_inbox_id="inbox_abc",
        database_path=tmp_path / "ideas.db",
        log_path=tmp_path / "app.log",
    )


# --- init: only works for a genuinely new installation ----------------------


def test_init_creates_state_dir_backups_dir_and_database(tmp_path):
    config = make_config(tmp_path)
    assert not config.database_path.exists()

    exit_code = cli.cmd_init(config)
    assert exit_code == 0

    assert config.database_path.exists()
    assert (config.database_path.parent / "backups").exists()

    conn = sqlite3.connect(str(config.database_path))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    conn.close()


def test_init_refuses_to_overwrite_an_existing_database(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    db.insert_message(
        conn,
        message_id="msg_1",
        inbox_id="i",
        thread_id="t",
        received_at="2026-01-01T00:00:00+00:00",
        sender="brad@example.com",
        recipients="x",
        subject="Original",
        body_raw="Original body",
        body_format="text",
        size_bytes=1,
        stored_at="2026-01-01T00:00:01+00:00",
    )
    conn.close()

    exit_code = cli.cmd_init(config)
    assert exit_code == 1

    # The existing database and its data must be completely untouched.
    conn = db.connect(config.database_path)
    row = conn.execute("SELECT * FROM messages_raw WHERE message_id = 'msg_1'").fetchone()
    assert row["subject"] == "Original"
    assert row["body_raw"] == "Original body"
    assert db.count_total_messages(conn) == 1
    conn.close()


def test_init_is_reported_clearly(tmp_path, capsys):
    config = make_config(tmp_path)
    cli.cmd_init(config)
    output = capsys.readouterr().out
    assert "Initialized a new production database" in output
    assert str(config.database_path) in output


# --- normal commands refuse to silently create a missing production DB -----


def test_open_production_database_refuses_when_missing_no_history(tmp_path):
    config = make_config(tmp_path)
    try:
        db.open_production_database(config.database_path)
        assert False, "should have raised"
    except db.ProductionDatabaseMissingError as exc:
        assert "python run.py init" in str(exc)


def test_cmd_status_refuses_when_database_missing(tmp_path, capsys):
    config = make_config(tmp_path)
    exit_code = cli.cmd_status(config)
    assert exit_code != 0
    output = capsys.readouterr().out
    assert "Database reachable: NO" in output
    assert "init" in output


def test_cmd_check_mail_refuses_when_database_missing_and_never_calls_agentmail(
    tmp_path, monkeypatch
):
    from ideascout import agentmail_client

    def fail_if_called(*args, **kwargs):
        raise AssertionError("AgentMail must never be contacted when the DB is missing")

    monkeypatch.setattr(agentmail_client, "build_client", fail_if_called)
    monkeypatch.setattr(agentmail_client, "list_all_message_items", fail_if_called)

    config = make_config(tmp_path)
    try:
        cli.cmd_check_mail(config)
        assert False, "should have raised"
    except db.ProductionDatabaseMissingError:
        pass


def test_open_production_database_detects_possible_data_loss_when_file_missing(tmp_path):
    """A database that previously had data, and has now vanished entirely,
    must be refused with a clear data-loss message -- not silently
    recreated as an empty database.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    db.insert_message(
        conn,
        message_id="msg_1",
        inbox_id="i",
        thread_id="t",
        received_at="2026-01-01T00:00:00+00:00",
        sender="brad@example.com",
        recipients="x",
        subject="s",
        body_raw="b",
        body_format="text",
        size_bytes=1,
        stored_at="2026-01-01T00:00:01+00:00",
    )
    conn.close()

    # Establish sentinel evidence via a normal open.
    conn = db.open_production_database(config.database_path)
    conn.close()

    # Simulate the database file vanishing.
    config.database_path.unlink()

    try:
        db.open_production_database(config.database_path)
        assert False, "should have raised"
    except db.ProductionDatabaseMissingError as exc:
        message = str(exc)
        assert "POSSIBLE DATA LOSS" in message
        assert "1 message" in message
        assert "Do NOT run 'init'" in message


def test_open_production_database_detects_message_count_drop(tmp_path):
    """Even if the file still exists, a drop in total message count vs.
    the last known-good count must be treated as possible data loss --
    messages_raw only ever grows under normal operation.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    for i in range(3):
        db.insert_message(
            conn,
            message_id=f"msg_{i}",
            inbox_id="i",
            thread_id="t",
            received_at="2026-01-01T00:00:00+00:00",
            sender="brad@example.com",
            recipients="x",
            subject="s",
            body_raw="b",
            body_format="text",
            size_bytes=1,
            stored_at="2026-01-01T00:00:01+00:00",
        )
    conn.close()

    conn = db.open_production_database(config.database_path)  # sentinel now says 3
    conn.close()

    # Simulate rows disappearing some other way (should never happen under
    # normal app operation, but the safety check must catch it anyway).
    conn = sqlite3.connect(str(config.database_path))
    conn.execute("DELETE FROM messages_raw WHERE message_id = 'msg_0'")
    conn.commit()
    conn.close()

    try:
        db.open_production_database(config.database_path)
        assert False, "should have raised"
    except db.ProductionDatabaseMissingError as exc:
        assert "only 2 message" in str(exc)


def test_open_production_database_lists_backup_filenames_but_does_not_restore(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    db.insert_message(
        conn,
        message_id="msg_1",
        inbox_id="i",
        thread_id="t",
        received_at="2026-01-01T00:00:00+00:00",
        sender="brad@example.com",
        recipients="x",
        subject="s",
        body_raw="b",
        body_format="text",
        size_bytes=1,
        stored_at="2026-01-01T00:00:01+00:00",
    )
    db.maybe_create_routine_backup(conn, config.database_path)
    conn.close()

    conn = db.open_production_database(config.database_path)
    conn.close()

    config.database_path.unlink()

    try:
        db.open_production_database(config.database_path)
        assert False, "should have raised"
    except db.ProductionDatabaseMissingError as exc:
        message = str(exc)
        assert "_routine.db" in message  # filename reported
    # The database must NOT have been recreated/restored by the failed call.
    assert not config.database_path.exists()


def test_open_production_database_succeeds_on_healthy_database_and_updates_sentinel(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    db.insert_message(
        conn,
        message_id="msg_1",
        inbox_id="i",
        thread_id="t",
        received_at="2026-01-01T00:00:00+00:00",
        sender="brad@example.com",
        recipients="x",
        subject="s",
        body_raw="b",
        body_format="text",
        size_bytes=1,
        stored_at="2026-01-01T00:00:01+00:00",
    )
    conn.close()

    conn = db.open_production_database(config.database_path)
    assert db.count_total_messages(conn) == 1
    conn.close()

    sentinel = db._read_sentinel(config.database_path)
    assert sentinel["message_count"] == 1


# --- backups are created correctly ------------------------------------------


def test_premigration_backup_is_a_correct_point_in_time_snapshot(tmp_path):
    db_path = tmp_path / "ideas.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    for migration_sql in db.MIGRATIONS[:5]:
        conn.executescript(migration_sql)
    conn.execute("PRAGMA user_version = 5")
    conn.execute(
        "INSERT INTO messages_raw (message_id, received_at, sender, subject, body_raw, "
        "body_format, stored_at) VALUES ('m1','2026-01-01T00:00:00+00:00',"
        "'brad@example.com','s','b','text','2026-01-01T00:00:01+00:00')"
    )
    conn.commit()
    conn.close()

    db.connect(db_path)  # applies migrations 6+, should back up first

    backups = list((tmp_path / "backups").glob("*_premigration.db"))
    assert len(backups) == 1

    backup_conn = sqlite3.connect(str(backups[0]))
    assert backup_conn.execute("PRAGMA user_version").fetchone()[0] == 5
    assert backup_conn.execute("SELECT COUNT(*) FROM messages_raw").fetchone()[0] == 1
    backup_conn.close()


def test_no_premigration_backup_for_a_brand_new_database(tmp_path):
    db_path = tmp_path / "ideas.db"
    db.connect(db_path)  # first-ever connection: nothing to lose, no backup
    assert not (tmp_path / "backups").exists() or not list(
        (tmp_path / "backups").glob("*_premigration.db")
    )


def test_routine_backup_is_rate_limited_to_once_per_day(tmp_path):
    db_path = tmp_path / "ideas.db"
    conn = db.connect(db_path)

    first = db.maybe_create_routine_backup(conn, db_path)
    second = db.maybe_create_routine_backup(conn, db_path)
    conn.close()

    assert first is not None
    assert second is None
    assert len(list((tmp_path / "backups").glob("*_routine.db"))) == 1


def test_routine_backup_retention_keeps_only_the_most_recent_14():
    import tempfile
    from pathlib import Path

    tmp_path = Path(tempfile.mkdtemp())
    db_path = tmp_path / "ideas.db"
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()

    for i in range(20):
        (backups_dir / f"ideas_202601{i:02d}T000000Z_routine.db").write_text("x")
    for i in range(3):
        (backups_dir / f"ideas_202602{i:02d}T000000Z_premigration.db").write_text("x")

    db._prune_routine_backups(db_path, keep=14)

    assert len(list(backups_dir.glob("*_routine.db"))) == 14
    assert len(list(backups_dir.glob("*_premigration.db"))) == 3  # never pruned


# --- production .env is never modified ---------------------------------------


def test_application_never_writes_to_env_path(tmp_path, monkeypatch):
    """A representative sweep of operations that touch config/db -- none
    of them should ever write to, move, or delete .env. load_config()
    only ever reads it (via python-dotenv); nothing else in the app opens
    ENV_PATH at all.
    """
    from ideascout import config as config_module

    env_path = tmp_path / "env_dir" / ".env"
    env_path.parent.mkdir()
    env_path.write_text("AGENTMAIL_API_KEY=real-secret\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "ENV_PATH", env_path)
    original_content = env_path.read_text(encoding="utf-8")
    original_mtime = env_path.stat().st_mtime

    config_module.load_config()
    cli.cmd_status(make_config(tmp_path / "status_check"))
    cli.cmd_init(make_config(tmp_path / "fresh_init"))

    assert env_path.read_text(encoding="utf-8") == original_content
    assert env_path.stat().st_mtime == original_mtime
