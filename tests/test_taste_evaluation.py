"""Tests for `python run.py evaluate-taste --version N` (read-only formal
evaluation of a frozen Taste version against its clean blind holdout).

Every database and artifact lives under pytest's tmp_path. No LLM, network,
AgentMail, or browser is touched, and production state is never read.
"""

from __future__ import annotations

import contextlib
import io
import json

from ideascout import blind_review, cli, db, taste, taste_evaluation as te
from tests.test_holdout_audit import _judge, _setup
from tests.test_cli_blind_review import insert_extracted_source as insert_source_at
from tests.test_narrative_repair_diagnostics import _snapshot_all_tables


def _setup_v2(tmp_path, monkeypatch):
    config, v1 = _setup(tmp_path, monkeypatch)
    artifact = tmp_path / "taste-v2-idea-taste.md"
    artifact.write_text("Taste v2 body.\n", encoding="utf-8")
    conn = db.connect(config.database_path)
    db.insert_taste_version(
        conn,
        version_number=2,
        version_label="v2",
        generated_at="2026-05-01T00:00:00+00:00",
        model_name="fake-model",
        training_count=v1["training_count"],
        training_feedback_ids_json=v1["training_feedback_ids_json"],
        checkpoint_feedback_id=v1["checkpoint_feedback_id"],
        idea_taste_path=str(artifact),
        idea_taste_sha256=taste.compute_file_sha256(artifact),
        candidate_rules_path=str(tmp_path / "candidate-permanent-rules.md"),
        created_at="2026-05-01T00:00:00+00:00",
    )
    latest = db.get_latest_taste_version(conn)
    conn.close()
    return config, latest


def _member(conn, n, *, label, prediction, ticker=None, company=None, reasons=(), concerns=(),
            questions=(), screen=True, upside="Potentially sufficient", confidence="MEDIUM"):
    ticker = ticker or f"H{n}"
    company = company or f"Holdout Co {n}"
    source_id = insert_source_at(conn, external_id=str(9000 + n), ticker=ticker, company=company)
    if screen:
        db.insert_source_screening(
            conn,
            source_id=source_id,
            created_at="2026-01-01T00:00:00+00:00",
            taste_version=2,
            taste_sha256="v2-sha",
            screen_rules_path="IDEA_SCREEN_RULES.md",
            screen_rules_sha256="rules-sha",
            content_hash=f"hash-{n}",
            model_name="fake-model",
            overall_prediction=prediction,
            mispricing="Plausible",
            variant_perception="Weak",
            upside=upside,
            business_quality="Plausible",
            downside="Acceptable",
            key_reasons_json=json.dumps(list(reasons)),
            key_concerns_json=json.dumps(list(concerns)),
            critical_questions_json=json.dumps(list(questions)),
            confidence=confidence,
        )
    assignment_id = db.record_blind_review_assignment(
        conn, source_id=source_id, assigned_at="2026-05-02T00:00:00+00:00", taste_version_at_assignment=2
    )
    _judge(conn, source_id=source_id, assignment_id=assignment_id, verdict=label, ticker=ticker, company=company)
    return assignment_id


STANDARD_20 = (
    ("LIKE", "INVESTIGATE_NOW"), ("LIKE", "INVESTIGATE_NOW"), ("STRONG_LIKE", "INVESTIGATE_NOW"),
    ("MAYBE", "WATCH"), ("MAYBE", "WATCH"),
    ("MAYBE", "PASS"), ("MAYBE", "PASS"),
    ("LIKE", "PASS"), ("STRONG_LIKE", "PASS"),
    ("PASS", "WATCH"), ("STRONG_PASS", "INVESTIGATE_NOW"),
    ("PASS", "PASS"), ("PASS", "PASS"), ("PASS", "PASS"), ("PASS", "PASS"),
    ("PASS", "INSUFFICIENT_INFORMATION"),
    ("MAYBE", "INVESTIGATE_NOW"), ("MAYBE", "INVESTIGATE_NOW"),
    ("LIKE", "WATCH"), ("LIKE", "WATCH"),
)

THREE_X_REASON = "Could compound at ~3x+ if the turnaround works."


