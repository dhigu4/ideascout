"""Controlled Taste evaluation decision and one-build authorization.

record-taste-evaluation records Brad's decision for the latest frozen Taste
version. RETRAIN authorizes exactly one new build, which the successful build
consumes. ACCEPT records a decision and authorizes nothing. Every database,
artifact, and backup lives under pytest's tmp_path. No LLM, network, or
production state is touched; generation is stubbed.
"""

from __future__ import annotations

import contextlib
import io
import sqlite3
from pathlib import Path

import pytest

from ideascout import cli, db, taste, taste_evaluation
from tests.test_holdout_audit import _judge
from tests.test_narrative_repair_diagnostics import _snapshot_all_tables
from tests.test_taste_evaluation import STANDARD_20, _setup_v2, _standard_holdout, _member

BENIGN_BODY = "## Upside / Asymmetry Preferences\nBrad appears to prefer larger upside.\n"


def _record(config, *, decision="RETRAIN", reason="failed discovery recall; retrain", dry_run=False, version=2):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli.cmd_record_taste_evaluation(
            config, version=version, decision=decision, reason=reason, dry_run=dry_run
        )
    return code, buffer.getvalue()


def _holdout(tmp_path, monkeypatch):
    config, latest = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _standard_holdout(conn)
    conn.close()
    return config, latest


def _decision(config):
    conn = db.connect(config.database_path)
    row = conn.execute("SELECT * FROM taste_evaluation_decisions WHERE taste_version_number = 2").fetchone()
    conn.close()
    return dict(row) if row else None


