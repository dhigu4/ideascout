"""Tests for `python run.py repair-holdout-duplicate` and the replacement
blind-review selector's duplicate protection.

Scenario mirrors the confirmed production duplicate: BR-15 / feedback 58
(KEEP) and BR-16 / feedback 59 (EXCLUDE) are both CPRT / Copart, Inc. with
label MAYBE. Every database lives under pytest's tmp_path; no LLM, network,
AgentMail, or browser is touched, and production state is never read.
"""

from __future__ import annotations

import contextlib
import io
import sqlite3

import pytest

from ideascout import blind_review, cli, db, holdout_audit
from tests.test_cli_blind_review import insert_extracted_source
from tests.test_holdout_audit import _audit, _holdout_member, _render, _setup
from tests.test_narrative_repair_diagnostics import _snapshot_all_tables


def _scenario(tmp_path, monkeypatch):
    config, latest = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    keep = _holdout_member(conn, external_id="143646", ticker="CPRT", company="Copart, Inc.", verdict="MAYBE")
    dup = _holdout_member(conn, external_id="143636", ticker="CPRT", company="Copart, Inc.", verdict="MAYBE",
                          discovered_at="2026-02-02T00:00:00+00:00")
    for i in range(18):
        _holdout_member(conn, external_id=str(2000 + i), ticker=f"H{i}", company=f"Holdout Co {i}")
    conn.close()
    return config, latest, keep, dup


def _repair(config, dup, keep, *, dry_run=False, **overrides):
    args = dict(
        exclude_feedback_id=dup["feedback_id"],
        expect_ref=blind_review.format_review_ref(dup["assignment_id"]),
        expect_ticker="CPRT",
        expect_company="Copart, Inc.",
        expect_label="MAYBE",
        retain_feedback_id=keep["feedback_id"],
        retain_ref=blind_review.format_review_ref(keep["assignment_id"]),
        reason="duplicate holdout idea; retained BR-15 feedback 58",
        dry_run=dry_run,
    )
    args.update(overrides)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli.cmd_repair_holdout_duplicate(config, **args)
    return code, buffer.getvalue()


def _backups(config):
    backups_dir = config.database_path.parent / "backups"
    return sorted(p.name for p in backups_dir.glob("*.db")) if backups_dir.exists() else []


def _feedback_row(config, feedback_id):
    conn = db.connect(config.database_path)
    row = dict(conn.execute("SELECT * FROM feedback WHERE feedback_id = ?", (feedback_id,)).fetchone())
    conn.close()
    return row


def _taste_status_ids(config):
    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    ids = {r["feedback_id"] for r in db.get_eligible_feedback_after(conn, latest["checkpoint_feedback_id"])}
    conn.close()
    return ids


# --- dry run and real exclusion ----------------------------------------------------------


def test_dry_run_changes_nothing_and_creates_no_backup(tmp_path, monkeypatch):
    config, _, keep, dup = _scenario(tmp_path, monkeypatch)
    before = _snapshot_all_tables(config)
    backups_before = _backups(config)

    code, output = _repair(config, dup, keep, dry_run=True)

    assert code == 0
    assert "DRY RUN: nothing changed" in output
    assert _snapshot_all_tables(config) == before
    assert _backups(config) == backups_before


def test_real_exclusion_preserves_rows_and_changes_only_the_exclusion_flag(tmp_path, monkeypatch):
    config, _, keep, dup = _scenario(tmp_path, monkeypatch)
    before = _snapshot_all_tables(config)

    code, _ = _repair(config, dup, keep)

    assert code == 0
    after = _snapshot_all_tables(config)
    for table in before:
        if table == "app_meta":
            continue
        assert len(after[table]) == len(before[table]), table
    assert after["blind_review_assignments"] == before["blind_review_assignments"]

    feedback_before = {r[0]: r for r in before["feedback"]}
    feedback_after = {r[0]: r for r in after["feedback"]}
    assert feedback_before.keys() == feedback_after.keys()
    excluded_index = [c[1] for c in _columns(config, "feedback")].index("excluded_from_learning")
    for fid, row in feedback_before.items():
        changed = [i for i, (a, b) in enumerate(zip(row, feedback_after[fid])) if a != b]
        if fid == dup["feedback_id"]:
            assert changed == [excluded_index]
            assert feedback_after[fid][excluded_index] == 1
        else:
            assert changed == [], fid

    app_meta_added = {r for r in after["app_meta"]} - {r for r in before["app_meta"]}
    assert len(app_meta_added) == 1
    assert f"holdout_repair:feedback:{dup['feedback_id']}" in next(iter(app_meta_added))[0]


def _columns(config, table):
    conn = db.connect(config.database_path)
    rows = [tuple(r) for r in conn.execute(f"PRAGMA table_info({table})")]
    conn.close()
    return rows


