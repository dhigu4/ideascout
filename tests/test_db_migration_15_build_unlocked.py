"""Tests for Migration 15: taste_versions.build_unlocked.

Production context: Migration 14 had already been recorded as applied
(PRAGMA user_version = 14) before a later change tried to add
build_unlocked directly into Migration 14's SQL -- since MIGRATIONS
entries are immutable once shipped, that edit silently never ran in
production, leaving taste_versions without the column cmd_taste_status/
cmd_build_taste both require. Migration 14 has been restored to exactly
its shipped form; Migration 15 is the correct, additive fix.

Same pattern as the other migration test files: build a database
schema-identical to a real one at a given prior migration state, insert
data under that OLD schema, then reconnect through db.connect() (the
app's normal path) to prove the migration applies correctly, backfills
existing rows, and is idempotent on repeated connects. Never touches the
real production database.
"""

import sqlite3

from ideascout import cli, db, taste
from ideascout.config import Config
from tests.test_cli_build_taste import insert_n_eligible, make_config, patch_taste


def _build_production_like_migration_14_db(db_path) -> sqlite3.Connection:
    """Exactly reproduces the real-world broken state: Migration 14
    applied (including blind_review_assignments and feedback.blind_
    review_assignment_id), but taste_versions has NO build_unlocked
    column, and PRAGMA user_version already claims migration 14 is done.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    for migration_sql in db.MIGRATIONS[:14]:
        conn.executescript(migration_sql)
    conn.execute("PRAGMA user_version = 14")
    conn.commit()
    return conn


def _insert_taste_version(conn, version_number: int = 1, idea_taste_path: str = "/x/idea-taste.md") -> None:
    conn.execute(
        """
        INSERT INTO taste_versions (
            version_number, version_label, generated_at, model_name,
            training_count, training_feedback_ids_json, checkpoint_feedback_id,
            idea_taste_path, idea_taste_sha256, candidate_rules_path, created_at
        ) VALUES (?, ?, '2026-01-01T00:00:00+00:00', 'fake-model', 39, '[]', 39,
            ?, 'hash', '/x/candidate-permanent-rules.md',
            '2026-01-01T00:00:00+00:00')
        """,
        (version_number, f"v{version_number}", idea_taste_path),
    )
    conn.commit()


def test_migration_15_upgrades_a_production_like_migration_14_db(tmp_path):
    db_path = tmp_path / "ideas.db"
    conn = _build_production_like_migration_14_db(db_path)
    columns_before = {row[1] for row in conn.execute("PRAGMA table_info(taste_versions)").fetchall()}
    assert "build_unlocked" not in columns_before
    conn.close()

    conn = db.connect(db_path)  # applies migration 15
    columns_after = {row[1] for row in conn.execute("PRAGMA table_info(taste_versions)").fetchall()}
    assert "build_unlocked" in columns_after

    user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert user_version == len(db.MIGRATIONS)
    conn.close()


def test_migration_15_backfills_existing_taste_version_to_build_unlocked_0(tmp_path):
    db_path = tmp_path / "ideas.db"
    conn = _build_production_like_migration_14_db(db_path)
    _insert_taste_version(conn, version_number=1)
    _insert_taste_version(conn, version_number=2)
    conn.close()

    conn = db.connect(db_path)  # applies migration 15
    rows = conn.execute("SELECT version_number, build_unlocked FROM taste_versions ORDER BY version_number").fetchall()
    conn.close()

    assert [dict(r) for r in rows] == [
        {"version_number": 1, "build_unlocked": 0},
        {"version_number": 2, "build_unlocked": 0},
    ]


def test_migration_15_never_loses_existing_taste_version_data(tmp_path):
    db_path = tmp_path / "ideas.db"
    conn = _build_production_like_migration_14_db(db_path)
    _insert_taste_version(conn, version_number=1)
    conn.close()

    conn = db.connect(db_path)
    row = conn.execute("SELECT * FROM taste_versions WHERE version_number = 1").fetchone()
    conn.close()

    assert row["version_label"] == "v1"
    assert row["training_count"] == 39
    assert row["idea_taste_path"] == "/x/idea-taste.md"
    assert row["build_unlocked"] == 0


def test_fresh_db_reaches_the_same_final_schema(tmp_path):
    db_path = tmp_path / "ideas.db"
    conn = db.connect(db_path)  # applies all migrations from scratch
    columns = {row[1] for row in conn.execute("PRAGMA table_info(taste_versions)").fetchall()}
    assert "build_unlocked" in columns

    user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert user_version == len(db.MIGRATIONS)
    conn.close()


def test_migration_15_is_idempotent_across_repeated_connects(tmp_path):
    db_path = tmp_path / "ideas.db"
    conn = _build_production_like_migration_14_db(db_path)
    _insert_taste_version(conn, version_number=1)
    conn.close()

    conn = db.connect(db_path)  # applies migration 15 the first time
    conn.close()
    conn = db.connect(db_path)  # must be a safe no-op
    conn.close()
    conn = db.connect(db_path)

    count = conn.execute("SELECT COUNT(*) FROM taste_versions").fetchone()[0]
    assert count == 1

    user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert user_version == len(db.MIGRATIONS)
    conn.close()


def test_taste_status_works_after_migration_15(tmp_path, monkeypatch, capsys):
    """Direct reproduction of the reported production crash: taste-status
    must no longer raise IndexError on latest["build_unlocked"].
    """
    idea_taste_path = tmp_path / "idea-taste.md"
    idea_taste_path.write_text("## Strong Positive Signals\nFake.\n", encoding="utf-8", newline="")

    db_path = tmp_path / "ideas.db"
    conn = _build_production_like_migration_14_db(db_path)
    conn.execute(
        """
        INSERT INTO taste_versions (
            version_number, version_label, generated_at, model_name,
            training_count, training_feedback_ids_json, checkpoint_feedback_id,
            idea_taste_path, idea_taste_sha256, candidate_rules_path, created_at
        ) VALUES (1, 'v1', '2026-01-01T00:00:00+00:00', 'fake-model', 39, '[]', 39, ?, ?,
            '/x/candidate-permanent-rules.md', '2026-01-01T00:00:00+00:00')
        """,
        (str(idea_taste_path), taste.compute_file_sha256(idea_taste_path)),
    )
    conn.commit()
    conn.close()

    # Built directly (not via make_config, which would connect/migrate the
    # database itself before this test gets to exercise the upgrade path).
    config = Config(
        agentmail_api_key=None,
        agentmail_inbox_id=None,
        database_path=db_path,
        log_path=tmp_path / "app.log",
        anthropic_api_key="fake-anthropic-key",
    )

    exit_code = cli.cmd_taste_status(config)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Taste version: v1" in output
    assert "Artifact integrity: OK" in output


def test_build_taste_remains_blocked_at_20_of_20_with_build_unlocked_0(tmp_path, monkeypatch):
    """After migration 15 applies (build_unlocked defaults to 0 for every
    existing and new version), the holdout-governance behavior from the
    prior fix is intact: reaching 20/20 alone never unlocks regeneration.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()
    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    conn = db.connect(config.database_path)
    from tests.test_cli_build_taste import make_parsed_message

    for i in range(taste.HOLDOUT_SIZE):
        make_parsed_message(conn, f"msg_holdout_{i}", ticker=f"H{i}")
    conn.close()

    exit_code = cli.cmd_build_taste(config)
    assert exit_code == 1

    conn = db.connect(config.database_path)
    assert db.get_latest_taste_version(conn)["version_label"] == "v1"
    conn.close()