def _standard_holdout(conn):
    for n, (label, prediction) in enumerate(STANDARD_20):
        reasons = (THREE_X_REASON,) if n == 5 else ("A specific reason.",)
        _member(conn, n, label=label, prediction=prediction, reasons=reasons, concerns=("A risk.",),
                questions=("A question?",))


def _evaluate(config, version=2):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli.cmd_evaluate_taste(config, version=version)
    return code, buffer.getvalue()


def _gate(config):
    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    gate = te.check_gate(conn, latest, latest["version_number"])
    conn.close()
    return gate


# --- gate ---------------------------------------------------------------------------------


def test_clean_holdout_evaluates(tmp_path, monkeypatch):
    config, _ = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _standard_holdout(conn)
    conn.close()

    code, output = _evaluate(config)

    assert code == 0
    assert "HOLDOUT GATE: PASSED" in output
    assert "TASTE EVALUATION -- v2" in output
    assert "Brad review/approval is required before any unlock or new Taste build." in output
    assert "EVALUATION ONLY — Taste remains frozen." in output


def test_gate_refuses_when_holdout_has_a_questionable_candidate(tmp_path, monkeypatch):
    config, _ = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _standard_holdout(conn)
    _member(conn, 50, label="LIKE", prediction="INVESTIGATE_NOW", ticker="H50", company="Candidate Co")
    conn.execute("UPDATE feedback SET holdout_eligible = 0 WHERE feedback_id = (SELECT MAX(feedback_id) FROM feedback)")
    conn.commit()
    conn.close()

    code, output = _evaluate(config)

    assert code == 1
    assert "EVALUATION REFUSED" in output
    assert "[FAIL] Zero questionable blind candidates" in output
    assert "TASTE EVALUATION --" not in output


def test_incomplete_19_of_20_refuses(tmp_path, monkeypatch):
    config, _ = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    for n, (label, prediction) in enumerate(STANDARD_20[:19]):
        _member(conn, n, label=label, prediction=prediction, reasons=("r",), concerns=("c",), questions=("q",))
    conn.close()

    code, output = _evaluate(config)

    assert code == 1
    assert "[FAIL] Holdout complete: 20 official members (19 official)" in output
    assert "TASTE EVALUATION --" not in output


def test_duplicate_holdout_refuses(tmp_path, monkeypatch):
    config, _ = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _standard_holdout(conn)
    conn.close()
    conn = db.connect(config.database_path)
    twin = insert_source_at(conn, external_id="9900", ticker="H0", company="Holdout Co 0")
    conn.close()
    conn = db.connect(config.database_path)
    assignment = db.record_blind_review_assignment(
        conn, source_id=twin, assigned_at="2026-05-02T00:00:00+00:00", taste_version_at_assignment=2
    )
    _judge(conn, source_id=twin, assignment_id=assignment, verdict="LIKE", ticker="H0", company="Holdout Co 0")
    conn.close()

    code, output = _evaluate(config)

    assert code == 1
    assert "[FAIL] Zero duplicate groups" in output


def test_superseded_duplicate_is_excluded_from_the_evaluation(tmp_path, monkeypatch):
    config, _ = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _standard_holdout(conn)
    superseded = _member(conn, 60, label="MAYBE", prediction="PASS", ticker="H60", company="Dup Co")
    superseded_feedback = conn.execute(
        "SELECT feedback_id FROM feedback WHERE blind_review_assignment_id = ?", (superseded,)
    ).fetchone()[0]
    db.apply_holdout_duplicate_exclusion(
        conn, feedback_id=superseded_feedback,
        record={"action": "excluded_duplicate", "feedback_id": superseded_feedback, "retained_feedback_id": 1,
                "retained_ref": "BR-1", "reason": "test duplicate"},
    )
    conn.close()

    gate = _gate(config)

    assert gate.passed
    assert len(gate.rows) == 20
    assert superseded_feedback not in {r.feedback_id for r in gate.rows}