def test_excluded_duplicate_no_longer_counts_toward_holdout(tmp_path, monkeypatch):
    config, _, keep, dup = _scenario(tmp_path, monkeypatch)
    assert dup["feedback_id"] in _taste_status_ids(config)

    _repair(config, dup, keep)

    assert dup["feedback_id"] not in _taste_status_ids(config)
    assert len(_taste_status_ids(config)) == 19
    audit = _audit(config)
    assert audit.official_count == 19
    assert audit.unique_count == 19
    assert audit.valid_unique_count == 19


def test_excluded_duplicate_no_longer_qualifies_for_future_learning(tmp_path, monkeypatch):
    config, _, keep, dup = _scenario(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    before = {r["feedback_id"] for r in db.get_feedback_eligible_for_learning(conn)}
    conn.close()
    assert dup["feedback_id"] in before

    _repair(config, dup, keep)

    conn = db.connect(config.database_path)
    after = {r["feedback_id"] for r in db.get_feedback_eligible_for_learning(conn)}
    conn.close()
    assert dup["feedback_id"] not in after
    assert keep["feedback_id"] in after


def test_retained_row_is_unchanged(tmp_path, monkeypatch):
    config, _, keep, dup = _scenario(tmp_path, monkeypatch)
    keep_before = _feedback_row(config, keep["feedback_id"])
    assignment_before = _assignment(config, keep["assignment_id"])

    _repair(config, dup, keep)

    assert _feedback_row(config, keep["feedback_id"]) == keep_before
    assert _assignment(config, keep["assignment_id"]) == assignment_before
    assert keep["feedback_id"] in _taste_status_ids(config)


def _assignment(config, assignment_id):
    conn = db.connect(config.database_path)
    row = dict(conn.execute("SELECT * FROM blind_review_assignments WHERE assignment_id = ?", (assignment_id,)).fetchone())
    conn.close()
    return row


def test_taste_stays_frozen_and_unlocked_flag_stays_zero(tmp_path, monkeypatch, capsys):
    config, _, keep, dup = _scenario(tmp_path, monkeypatch)

    _repair(config, dup, keep)
    capsys.readouterr()
    assert cli.cmd_taste_status(config) == 0
    status = capsys.readouterr().out

    assert "Holdout judgments collected: 19 / 20" in status
    assert "Holdout complete: NO" in status
    assert "Taste frozen: YES" in status
    conn = db.connect(config.database_path)
    assert conn.execute("SELECT build_unlocked FROM taste_versions ORDER BY version_number DESC LIMIT 1").fetchone()[0] == 0
    conn.close()


def test_audit_reports_nineteen_unique_and_replacement_needed(tmp_path, monkeypatch):
    config, _, keep, dup = _scenario(tmp_path, monkeypatch)

    _repair(config, dup, keep)
    rendered = _render(config)
    audit = _audit(config)

    assert "Unique valid holdout ideas: 19 / 20" in rendered
    assert "REPLACEMENT BLIND-REVIEW JUDGMENT REQUIRED: 1" in rendered
    assert "Replacement blind-review judgment required: 1" in rendered
    assert "python run.py blind-review --limit 1" in rendered
    assert "HOLDOUT NOT CLEAN" in rendered
    assert "SUPERSEDED DUPLICATES" in rendered
    assert f"retained: {blind_review.format_review_ref(keep['assignment_id'])}" in rendered
    assert [r.feedback_id for r in audit.superseded] == [dup["feedback_id"]]
    assert audit.duplicate_groups == []
    assert audit.reconciles_total


def test_replacement_judgment_restores_a_clean_twenty(tmp_path, monkeypatch):
    config, _, keep, dup = _scenario(tmp_path, monkeypatch)
    _repair(config, dup, keep)
    conn = db.connect(config.database_path)
    _holdout_member(conn, external_id="3001", ticker="NEW1", company="New Replacement Co")
    conn.close()

    audit = _audit(config)

    assert audit.official_count == 20
    assert audit.unique_count == 20
    assert audit.clean
    assert "HOLDOUT CLEAN" in _render(config)


# --- idempotency -----------------------------------------------------------------------


def test_repair_is_idempotent(tmp_path, monkeypatch):
    config, _, keep, dup = _scenario(tmp_path, monkeypatch)
    _repair(config, dup, keep)
    after_first = _snapshot_all_tables(config)
    backups_after_first = _backups(config)

    code, output = _repair(config, dup, keep)

    assert code == 0
    assert "Already applied" in output
    assert _snapshot_all_tables(config) == after_first
    assert _backups(config) == backups_after_first


def test_second_repair_with_a_different_retained_row_is_refused(tmp_path, monkeypatch):
    config, _, keep, dup = _scenario(tmp_path, monkeypatch)
    _repair(config, dup, keep)
    other = _holdout_member_fixture(config, "3002", "OTHR", "Other Co")

    code, output = _repair(config, dup, other)

    assert code == 1
    assert "already excluded by a different recorded repair" in output


def _holdout_member_fixture(config, external_id, ticker, company):
    conn = db.connect(config.database_path)
    member = _holdout_member(conn, external_id=external_id, ticker=ticker, company=company)
    conn.close()
    return member


# --- safety validation -------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"expect_ref": "BR-99"},
        {"expect_ticker": "AAPL"},
        {"expect_company": "Other Corp"},
        {"expect_label": "LIKE"},
        {"retain_ref": "BR-99"},
        {"reason": "   "},
        {"exclude_feedback_id": 99999},
    ],
)
def test_mismatched_expectations_abort_with_no_changes(tmp_path, monkeypatch, overrides):
    config, _, keep, dup = _scenario(tmp_path, monkeypatch)
    before = _snapshot_all_tables(config)
    backups_before = _backups(config)

    code, output = _repair(config, dup, keep, **overrides)

    assert code != 0
    assert _snapshot_all_tables(config) == before
    assert _backups(config) == backups_before


