"""Tests for `python run.py audit-holdout-integrity` (read-only holdout audit).

Three groups are separated after the training checkpoint: OFFICIAL holdout
members (the taste-status predicate), questionable BLIND_REVIEW candidates
(which make the holdout NOT CLEAN), and OTHER feedback such as SCREEN_REVIEW
and DIGEST_REPLY (reported only, never members, never duplicates).

Every database lives under pytest's tmp_path. No LLM, network, AgentMail, or
browser is ever touched, and production state is never read.
"""

from __future__ import annotations

import contextlib
import io

from ideascout import blind_review, cli, db, holdout_audit
from tests.test_cli_blind_review import insert_extracted_source, insert_screening
from tests.test_narrative_repair_diagnostics import _snapshot_all_tables
from tests.test_screening_narrative import build_taste_v1, make_config, write_rules

_counter = {"n": 0}


def _setup(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    return config, latest


def _judge(
    conn,
    *,
    source_id: int | None,
    assignment_id: int | None,
    verdict: str = "LIKE",
    ticker: str | None = None,
    company: str | None = None,
    origin: str = "BLIND_REVIEW",
    holdout_eligible: bool = True,
    parse_status: str = "PARSED",
    excluded: bool = False,
    screening_id: int | None = None,
    created_at: str = "2026-06-01T10:00:00+00:00",
) -> int:
    _counter["n"] += 1
    message_id = f"holdout-msg-{_counter['n']}"
    db.insert_message(
        conn,
        message_id=message_id,
        inbox_id=None,
        thread_id=None,
        received_at=created_at,
        sender="brad@example.com",
        recipients=None,
        subject="IdeaScout Blind Review",
        body_raw="judgment",
        body_format="text",
        size_bytes=1,
        stored_at=created_at,
    )
    db.set_feedback_parse_status(conn, message_id, parse_status)
    event = {
        "event_type": "BLIND_REVIEW",
        "verdict": verdict,
        "ticker": ticker,
        "company": company,
        "novelty": None,
        "user_comment": "",
        "parsed_json": "{}",
        "parser_version": blind_review.PARSER_VERSION,
        "model_name": blind_review.MODEL_NAME,
        "confidence": None,
        "created_at": created_at,
        "feedback_origin": origin,
        "holdout_eligible": holdout_eligible,
        "source_id": source_id,
        "screening_id": screening_id,
        "blind_review_assignment_id": assignment_id,
    }
    db.insert_feedback_events(conn, message_id=message_id, events=[event])
    feedback_id = conn.execute("SELECT feedback_id FROM feedback WHERE message_id = ?", (message_id,)).fetchone()[0]
    if excluded:
        conn.execute("UPDATE feedback SET excluded_from_learning = 1 WHERE feedback_id = ?", (feedback_id,))
        conn.commit()
    return feedback_id


def _holdout_member(conn, *, external_id: str, ticker: str, company: str,
                    discovered_at="2026-01-02T00:00:00+00:00", verdict="LIKE", screening=True,
                    **judgment_kwargs):
    """A fully valid BLIND_REVIEW holdout member unless overridden."""
    source_id = insert_extracted_source(
        conn, external_id=external_id, ticker=ticker, company=company, discovered_at=discovered_at
    )
    screening_id = insert_screening(conn, source_id=source_id, overall_prediction="WATCH") if screening else None
    assignment_id = db.record_blind_review_assignment(
        conn, source_id=source_id, assigned_at="2026-05-01T00:00:00+00:00", taste_version_at_assignment=2
    )
    feedback_id = _judge(
        conn, source_id=source_id, assignment_id=assignment_id, verdict=verdict, ticker=ticker, company=company,
        **judgment_kwargs,
    )
    return {"source_id": source_id, "assignment_id": assignment_id, "feedback_id": feedback_id,
            "screening_id": screening_id}


def _audit(config):
    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    audit = holdout_audit.audit_holdout(conn, latest)
    conn.close()
    return audit


def _all_rows(audit):
    return audit.official + audit.candidates + audit.other


def _render(config) -> str:
    return "\n".join(holdout_audit.render_lines(_audit(config)))


def _problems_for(config, feedback_id):
    return next(r for r in _all_rows(_audit(config)) if r.feedback_id == feedback_id).problems


def _post_checkpoint_screen_review(conn, *, ticker: str, company: str, screening_id: int | None = None) -> int:
    source_id = insert_extracted_source(conn, external_id=f"sr-{ticker}", ticker=ticker, company=company)
    sid = screening_id or insert_screening(conn, source_id=source_id, overall_prediction="WATCH")
    return _judge(conn, source_id=source_id, assignment_id=None, origin="SCREEN_REVIEW", holdout_eligible=False,
                  ticker=ticker, company=company, screening_id=sid)


def _post_checkpoint_digest_reply(conn, *, ticker: str, company: str) -> int:
    source_id = insert_extracted_source(conn, external_id=f"dr-{ticker}", ticker=ticker, company=company)
    insert_screening(conn, source_id=source_id, overall_prediction="WATCH")
    return _judge(conn, source_id=source_id, assignment_id=None, origin="DIGEST_REPLY", holdout_eligible=False,
                  ticker=ticker, company=company)


def _twenty_valid(conn):
    for i in range(20):
        _holdout_member(conn, external_id=str(1000 + i), ticker=f"H{i}", company=f"Holdout Co {i}")


# --- clean verdict and non-blind feedback -------------------------------------------------


def test_clean_twenty_unique_valid_holdout(tmp_path, monkeypatch):
    config, _ = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _twenty_valid(conn)
    conn.close()

    audit = _audit(config)

    assert audit.official_count == 20
    assert audit.unique_count == 20
    assert audit.valid_unique_count == 20
    assert audit.duplicate_groups == []
    assert audit.candidates == []
    assert audit.clean
    assert audit.reconciles
    assert "HOLDOUT CLEAN: 20 official rows, 20 unique valid ideas, no questionable blind candidates." in _render(config)


def test_screen_review_after_checkpoint_does_not_break_a_clean_holdout(tmp_path, monkeypatch):
    config, _ = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _twenty_valid(conn)
    _post_checkpoint_screen_review(conn, ticker="SR1", company="Screen Review Co")
    conn.close()

    audit = _audit(config)

    assert audit.official_count == 20
    assert len(audit.other) == 1 and audit.other[0].origin == "SCREEN_REVIEW"
    assert audit.candidates == []
    assert audit.clean
    assert "HOLDOUT CLEAN" in _render(config)


def test_digest_reply_after_checkpoint_does_not_break_a_clean_holdout(tmp_path, monkeypatch):
    config, _ = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _twenty_valid(conn)
    _post_checkpoint_digest_reply(conn, ticker="DR1", company="Digest Reply Co")
    conn.close()

    audit = _audit(config)

    assert audit.official_count == 20
    assert [r.origin for r in audit.other] == ["DIGEST_REPLY"]
    assert audit.clean
    assert "HOLDOUT CLEAN" in _render(config)


def test_non_blind_rows_are_reported_separately_not_as_invalid(tmp_path, monkeypatch):
    config, _ = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _twenty_valid(conn)
    sr_id = _post_checkpoint_screen_review(conn, ticker="SR2", company="Screen Co Two")
    dr_id = _post_checkpoint_digest_reply(conn, ticker="DR2", company="Digest Co Two")
    conn.close()

    audit = _audit(config)
    rendered = _render(config)

    assert {r.feedback_id for r in audit.other} == {sr_id, dr_id}
    assert all(r.feedback_id not in {m.feedback_id for m in audit.official} for r in audit.other)
    assert "OTHER POST-CHECKPOINT FEEDBACK" in rendered
    assert f"FB {sr_id} | origin SCREEN_REVIEW" in rendered
    assert f"FB {dr_id} | origin DIGEST_REPLY" in rendered
    assert "not part of holdout" in rendered
    assert "Post-training feedback rows (feedback_id > checkpoint): 22" in rendered
    assert "Official blind holdout rows (taste-status predicate): 20" in rendered
    assert "Other post-checkpoint feedback (not part of holdout): 2" in rendered
    assert "Totals reconcile: YES" in rendered


def test_duplicate_detection_excludes_unrelated_non_holdout_feedback(tmp_path, monkeypatch):
    config, _ = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _twenty_valid(conn)
    member_screening = _holdout_member_screening_id(conn, "H3")
    _post_checkpoint_screen_review(conn, ticker="H3", company="Holdout Co 3", screening_id=member_screening)
    conn.close()

    audit = _audit(config)

    assert audit.duplicate_groups == []
    assert audit.clean


def _holdout_member_screening_id(conn, ticker: str) -> int:
    return conn.execute(
        "SELECT screening_id FROM source_screenings ss JOIN collected_sources cs ON cs.source_id = ss.source_id "
        "WHERE cs.ticker = ? ORDER BY screening_id DESC LIMIT 1",
        (ticker,),
    ).fetchone()[0]


def test_malformed_blind_candidate_makes_holdout_not_clean(tmp_path, monkeypatch):
    config, _ = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _twenty_valid(conn)
    bad = _holdout_member(conn, external_id="2001", ticker="H30", company="Holdout Co 30", holdout_eligible=False)
    conn.close()

    audit = _audit(config)
    rendered = _render(config)

    assert audit.official_count == 20
    assert [r.feedback_id for r in audit.candidates] == [bad["feedback_id"]]
    assert "holdout_eligible=0" in _problems_for(config, bad["feedback_id"])
    assert not audit.clean
    assert "QUESTIONABLE BLIND HOLDOUT CANDIDATES" in rendered
    assert "HOLDOUT NOT CLEAN" in rendered


def test_excluded_blind_candidate_makes_holdout_not_clean(tmp_path, monkeypatch):
    config, _ = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _twenty_valid(conn)
    _holdout_member(conn, external_id="2002", ticker="H31", company="Holdout Co 31", excluded=True)
    conn.close()

    audit = _audit(config)

    assert audit.official_count == 20
    assert len(audit.candidates) == 1
    assert not audit.clean


def test_official_count_matches_taste_status_predicate(tmp_path, monkeypatch):
    config, latest = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _twenty_valid(conn)
    _holdout_member(conn, external_id="2003", ticker="H32", company="Holdout Co 32", holdout_eligible=False)
    _post_checkpoint_screen_review(conn, ticker="SR3", company="Screen Co Three")
    taste_status = {r["feedback_id"] for r in db.get_eligible_feedback_after(conn, latest["checkpoint_feedback_id"])}
    conn.close()

    audit = _audit(config)

    assert {r.feedback_id for r in audit.official} == taste_status
    assert len(taste_status) == 20
    assert audit.reconciles


# --- duplicate detection ------------------------------------------------------------------


def test_duplicate_by_source_external_id(tmp_path, monkeypatch):
    config, _ = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    first = _holdout_member(conn, external_id="500", ticker="H1", company="Holdout Co 1",
                            discovered_at="2026-01-02T00:00:00+00:00")
    second = _holdout_member(conn, external_id="500", ticker="H1X", company="Holdout Co 1X",
                             discovered_at="2026-02-02T00:00:00+00:00")
    conn.close()

    [group] = _audit(config).duplicate_groups

    assert group.identity_level == "D"
    assert {m.assignment_id for m in group.members} == {first["assignment_id"], second["assignment_id"]}


def test_duplicate_by_screening_id(tmp_path, monkeypatch):
    config, _ = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    a = _holdout_member(conn, external_id="601", ticker="H2", company="Holdout Co 2")
    b = _holdout_member(conn, external_id="602", ticker="H3", company="Holdout Co 3", screening=False)
    conn.execute("UPDATE feedback SET screening_id = ? WHERE feedback_id = ?", (a["screening_id"], b["feedback_id"]))
    conn.commit()
    conn.close()

    [group] = _audit(config).duplicate_groups

    assert group.identity_level == "C"
    assert {m.feedback_id for m in group.members} == {a["feedback_id"], b["feedback_id"]}


def test_duplicate_by_ticker(tmp_path, monkeypatch):
    config, _ = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _holdout_member(conn, external_id="701", ticker="H4", company="Alpha Co")
    _holdout_member(conn, external_id="702", ticker="h4", company="Beta Co")
    conn.close()

    [group] = _audit(config).duplicate_groups

    assert group.identity_level == "E"
    assert group.identity_value == "H4"


def test_duplicate_by_company_with_normalized_punctuation(tmp_path, monkeypatch):
    config, _ = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _holdout_member(conn, external_id="801", ticker="H5", company="Acme Holdings, Inc.")
    _holdout_member(conn, external_id="802", ticker="H6", company="acme holdings inc")
    conn.close()

    [group] = _audit(config).duplicate_groups

    assert group.identity_level == "F"


def test_cprt_like_duplicate_with_two_br_refs_is_flagged_and_named(tmp_path, monkeypatch):
    config, _ = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    first = _holdout_member(conn, external_id="901", ticker="CPRT", company="Capricorn Corp")
    second = _holdout_member(conn, external_id="902", ticker="CPRT", company="Capricorn Corp")
    conn.close()

    audit = _audit(config)
    rendered = _render(config)

    [group] = audit.duplicate_groups
    refs = {m.ref for m in group.members}
    assert refs == {
        blind_review.format_review_ref(first["assignment_id"]),
        blind_review.format_review_ref(second["assignment_id"]),
    }
    assert {m.ref for m in audit.named_checks["CPRT"]} == refs
    assert "Named check CPRT: 2 row(s)" in rendered
    assert "HOLDOUT NOT CLEAN" in rendered
    assert "Recommended action: exclude one duplicate" in rendered
    assert not audit.clean


def test_duplicate_with_conflicting_labels_is_reported(tmp_path, monkeypatch):
    config, _ = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _holdout_member(conn, external_id="911", ticker="H7", company="Gamma Co", verdict="LIKE")
    _holdout_member(conn, external_id="912", ticker="H7", company="Gamma Co", verdict="PASS")
    conn.close()

    rendered = _render(config)

    assert "CONFLICTING LABELS" in rendered
    assert "labels: LIKE, PASS" in rendered


# --- validity -----------------------------------------------------------------------------


def test_training_judgment_overlap_is_flagged_for_a_real_holdout_member(tmp_path, monkeypatch):
    config, _ = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    member = _holdout_member(conn, external_id="1010", ticker="T1", company="Training Overlap Co")
    conn.close()

    audit = _audit(config)
    problems = _problems_for(config, member["feedback_id"])

    assert member["feedback_id"] in {r.feedback_id for r in audit.official}
    assert any("matches training judgment" in p for p in problems)
    assert not audit.clean


def test_excluded_feedback_is_flagged_and_still_reconciles(tmp_path, monkeypatch):
    config, _ = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    member = _holdout_member(conn, external_id="1001", ticker="H8", company="Delta Co", excluded=True)
    conn.close()

    assert "excluded_from_learning=1" in _problems_for(config, member["feedback_id"])
    assert _audit(config).reconciles


def test_non_parsed_feedback_is_flagged(tmp_path, monkeypatch):
    config, _ = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    member = _holdout_member(conn, external_id="1002", ticker="H9", company="Epsilon Co", parse_status="NEEDS_REVIEW")
    conn.close()

    assert "feedback_parse_status is NEEDS_REVIEW" in _problems_for(config, member["feedback_id"])


def test_official_non_blind_origin_is_flagged_as_invalid(tmp_path, monkeypatch):
    config, _ = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    source_id = insert_extracted_source(conn, external_id="1003", ticker="H10", company="Zeta Co")
    insert_screening(conn, source_id=source_id, overall_prediction="WATCH")
    feedback_id = _judge(conn, source_id=source_id, assignment_id=None, origin="DIRECT", ticker="H10", company="Zeta Co")
    conn.close()

    problems = _problems_for(config, feedback_id)

    assert "counted as an official holdout member but origin is DIRECT" in problems
    assert "no blind-review assignment is linked" not in problems


def test_holdout_ineligible_feedback_is_flagged(tmp_path, monkeypatch):
    config, _ = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    member = _holdout_member(conn, external_id="1004", ticker="H11", company="Eta Co", holdout_eligible=False)
    conn.close()

    assert "holdout_eligible=0" in _problems_for(config, member["feedback_id"])


def test_smoke_test_data_makes_holdout_not_clean(tmp_path, monkeypatch):
    config, _ = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    member = _holdout_member(conn, external_id="1005", ticker="TESTCO", company="Test Co Ltd")
    xyz = _holdout_member(conn, external_id="1006", ticker="XYZ", company="Smoke Co")
    conn.close()

    assert "smoke-test marker TESTCO" in _problems_for(config, member["feedback_id"])
    assert "smoke-test marker XYZ" in _problems_for(config, xyz["feedback_id"])
    assert not _audit(config).clean


def test_two_usable_judgments_for_one_assignment_are_flagged(tmp_path, monkeypatch):
    config, _ = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    member = _holdout_member(conn, external_id="1007", ticker="H12", company="Theta Co")
    _judge(conn, source_id=member["source_id"], assignment_id=member["assignment_id"],
           ticker="H12", company="Theta Co", created_at="2026-06-02T10:00:00+00:00")
    conn.close()

    assert "2 usable Brad judgments for this assignment (need exactly 1)" in _problems_for(config, member["feedback_id"])


def test_source_shown_in_digest_before_judgment_is_flagged(tmp_path, monkeypatch):
    config, _ = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    member = _holdout_member(conn, external_id="1008", ticker="H13", company="Iota Co")
    conn.execute(
        "INSERT INTO digest_shown_sources (source_id, shown_at) VALUES (?, ?)",
        (member["source_id"], "2026-05-15T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    assert any(p.startswith("source was shown in a digest") for p in _problems_for(config, member["feedback_id"]))


def test_missing_screening_is_flagged(tmp_path, monkeypatch):
    config, _ = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    member = _holdout_member(conn, external_id="1009", ticker="H14", company="Kappa Co", screening=False)
    conn.close()

    assert "no screening recorded for this source" in _problems_for(config, member["feedback_id"])


# --- read-only, ordering, and CLI -----------------------------------------------------------


def test_audit_is_read_only(tmp_path, monkeypatch):
    config, _ = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _holdout_member(conn, external_id="1101", ticker="CPRT", company="Capricorn Corp")
    _holdout_member(conn, external_id="1102", ticker="CPRT", company="Capricorn Corp", excluded=True)
    _post_checkpoint_screen_review(conn, ticker="SR9", company="Screen Co Nine")
    conn.close()
    before = _snapshot_all_tables(config)

    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.cmd_audit_holdout_integrity(config) == 0
    _render(config)

    assert _snapshot_all_tables(config) == before
    conn = db.connect(config.database_path)
    assert conn.execute("SELECT build_unlocked FROM taste_versions ORDER BY version_number DESC LIMIT 1").fetchone()[0] == 0
    conn.close()


def test_audit_creates_no_assignments_and_changes_no_feedback(tmp_path, monkeypatch):
    config, _ = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _holdout_member(conn, external_id="1103", ticker="H15", company="Lambda Co", excluded=True)
    conn.close()

    conn = db.connect(config.database_path)
    assignments_before = conn.execute("SELECT COUNT(*) FROM blind_review_assignments").fetchone()[0]
    feedback_before = [tuple(r) for r in conn.execute("SELECT * FROM feedback ORDER BY feedback_id")]
    conn.close()

    with contextlib.redirect_stdout(io.StringIO()):
        cli.cmd_audit_holdout_integrity(config)

    conn = db.connect(config.database_path)
    assert conn.execute("SELECT COUNT(*) FROM blind_review_assignments").fetchone()[0] == assignments_before
    assert [tuple(r) for r in conn.execute("SELECT * FROM feedback ORDER BY feedback_id")] == feedback_before
    conn.close()


def test_output_is_deterministic_and_ordered(tmp_path, monkeypatch):
    config, _ = _setup(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _holdout_member(conn, external_id="1201", ticker="CPRT", company="Capricorn Corp")
    _holdout_member(conn, external_id="1202", ticker="H16", company="Mu Co")
    _holdout_member(conn, external_id="1203", ticker="CPRT", company="Capricorn Corp")
    conn.close()

    first = _render(config)
    second = _render(config)
    audit = _audit(config)

    assert first == second
    expected_ids = sorted(r.feedback_id for r in audit.official)
    assert [r.feedback_id for r in audit.official] == expected_ids
    membership = [line for line in first.splitlines() if line.startswith("  BR-")]
    assert [line.split("|")[1].strip() for line in membership] == [f"feedback {fid}" for fid in expected_ids]


def test_empty_holdout_reports_not_complete(tmp_path, monkeypatch):
    config, _ = _setup(tmp_path, monkeypatch)

    rendered = _render(config)

    assert "Post-training feedback rows (feedback_id > checkpoint): 0" in rendered
    assert "HOLDOUT NOT CLEAN" in rendered
    assert "Replacement blind-review judgment required: 20" in rendered


def test_no_taste_version_prints_message(tmp_path):
    config = make_config(tmp_path)
    db.connect(config.database_path).close()
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        assert cli.cmd_audit_holdout_integrity(config) == 0
    assert "No taste model has been generated yet" in buffer.getvalue()