def test_wrong_requested_version_refuses(tmp_path, monkeypatch):
    config, _ = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _standard_holdout(conn)
    conn.close()

    code, output = _evaluate(config, version=1)

    assert code == 1
    assert "[FAIL] Taste version 1 is the latest version" in output


def test_unlocked_taste_refuses(tmp_path, monkeypatch):
    config, _ = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _standard_holdout(conn)
    conn.execute("UPDATE taste_versions SET build_unlocked = 1 WHERE version_number = 2")
    conn.commit()
    conn.close()

    code, output = _evaluate(config)

    assert code == 1
    assert "[FAIL] Taste version frozen (build_unlocked = 0, or authorized by a recorded decision)" in output


def test_missing_prediction_before_judgment_refuses(tmp_path, monkeypatch):
    config, _ = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _standard_holdout(conn)
    _member(conn, 70, label="LIKE", prediction="WATCH", ticker="H70", company="Unscreened Co", screen=False)
    conn.close()

    code, output = _evaluate(config)

    assert code == 1
    assert "no Taste v2 screening created before the judgment" in output


def test_artifact_tampering_refuses(tmp_path, monkeypatch):
    config, latest = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _standard_holdout(conn)
    conn.close()
    artifact = tmp_path / "taste-v2-idea-taste.md"
    artifact.write_text("tampered\n", encoding="utf-8")

    code, output = _evaluate(config)

    assert code == 1
    assert "[FAIL] Taste artifact integrity OK" in output


# --- label mapping and metrics ----------------------------------------------------------------


def _synth(label, prediction, feedback_id=1, screening=None):
    label = te.normalize_label(label)
    return te.EvalRow(
        ref=f"BR-{feedback_id}", feedback_id=feedback_id, company="Co", ticker="T", source="yellowbrick/1",
        sr_ref="SR-1", prediction=prediction, label=label, actual=te.BRAD_LABEL_TO_CLASS[label],
        screening=screening, judged_at="2026-06-01T00:00:00+00:00",
    )


def test_exact_label_mapping():
    assert te.BRAD_LABEL_TO_CLASS == {
        "STRONG LIKE": "INVESTIGATE_NOW", "LIKE": "INVESTIGATE_NOW", "MAYBE": "WATCH",
        "PASS": "PASS", "STRONG PASS": "PASS",
    }
    assert te.normalize_label("STRONG_LIKE") == "STRONG LIKE"
    assert te.normalize_label(" maybe ") == "MAYBE"


def test_three_class_confusion_matrix_and_accuracy():
    rows = [
        _synth("LIKE", "INVESTIGATE_NOW", 1), _synth("MAYBE", "PASS", 2), _synth("PASS", "PASS", 3),
        _synth("PASS", "WATCH", 4), _synth("LIKE", "INSUFFICIENT_INFORMATION", 5),
    ]
    m = te.compute_metrics(rows)

    assert m.matrix["INVESTIGATE_NOW"]["INVESTIGATE_NOW"] == 1
    assert m.matrix["WATCH"]["PASS"] == 1
    assert m.matrix["PASS"]["PASS"] == 1
    assert m.matrix["PASS"]["WATCH"] == 1
    assert m.matrix["INVESTIGATE_NOW"]["INSUFFICIENT_INFORMATION"] == 1
    assert m.exact_correct == 2
    assert m.n == 5
    assert m.n_scored == 4


def test_binary_precision_recall_specificity_balanced_and_f1():
    rows = [
        _synth("LIKE", "INVESTIGATE_NOW", 1),   # TP
        _synth("MAYBE", "WATCH", 2),            # TP
        _synth("MAYBE", "PASS", 3),             # FN
        _synth("PASS", "WATCH", 4),             # FP
        _synth("PASS", "PASS", 5),              # TN
        _synth("STRONG_PASS", "PASS", 6),       # TN
    ]
    m = te.compute_metrics(rows)

    assert (m.tp, m.fp, m.tn, m.fn) == (2, 1, 2, 1)
    assert m.precision == 2 / 3
    assert m.recall == 2 / 3
    assert m.specificity == 2 / 3
    assert m.balanced_accuracy == 2 / 3
    assert m.f1 == 2 / 3