def test_retained_row_must_be_the_same_identity(tmp_path, monkeypatch):
    config, _, keep, dup = _scenario(tmp_path, monkeypatch)
    other = _holdout_member_fixture(config, "3003", "OTHR2", "Unrelated Co")
    before = _snapshot_all_tables(config)

    code, output = _repair(config, dup, other)

    assert code == 1
    assert "does not share the excluded row's normalized ticker and company" in output
    assert _snapshot_all_tables(config) == before


def test_training_row_cannot_be_excluded_by_this_command(tmp_path, monkeypatch):
    config, latest, keep, dup = _scenario(tmp_path, monkeypatch)
    before = _snapshot_all_tables(config)
    training_id = min(__import__("json").loads(latest["training_feedback_ids_json"]))

    code, output = _repair(config, dup, keep, exclude_feedback_id=training_id)

    assert code == 1
    assert "is not a post-checkpoint feedback row" in output
    assert _snapshot_all_tables(config) == before


def test_non_official_row_cannot_be_excluded_twice_or_when_not_eligible(tmp_path, monkeypatch):
    config, _, keep, dup = _scenario(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    conn.execute("UPDATE feedback SET holdout_eligible = 0 WHERE feedback_id = ?", (dup["feedback_id"],))
    conn.commit()
    conn.close()
    before = _snapshot_all_tables(config)

    code, output = _repair(config, dup, keep)

    assert code == 1
    assert "is not a current official holdout member" in output
    assert _snapshot_all_tables(config) == before


# --- backup ----------------------------------------------------------------------------------


def test_real_run_creates_a_backup_of_the_pre_repair_state(tmp_path, monkeypatch):
    config, _, keep, dup = _scenario(tmp_path, monkeypatch)
    backups_before = set(_backups(config))

    code, output = _repair(config, dup, keep)

    assert code == 0
    new_backups = [b for b in _backups(config) if b not in backups_before]
    assert len(new_backups) == 1
    assert "repair_holdout_duplicate" in new_backups[0]
    assert f"Backup created: " in output
    backup_path = config.database_path.parent / "backups" / new_backups[0]
    backup = sqlite3.connect(str(backup_path))
    excluded = backup.execute(
        "SELECT excluded_from_learning FROM feedback WHERE feedback_id = ?", (dup["feedback_id"],)
    ).fetchone()[0]
    backup.close()
    assert excluded == 0


# --- replacement selector ----------------------------------------------------------------


def test_replacement_selector_excludes_cprt_and_every_holdout_company(tmp_path, monkeypatch):
    config, _, keep, dup = _scenario(tmp_path, monkeypatch)
    _repair(config, dup, keep)
    conn = db.connect(config.database_path)
    same_ticker_other_name = insert_extracted_source(
        conn, external_id="4001", ticker="CPRT", company="Copart Holdings Group", discovered_at="2026-03-01T00:00:00+00:00"
    )
    same_company_other_ticker = insert_extracted_source(
        conn, external_id="4002", ticker="CPRX", company="Copart, Inc.", discovered_at="2026-03-01T00:01:00+00:00"
    )
    same_company_spelled_differently = insert_extracted_source(
        conn, external_id="4003", ticker="CPRZ", company="copart inc", discovered_at="2026-03-01T00:02:00+00:00"
    )
    fresh = insert_extracted_source(
        conn, external_id="4004", ticker="NEW2", company="Fresh Replacement Co", discovered_at="2026-03-01T00:03:00+00:00"
    )
    conn.close()
    assigned_before = _assigned_source_ids(config)

    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.cmd_blind_review(config, limit=1) == 0

    newly_assigned = _assigned_source_ids(config) - assigned_before
    assert newly_assigned == {fresh}
    assert same_ticker_other_name not in newly_assigned
    assert same_company_other_ticker not in newly_assigned
    assert same_company_spelled_differently not in newly_assigned


def _assigned_source_ids(config):
    conn = db.connect(config.database_path)
    ids = {r["source_id"] for r in conn.execute("SELECT source_id FROM blind_review_assignments")}
    conn.close()
    return ids
