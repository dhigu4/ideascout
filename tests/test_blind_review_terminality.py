"""Blind-review assignment terminality.

Once an assignment has been judged (judged_at set by an accepted judgment), it
can never be answered again -- even if every judgment for it has since been
excluded from learning. A stray second reply fails closed to NEEDS_REVIEW with
no feedback row created, and inconsistent judged_at/judgment states also fail
closed. Reuses the parse-mail and duplicate-repair fixtures; every database
lives under pytest's tmp_path, with no LLM, network, or AgentMail calls.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io

import pytest

from ideascout import blind_review, cli, db, holdout_audit
from tests.test_cli_parse_mail_blind_review import (
    assign_blind_review,
    get_status,
    insert_blind_review_message,
    insert_extracted_source,
    make_config,
)
from tests.test_narrative_repair_diagnostics import _snapshot_all_tables
from tests.test_repair_holdout_duplicate import _repair, _scenario
from tests.test_holdout_audit import _audit
from tests.test_cli_blind_review import insert_extracted_source as insert_source_at
from tests.test_cli_blind_review import insert_screening


def _with_allowed_sender(config):
    return dataclasses.replace(config, brad_allowed_senders=frozenset({"brad@example.com"}))


def _feedback_count(config, assignment_id):
    conn = db.connect(config.database_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM feedback WHERE blind_review_assignment_id = ?", (assignment_id,)
    ).fetchone()[0]
    conn.close()
    return count


def _assignment(config, assignment_id):
    conn = db.connect(config.database_path)
    row = dict(conn.execute("SELECT * FROM blind_review_assignments WHERE assignment_id = ?", (assignment_id,)).fetchone())
    conn.close()
    return row


def _first_judgment(config, body="BR-1 - LIKE\nfirst opinion"):
    conn = db.connect(config.database_path)
    source_id = insert_extracted_source(conn, external_id="1")
    assignment_id = assign_blind_review(conn, source_id)
    insert_blind_review_message(conn, "msg_1", body_raw=body)
    conn.close()
    cli.cmd_parse_mail(config)
    return assignment_id


def _stray_reply(config, message_id, body):
    conn = db.connect(config.database_path)
    insert_blind_review_message(conn, message_id, body_raw=body)
    conn.close()
    with contextlib.redirect_stdout(io.StringIO()):
        cli.cmd_parse_mail(config)


# --- terminality on a plain assignment -----------------------------------------------


def test_first_verdict_is_accepted_and_sets_judged_at(tmp_path):
    config = make_config(tmp_path)
    assignment_id = _first_judgment(config)

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "PARSED"
    conn.close()
    assert _assignment(config, assignment_id)["judged_at"] is not None
    assert _feedback_count(config, assignment_id) == 1


def test_second_verdict_rejected_while_first_judgment_is_learning_eligible(tmp_path):
    config = make_config(tmp_path)
    assignment_id = _first_judgment(config)
    judged_at = _assignment(config, assignment_id)["judged_at"]
    body = "BR-1 - PASS\nsecond, independent opinion"

    _stray_reply(config, "msg_2", body)

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_2") == "NEEDS_REVIEW"
    row = db.get_message(conn, "msg_2")
    assert "DUPLICATE_BLIND_REVIEW" in row["error"]
    assert "terminal" in row["error"]
    assert row["body_raw"] == body
    conn.close()
    assert _feedback_count(config, assignment_id) == 1
    assert _assignment(config, assignment_id)["judged_at"] == judged_at


def test_second_verdict_rejected_even_after_first_judgment_is_excluded(tmp_path):
    config = make_config(tmp_path)
    assignment_id = _first_judgment(config)
    conn = db.connect(config.database_path)
    first_feedback_id = conn.execute(
        "SELECT feedback_id FROM feedback WHERE blind_review_assignment_id = ?", (assignment_id,)
    ).fetchone()[0]
    conn.close()
    with contextlib.redirect_stdout(io.StringIO()):
        cli.cmd_exclude_feedback(config, None, feedback_id=first_feedback_id)
    before = _snapshot_all_tables(config)

    _stray_reply(config, "msg_2", "BR-1 - MAYBE\nreply after exclusion")

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_2") == "NEEDS_REVIEW"
    assert "DUPLICATE_BLIND_REVIEW" in db.get_message(conn, "msg_2")["error"]
    feedback_flags = conn.execute(
        "SELECT excluded_from_learning FROM feedback WHERE feedback_id = ?", (first_feedback_id,)
    ).fetchone()[0]
    conn.close()
    assert feedback_flags == 1
    assert _feedback_count(config, assignment_id) == 1

    after = _snapshot_all_tables(config)
    for table in before:
        if table in ("messages_raw", "app_meta"):
            continue
        assert after[table] == before[table], table


# --- administrative repair interaction ---------------------------------------------------


def test_stray_br16_reply_after_duplicate_repair_keeps_the_holdout_at_19(tmp_path, monkeypatch):
    config, _, keep, dup = _scenario(tmp_path, monkeypatch)
    assert _repair(config, dup, keep)[0] == 0
    dup_ref = blind_review.format_review_ref(dup["assignment_id"])
    keep_before = _snapshot_rows(config, "feedback", keep["feedback_id"])
    dup_row_before = _snapshot_rows(config, "feedback", dup["feedback_id"])

    _stray_reply(_with_allowed_sender(config), "stray_br16", f"{dup_ref} — MAYBE\nreplying again")

    conn = db.connect(config.database_path)
    assert get_status(conn, "stray_br16") == "NEEDS_REVIEW"
    assert "DUPLICATE_BLIND_REVIEW" in db.get_message(conn, "stray_br16")["error"]
    conn.close()
    assert _feedback_count(config, dup["assignment_id"]) == 1
    assert _snapshot_rows(config, "feedback", keep["feedback_id"]) == keep_before
    assert _snapshot_rows(config, "feedback", dup["feedback_id"]) == dup_row_before
    assert _assignment(config, dup["assignment_id"])["judged_at"] is not None

    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    assert len(db.get_eligible_feedback_after(conn, latest["checkpoint_feedback_id"])) == 19
    conn.close()
    audit = _audit(config)
    assert audit.official_count == 19
    assert audit.unique_count == 19
    assert dup["feedback_id"] not in {r.feedback_id for r in audit.official}


def _snapshot_rows(config, table, feedback_id):
    conn = db.connect(config.database_path)
    row = dict(conn.execute(f"SELECT * FROM {table} WHERE feedback_id = ?", (feedback_id,)).fetchone())
    conn.close()
    return row


def test_replacement_blind_review_is_still_judged_normally_after_a_stray_reply(tmp_path, monkeypatch):
    config, _, keep, dup = _scenario(tmp_path, monkeypatch)
    assert _repair(config, dup, keep)[0] == 0
    dup_ref = blind_review.format_review_ref(dup["assignment_id"])
    _stray_reply(_with_allowed_sender(config), "stray_br16", f"{dup_ref} — MAYBE\nagain")

    conn = db.connect(config.database_path)
    replacement_source = insert_source_at(conn, external_id="4100", ticker="NEW3", company="Fresh Replacement Co",
                            discovered_at="2026-03-01T00:00:00+00:00")
    insert_screening(conn, source_id=replacement_source, overall_prediction="WATCH")
    conn.close()
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.cmd_blind_review(config, limit=1) == 0
    conn = db.connect(config.database_path)
    new_assignment = conn.execute("SELECT MAX(assignment_id) FROM blind_review_assignments").fetchone()[0]
    conn.close()
    assert new_assignment > dup["assignment_id"]
    new_ref = blind_review.format_review_ref(new_assignment)

    _stray_reply(_with_allowed_sender(config), "replacement_reply", f"{new_ref} — LIKE\nfresh judgment")

    conn = db.connect(config.database_path)
    assert get_status(conn, "replacement_reply") == "PARSED"
    conn.close()
    assert _feedback_count(config, new_assignment) == 1
    audit = _audit(config)
    assert audit.official_count == 20
    assert audit.unique_count == 20
    assert audit.clean


def test_new_assignment_remains_creatable_and_judgeable_with_no_prior_judgments(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    untouched = insert_extracted_source(conn, external_id="5001")
    fresh = insert_extracted_source(conn, external_id="5002", ticker="FRESH", company="Fresh Co")
    conn.close()
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.cmd_blind_review(config, limit=2) == 0
    conn = db.connect(config.database_path)
    ids = {r["source_id"]: r["assignment_id"] for r in conn.execute("SELECT source_id, assignment_id FROM blind_review_assignments")}
    conn.close()
    assert set(ids) == {untouched, fresh}

    _stray_reply(config, "fresh_reply", f"BR-{ids[fresh]} — LIKE\nfine")

    conn = db.connect(config.database_path)
    assert get_status(conn, "fresh_reply") == "PARSED"
    conn.close()


# --- inconsistent legacy states ---------------------------------------------------------


def test_judged_at_null_but_judgment_row_exists_fails_safely(tmp_path):
    config = make_config(tmp_path)
    assignment_id = _first_judgment(config)
    conn = db.connect(config.database_path)
    conn.execute("UPDATE blind_review_assignments SET judged_at = NULL WHERE assignment_id = ?", (assignment_id,))
    conn.commit()
    conn.close()

    _stray_reply(config, "legacy_null", f"BR-{assignment_id} — PASS\nlegacy state")

    conn = db.connect(config.database_path)
    assert get_status(conn, "legacy_null") == "NEEDS_REVIEW"
    assert "INCONSISTENT_BLIND_REVIEW_STATE" in db.get_message(conn, "legacy_null")["error"]
    conn.close()
    assert _feedback_count(config, assignment_id) == 1


def test_judged_at_set_but_no_judgment_row_fails_safely(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = insert_extracted_source(conn, external_id="6001")
    assignment_id = assign_blind_review(conn, source_id)
    conn.execute(
        "UPDATE blind_review_assignments SET judged_at = '2026-01-05T00:00:00+00:00' WHERE assignment_id = ?",
        (assignment_id,),
    )
    conn.commit()
    conn.close()

    _stray_reply(config, "legacy_orphan", f"BR-{assignment_id} — LIKE\norphaned state")

    conn = db.connect(config.database_path)
    assert get_status(conn, "legacy_orphan") == "NEEDS_REVIEW"
    assert "INCONSISTENT_BLIND_REVIEW_STATE" in db.get_message(conn, "legacy_orphan")["error"]
    conn.close()
    assert _feedback_count(config, assignment_id) == 0


# --- parser idempotence -------------------------------------------------------------------


def test_parser_never_writes_and_is_repeatable_for_a_replayed_body(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = insert_extracted_source(conn, external_id="7001")
    assignment_id = assign_blind_review(conn, source_id)
    conn.close()
    body = f"BR-{assignment_id} — LIKE\nreplayed body"
    before = _snapshot_all_tables(config)

    conn = db.connect(config.database_path)
    first = blind_review.parse_blind_review(conn, body)
    second = blind_review.parse_blind_review(conn, body)
    conn.close()

    assert first.ok and second.ok
    assert _snapshot_all_tables(config) == before


def test_parser_rejects_replay_once_judged_and_writes_nothing(tmp_path):
    config = make_config(tmp_path)
    assignment_id = _first_judgment(config)
    body = f"BR-{assignment_id} — LIKE\nreplayed body"
    before = _snapshot_all_tables(config)

    conn = db.connect(config.database_path)
    result = blind_review.parse_blind_review(conn, body)
    conn.close()

    assert not result.ok
    assert "DUPLICATE_BLIND_REVIEW" in result.reason
    assert _snapshot_all_tables(config) == before


def test_mail_replay_of_the_same_message_creates_nothing_further(tmp_path):
    config = make_config(tmp_path)
    assignment_id = _first_judgment(config)
    before = _judgment_tables(config)

    cli.cmd_parse_mail(config)
    cli.cmd_parse_mail(config)

    assert _judgment_tables(config) == before
    assert _feedback_count(config, assignment_id) == 1


def _judgment_tables(config):
    """Judgment-bearing tables only: app_meta carries a last_parse_at run
    timestamp that legitimately changes on every parse-mail run.
    """
    snapshot = _snapshot_all_tables(config)
    return {table: snapshot[table] for table in ("feedback", "blind_review_assignments")}