def test_zero_denominators_are_reported_as_na_not_numbers():
    rows = [_synth("PASS", "PASS", 1), _synth("STRONG_PASS", "PASS", 2)]
    m = te.compute_metrics(rows)

    assert m.precision is None
    assert m.recall is None
    assert m.balanced_accuracy is None
    assert m.f1 is None
    assert te._fmt(m.precision) == "N/A"
    assert m.hc_recall is None
    assert te._fmt(m.hc_recall) == "N/A"


def test_high_conviction_recall_is_reported_separately():
    rows = [_synth("LIKE", "INVESTIGATE_NOW", 1), _synth("STRONG_LIKE", "PASS", 2), _synth("LIKE", "WATCH", 3)]
    m = te.compute_metrics(rows)

    assert (m.hc_hit, m.hc_actual) == (1, 3)
    assert m.hc_recall == 1 / 3


def test_insufficient_information_is_separate_not_pass():
    rows = [_synth("PASS", "INSUFFICIENT_INFORMATION", 1), _synth("PASS", "PASS", 2)]
    m = te.compute_metrics(rows)

    assert len(m.insufficient_rows) == 1
    assert m.n_scored == 1
    assert (m.tp, m.fp, m.tn, m.fn) == (0, 0, 1, 0)
    assert rows[0].kind == "UNSCORED (INSUFFICIENT_INFORMATION)"
    assert m.pass_pred_share == 0.5


def test_all_pass_classifier_is_flagged_as_degenerate(tmp_path, monkeypatch):
    config, _ = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    for n in range(20):
        label = "LIKE" if n < 10 else "PASS"
        _member(conn, n, label=label, prediction="PASS", reasons=("r",), concerns=("c",), questions=("q",))
    conn.close()

    _, output = _evaluate(config)

    assert "Degenerate all-PASS classifier behavior" in output
    assert "Judge by recall and balanced accuracy, not raw accuracy." in output


def test_materially_imbalanced_holdout_is_flagged(tmp_path, monkeypatch):
    config, _ = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    for n in range(20):
        label = "LIKE" if n < 4 else "PASS"
        prediction = "INVESTIGATE_NOW" if n < 4 else "PASS"
        _member(conn, n, label=label, prediction=prediction, reasons=("r",), concerns=("c",), questions=("q",))
    conn.close()

    _, output = _evaluate(config)

    assert "materially imbalanced" in output


# --- ordering -----------------------------------------------------------------------------------


def test_false_negative_rows_sort_most_costly_first():
    rows = [
        _synth("MAYBE", "PASS", 1),        # bucket 3
        _synth("STRONG_LIKE", "PASS", 2),  # bucket 1
        _synth("LIKE", "PASS", 3),         # bucket 2
        _synth("LIKE", "WATCH", 4),        # bucket 4
        _synth("STRONG_PASS", "INVESTIGATE_NOW", 5),  # bucket 5
        _synth("PASS", "WATCH", 6),        # bucket 6
        _synth("LIKE", "INVESTIGATE_NOW", 7),  # correct, bucket 8
    ]
    ordered = [r.feedback_id for r in sorted(rows, key=lambda r: (r.error_bucket, r.feedback_id))]

    assert ordered == [2, 3, 1, 4, 5, 6, 7]


def test_false_negatives_sort_before_false_positives_of_the_same_severity_band():
    fp_high = _synth("STRONG_PASS", "INVESTIGATE_NOW", 1)
    fp_watch = _synth("PASS", "WATCH", 2)
    fn_maybe = _synth("MAYBE", "PASS", 3)

    assert fn_maybe.error_bucket < fp_high.error_bucket < fp_watch.error_bucket
    assert fp_high.kind == "false positive"


# --- stored-text diagnostics ------------------------------------------------------------------


def _text_row(feedback_id, label, prediction, texts):
    screening = {"key_reasons_json": json.dumps(texts), "key_concerns_json": "[]",
                 "critical_questions_json": "[]"}
    return _synth(label, prediction, feedback_id, screening=screening)