def _v(config, number):
    conn = db.connect(config.database_path)
    row = conn.execute("SELECT * FROM taste_versions WHERE version_number = ?", (number,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _unlocked(config, number=2):
    return _v(config, number)["build_unlocked"]


def _patch_generation(monkeypatch, *, body=BENIGN_BODY, fail=False):
    monkeypatch.setattr(taste, "build_client", lambda api_key: object())

    def idea_body(client, model, records, *, permanent_rules_text, logger=None):
        if fail:
            raise taste.TasteGenerationError("simulated generation failure")
        return body

    monkeypatch.setattr(taste, "generate_idea_taste_body", idea_body)
    monkeypatch.setattr(taste, "generate_candidate_rules", lambda *a, permanent_rules_text, logger=None, **k: [])


def _build(config, capsys):
    code = cli.cmd_build_taste(config)
    return code, capsys.readouterr().out


# --- recording: dry run, real run, semantics --------------------------------------------


def test_retrain_dry_run_changes_nothing_and_creates_no_backup(tmp_path, monkeypatch):
    config, _ = _holdout(tmp_path, monkeypatch)
    before = _snapshot_all_tables(config)
    backups_before = sorted((config.database_path.parent / "backups").glob("*")) if (config.database_path.parent / "backups").exists() else []

    code, output = _record(config, dry_run=True)

    assert code == 0
    assert "DRY RUN: nothing recorded, nothing unlocked, and no backup was created." in output
    assert _snapshot_all_tables(config) == before
    after_backups = sorted((config.database_path.parent / "backups").glob("*")) if (config.database_path.parent / "backups").exists() else []
    assert after_backups == backups_before


def test_real_retrain_records_complete_evaluation_metadata(tmp_path, monkeypatch):
    config, latest = _holdout(tmp_path, monkeypatch)

    code, output = _record(config)

    assert code == 0
    row = _decision(config)
    assert row["taste_version_number"] == 2
    assert row["decision"] == "RETRAIN"
    assert row["reason"] == "failed discovery recall; retrain"
    assert row["recorded_at"]
    assert row["checkpoint_feedback_id"] == latest["checkpoint_feedback_id"]
    assert (row["holdout_count"], row["unique_holdout_count"]) == (20, 20)
    assert row["screening_prompt_version"] == 2
    assert row["taste_artifact_sha256"] == latest["idea_taste_sha256"]
    assert len(row["screen_rules_sha256"]) == 64
    assert (row["exact_correct"], row["exact_total"]) == (9, 20)
    assert (row["binary_tp"], row["binary_fp"], row["binary_tn"], row["binary_fn"]) == (9, 2, 4, 4)
    assert row["recall"] == pytest.approx(9 / 13)
    assert row["balanced_accuracy"] == pytest.approx((9 / 13 + 4 / 6) / 2)
    assert (row["high_conviction_hit"], row["high_conviction_total"]) == (3, 7)
    assert len(row["evaluation_fingerprint"]) == 64
    assert row["consumed_by_version"] is None and row["consumed_at"] is None


def test_retrain_sets_build_unlocked_from_zero_to_one(tmp_path, monkeypatch):
    config, _ = _holdout(tmp_path, monkeypatch)
    assert _unlocked(config) == 0

    code, output = _record(config)

    assert code == 0
    assert _unlocked(config) == 1
    assert "exactly ONE new Taste build is authorized" in output
    assert "It has NOT been built." in output


def test_accept_records_the_decision_and_leaves_build_unlocked_zero(tmp_path, monkeypatch):
    config, _ = _holdout(tmp_path, monkeypatch)

    code, output = _record(config, decision="ACCEPT")

    assert code == 0
    assert _decision(config)["decision"] == "ACCEPT"
    assert _unlocked(config) == 0
    assert "no new build is authorized" in output


def test_recorded_fingerprint_is_deterministic_and_sensitive_to_results(tmp_path, monkeypatch):
    config, latest = _holdout(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    gate = taste_evaluation.check_gate(conn, latest, 2)
    conn.close()
    metrics = taste_evaluation.compute_metrics(gate.rows)

    def fingerprint(m):
        return taste_evaluation.build_decision_record(
            latest_taste=latest, gate=gate, metrics=m, rules_sha256="r", decision="RETRAIN",
            reason="x", recorded_at="t",
        )["evaluation_fingerprint"]

    first = fingerprint(metrics)
    assert first == fingerprint(metrics)
    changed = taste_evaluation.Metrics(**{**metrics.__dict__, "tp": metrics.tp + 1})
    assert fingerprint(changed) != first


# --- refusals ----------------------------------------------------------------------------


def test_incomplete_holdout_refuses_and_records_nothing(tmp_path, monkeypatch):
    config, _ = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    for n, (label, prediction) in enumerate(STANDARD_20[:19]):
        _member(conn, n, label=label, prediction=prediction, reasons=("r",), concerns=("c",), questions=("q",))
    conn.close()

    code, output = _record(config)

    assert code == 1
    assert "[FAIL] Holdout complete" in output
    assert _decision(config) is None
    assert _unlocked(config) == 0


def test_unclean_holdout_refuses_and_records_nothing(tmp_path, monkeypatch):
    config, _ = _holdout(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    conn.execute("UPDATE feedback SET excluded_from_learning = 1 WHERE feedback_id = (SELECT MAX(feedback_id) FROM feedback)")
    conn.commit()
    conn.close()

    code, output = _record(config)

    assert code == 1
    assert "REFUSED" in output
    assert _decision(config) is None
    assert _unlocked(config) == 0


def test_artifact_integrity_failure_refuses(tmp_path, monkeypatch):
    config, latest = _holdout(tmp_path, monkeypatch)
    Path(latest["idea_taste_path"]).write_text("tampered\n", encoding="utf-8")

    code, output = _record(config)

    assert code == 1
    assert "[FAIL] Taste artifact integrity OK" in output
    assert _decision(config) is None


def test_mixed_prompt_version_evaluation_refuses(tmp_path, monkeypatch):
    config, _ = _holdout(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    first_source = conn.execute("SELECT source_id FROM blind_review_assignments ORDER BY assignment_id LIMIT 1").fetchone()[0]
    conn.execute("UPDATE source_screenings SET screening_prompt_version = 1 WHERE source_id = ?", (first_source,))
    conn.commit()
    conn.close()

    code, output = _record(config)

    assert code == 1
    assert "[FAIL] Single screening prompt version across the holdout" in output
    assert _decision(config) is None


def test_reason_is_required(tmp_path, monkeypatch):
    config, _ = _holdout(tmp_path, monkeypatch)

    code, output = _record(config, reason="   ")

    assert code == 2
    assert "non-empty --reason is required" in output
    assert _decision(config) is None


def test_duplicate_second_decision_is_refused_and_leaves_the_first_intact(tmp_path, monkeypatch):
    config, _ = _holdout(tmp_path, monkeypatch)
    _record(config, decision="RETRAIN")
    first = _decision(config)

    code, output = _record(config, decision="ACCEPT", reason="changed my mind")

    assert code == 1
    assert "Decisions are never replaced" in output
    assert _decision(config) == first
    assert _unlocked(config) == 1


def test_a_non_latest_version_is_refused(tmp_path, monkeypatch):
    config, _ = _holdout(tmp_path, monkeypatch)

    code, output = _record(config, version=1)

    assert code == 1
    assert "not the latest Taste version" in output


# --- backup -----------------------------------------------------------------------------


def test_real_run_takes_a_backup_before_mutation_and_dry_run_does_not(tmp_path, monkeypatch):
    config, _ = _holdout(tmp_path, monkeypatch)
    backups = config.database_path.parent / "backups"

    _record(config, dry_run=True)
    assert not backups.exists() or not any("record_taste_evaluation" in p.name for p in backups.iterdir())

    code, output = _record(config)

    assert code == 0
    assert "Backup created:" in output
    matches = [p for p in backups.iterdir() if "record_taste_evaluation" in p.name]
    assert len(matches) == 1


def test_backup_contents_are_the_pre_mutation_state(tmp_path, monkeypatch):
    config, _ = _holdout(tmp_path, monkeypatch)

    _record(config)

    backup = next(p for p in (config.database_path.parent / "backups").iterdir() if "record_taste_evaluation" in p.name)
    conn = sqlite3.connect(str(backup))
    assert conn.execute("SELECT build_unlocked FROM taste_versions WHERE version_number = 2").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM taste_evaluation_decisions").fetchone()[0] == 0
    conn.close()


# --- taste-status ------------------------------------------------------------------------


def test_taste_status_displays_decision_and_one_build_authorization(tmp_path, monkeypatch, capsys):
    config, _ = _holdout(tmp_path, monkeypatch)
    _record(config)
    capsys.readouterr()

    assert cli.cmd_taste_status(config) == 0
    out = capsys.readouterr().out

    assert "Holdout complete: YES" in out
    assert "Taste frozen: YES" in out
    assert "Evaluation decision: RETRAIN" in out
    assert "Evaluation recorded:" in out
    assert "Build authorization: ONE BUILD AUTHORIZED" in out


def test_taste_status_shows_locked_before_any_decision(tmp_path, monkeypatch, capsys):
    config, _ = _holdout(tmp_path, monkeypatch)

    cli.cmd_taste_status(config)
    out = capsys.readouterr().out

    assert "Evaluation decision: none" in out
    assert "Build authorization: LOCKED" in out
    assert "Awaiting holdout evaluation: YES" in out


# --- build-taste authorization ---------------------------------------------------------------


def test_build_refuses_without_an_evaluation_decision(tmp_path, monkeypatch, capsys):
    config, _ = _holdout(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    conn.execute("UPDATE taste_versions SET build_unlocked = 1 WHERE version_number = 2")
    conn.commit()
    conn.close()
    _patch_generation(monkeypatch)

    code, out = _build(config, capsys)

    assert code == 1
    assert "has no recorded evaluation decision" in out
    assert _v(config, 3) is None


def test_build_refuses_after_accept(tmp_path, monkeypatch, capsys):
    config, _ = _holdout(tmp_path, monkeypatch)
    _record(config, decision="ACCEPT")
    _patch_generation(monkeypatch)

    code, out = _build(config, capsys)

    assert code == 1
    assert "evaluation decision is ACCEPT" in out
    assert _v(config, 3) is None


def test_build_accepts_after_retrain_and_creates_v3_frozen(tmp_path, monkeypatch, capsys):
    config, _ = _holdout(tmp_path, monkeypatch)
    _record(config)
    _patch_generation(monkeypatch)

    code, out = _build(config, capsys)

    assert code == 0
    assert _v(config, 3)["build_unlocked"] == 0


def test_successful_build_consumes_the_authorization_and_resets_the_old_flag(tmp_path, monkeypatch, capsys):
    config, _ = _holdout(tmp_path, monkeypatch)
    _record(config)
    _patch_generation(monkeypatch)

    assert _build(config, capsys)[0] == 0

    decision = _decision(config)
    assert decision["consumed_by_version"] == 3
    assert decision["consumed_at"]
    assert _unlocked(config, 2) == 0


def test_failed_generation_leaves_the_authorization_available_for_retry(tmp_path, monkeypatch, capsys):
    config, _ = _holdout(tmp_path, monkeypatch)
    _record(config)
    _patch_generation(monkeypatch, fail=True)

    code, _ = _build(config, capsys)
    assert code == 1
    assert _decision(config)["consumed_by_version"] is None
    assert _unlocked(config, 2) == 1
    assert _v(config, 3) is None

    _patch_generation(monkeypatch)
    assert _build(config, capsys)[0] == 0
    assert _decision(config)["consumed_by_version"] == 3


def test_refused_hard_threshold_candidate_leaves_the_authorization_available(tmp_path, monkeypatch, capsys):
    config, _ = _holdout(tmp_path, monkeypatch)
    _record(config)
    _patch_generation(monkeypatch, body="Brad requires 3x upside for every idea.\n")

    code, _ = _build(config, capsys)

    assert code == 1
    assert _decision(config)["consumed_by_version"] is None
    assert _unlocked(config, 2) == 1


def test_second_build_without_a_new_evaluation_is_refused(tmp_path, monkeypatch, capsys):
    config, _ = _holdout(tmp_path, monkeypatch)
    _record(config)
    _patch_generation(monkeypatch)
    assert _build(config, capsys)[0] == 0

    code, out = _build(config, capsys)

    assert code == 1
    assert _v(config, 4) is None


def test_v2_artifact_and_screenings_are_untouched_by_the_authorized_build(tmp_path, monkeypatch, capsys):
    config, latest = _holdout(tmp_path, monkeypatch)
    artifact_bytes = Path(latest["idea_taste_path"]).read_bytes()
    _record(config)
    before = _snapshot_all_tables(config)
    _patch_generation(monkeypatch)

    assert _build(config, capsys)[0] == 0

    assert Path(latest["idea_taste_path"]).read_bytes() == artifact_bytes
    after = _snapshot_all_tables(config)
    assert after["source_screenings"] == before["source_screenings"]
    assert after["feedback"] == before["feedback"]


# --- v3 training set --------------------------------------------------------------------------


def _eligible_ids(config):
    conn = db.connect(config.database_path)
    ids = {r["feedback_id"] for r in db.get_feedback_eligible_for_learning(conn)}
    conn.close()
    return ids


def test_clean_v2_holdout_judgments_enter_the_next_training_set(tmp_path, monkeypatch):
    config, _ = _holdout(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    holdout_ids = {r["feedback_id"] for r in db.get_eligible_feedback_after(conn, 15)}
    conn.close()

    assert len(holdout_ids) == 20
    assert holdout_ids <= _eligible_ids(config)


def test_superseded_duplicate_never_enters_the_next_training_set(tmp_path, monkeypatch):
    config, _ = _holdout(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    dup = conn.execute("SELECT feedback_id FROM feedback ORDER BY feedback_id DESC LIMIT 1").fetchone()[0]
    db.apply_holdout_duplicate_exclusion(
        conn, feedback_id=dup,
        record={"action": "excluded_duplicate", "feedback_id": dup, "retained_feedback_id": 1,
                "retained_ref": "BR-1", "reason": "test"},
    )
    conn.close()

    assert dup not in _eligible_ids(config)


def test_learning_eligibility_of_screen_review_and_digest_reply_is_unchanged(tmp_path, monkeypatch):
    config, _ = _holdout(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    sr = _judge(conn, source_id=None, assignment_id=None, origin="SCREEN_REVIEW", holdout_eligible=False,
                ticker="SRX", company="Screen Review X")
    dr = _judge(conn, source_id=None, assignment_id=None, origin="DIGEST_REPLY", holdout_eligible=False,
                ticker="DRX", company="Digest Reply X")
    conn.close()

    eligible = _eligible_ids(config)

    assert sr in eligible and dr in eligible


def test_smoke_parse_failure_and_excluded_rows_never_enter_training(tmp_path, monkeypatch):
    config, _ = _holdout(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    smoke = _judge(conn, source_id=None, assignment_id=None, origin="DIRECT", ticker="TESTCO", company="Testco")
    conn.execute("UPDATE feedback SET excluded_from_learning = 1 WHERE feedback_id = ?", (smoke,))
    unparsed = _judge(conn, source_id=None, assignment_id=None, origin="DIRECT", ticker="NPX", company="Needs Review",
                      parse_status="NEEDS_REVIEW")
    conn.commit()
    conn.close()

    eligible = _eligible_ids(config)

    assert smoke not in eligible
    assert unparsed not in eligible


def test_evaluate_taste_stays_read_only_after_a_decision(tmp_path, monkeypatch):
    config, _ = _holdout(tmp_path, monkeypatch)
    _record(config)
    before = _snapshot_all_tables(config)

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        assert cli.cmd_evaluate_taste(config, version=2) == 0

    assert _snapshot_all_tables(config) == before
    assert "Recorded evaluation decision for v2: RETRAIN" in buffer.getvalue()