def test_three_x_diagnostic_finds_matching_stored_text():
    rows = [
        _text_row(1, "MAYBE", "PASS", ["Could compound at ~3x+ over five years."]),
        _text_row(2, "LIKE", "INVESTIGATE_NOW", ["A three-bagger if the margin recovers."]),
        _text_row(3, "MAYBE", "WATCH", ["Effective hurdle is 2-3x after dilution."]),
        _text_row(4, "MAYBE", "WATCH", ["Solid balance sheet and a clear catalyst."]),
        _text_row(5, "MAYBE", "WATCH", ["Revenue is 13x last year, unrelated."]),
    ]
    hits = te.hurdle_hits(rows)

    assert [h.row.feedback_id for h in hits] == [1, 2, 3]
    assert "3x-style multiple" in hits[0].categories
    assert "three-bagger" in hits[1].categories
    assert "effective hurdle" in hits[2].categories
    assert "3x" in hits[0].excerpt
    assert len(hits[0].excerpt) <= 180


def test_three_x_diagnostic_distinguishes_permanent_100_percent_framing_from_3x_only():
    only_3x = _text_row(1, "MAYBE", "PASS", ["Looks like ~3x potential."])
    with_permanent = _text_row(2, "MAYBE", "PASS", ["Looks like ~3x potential; upside framed near 100%+."])

    hits = {h.row.feedback_id: h for h in te.hurdle_hits([only_3x, with_permanent])}

    assert hits[1].permanent_framing is False
    assert hits[2].permanent_framing is True


def test_three_x_diagnostic_never_claims_causality(tmp_path, monkeypatch):
    config, _ = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _standard_holdout(conn)
    conn.close()

    _, output = _evaluate(config)

    assert te.DISCLAIMER in output
    assert "does not prove that the 3x language caused the prediction" in output
    assert "3x-style multiple" in output


def test_rule_excerpt_quotes_only_the_permanent_rule_lines():
    rules = "1. Mispricing\n   Plausible.\n2. Upside\n   Should be roughly 100%+ before it is interesting.\n"

    excerpt = te.rule_excerpt(rules)

    assert excerpt == ["Should be roughly 100%+ before it is interesting."]


def test_pattern_themes_are_matched_on_stored_text_of_false_negatives():
    rows = [
        _text_row(1, "MAYBE", "PASS", ["No clear reason the market is wrong.", "Large-cap and well covered."]),
        _text_row(2, "LIKE", "PASS", ["NAV discount looks real.", "Takeout dependent thesis."]),
        _text_row(3, "MAYBE", "PASS", ["Nothing matched here at all."]),
    ]
    themes = dict(te.theme_counts(rows))

    assert themes["no clear reason for mispricing"] == ["BR-1"]
    assert themes["large cap / well covered"] == ["BR-1"]
    assert themes["NAV / book discount"] == ["BR-2"]
    assert themes["takeout dependent"] == ["BR-2"]
    assert themes["cyclical"] == []


# --- read-only, determinism, end-to-end --------------------------------------------------------


def test_evaluate_changes_no_rows_or_taste_flags(tmp_path, monkeypatch):
    config, _ = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _standard_holdout(conn)
    conn.close()
    before = _snapshot_all_tables(config)

    code, _ = _evaluate(config)

    assert code == 0
    assert _snapshot_all_tables(config) == before
    conn = db.connect(config.database_path)
    assert conn.execute("SELECT build_unlocked FROM taste_versions WHERE version_number = 2").fetchone()[0] == 0
    conn.close()


def test_output_is_deterministic(tmp_path, monkeypatch):
    config, _ = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _standard_holdout(conn)
    conn.close()

    first = _evaluate(config)[1]
    second = _evaluate(config)[1]

    assert first == second


def test_refused_evaluation_prints_no_metrics(tmp_path, monkeypatch):
    config, _ = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    for n, (label, prediction) in enumerate(STANDARD_20[:5]):
        _member(conn, n, label=label, prediction=prediction, reasons=("r",), concerns=("c",), questions=("q",))
    conn.close()

    code, output = _evaluate(config)

    assert code == 1
    assert "OVERALL ACCURACY" not in output
    assert "CONFUSION MATRIX" not in output
