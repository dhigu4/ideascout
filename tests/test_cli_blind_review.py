"""Tests for `python run.py blind-review [--limit N]` and
`blind-review-results [--limit N]` (Stage 11): assignment, exposure
persistence, contamination-safety exclusions, and the no-leakage display
guarantee. Feedback creation/parsing is covered separately in
tests/test_cli_parse_mail_blind_review.py.

Every database lives under pytest's tmp_path; nothing here ever touches
production state, and nothing calls a real LLM or AgentMail.
"""

from __future__ import annotations

import dataclasses

from ideascout import blind_review, cli, db
from tests.test_cli_build_taste import make_config as _base_make_config
from tests.test_cli_build_taste import insert_n_eligible, patch_taste


def make_config(tmp_path):
    config = _base_make_config(tmp_path)
    return dataclasses.replace(config, brad_allowed_senders=frozenset({"brad@example.com"}))


def insert_extracted_source(
    conn,
    *,
    external_id: str,
    ticker: str | None = None,
    company: str | None = None,
    discovered_at: str = "2026-01-01T00:00:00+00:00",
    extraction_status: str = "EXTRACTED",
    previous_version_source_id: int | None = None,
) -> int:
    ticker = ticker if ticker is not None else f"T{external_id}"
    company = company if company is not None else f"Company {external_id}"
    source_id = db.insert_collected_source(
        conn,
        source_name="yellowbrick",
        external_id=external_id,
        canonical_url=f"https://www.joinyellowbrick.com/sp/{external_id}",
        discovered_at=discovered_at,
        discovery_title=company,
        source_date=None,
        source_title=company,
        author=None,
        ticker=ticker,
        company=company,
        source_type="stock_pitch",
        content_hash=f"hash-{external_id}-{discovered_at}",
        raw_html_path=f"/x/{external_id}.html",
        metadata_json="{}",
        created_at=discovered_at,
        previous_version_source_id=previous_version_source_id,
    )
    if extraction_status == "EXTRACTED":
        db.update_collected_source_extracted(
            conn,
            source_id=source_id,
            company=company,
            ticker=ticker,
            source_title=company,
            source_date=None,
            business_summary="A business.",
            core_thesis="A thesis.",
            why_mispriced="A reason.",
            future_earnings_change="Up.",
            upside_case="Upside.",
            downside_or_key_risks="Risk.",
            catalysts="A catalyst.",
            what_must_be_true="Something.",
            evidence_of_market_misunderstanding="Evidence.",
            known_unknowns="Unknowns.",
            extraction_model_name="fake-model",
            extracted_at=discovered_at,
        )
    return source_id


def insert_screening(conn, *, source_id: int, overall_prediction: str, taste_version: int = 1, content_hash="hash") -> int:
    return db.insert_source_screening(
        conn,
        source_id=source_id,
        created_at="2026-01-01T00:00:00+00:00",
        taste_version=taste_version,
        taste_sha256="tastehash",
        screen_rules_path="IDEA_SCREEN_RULES.md",
        screen_rules_sha256="ruleshash",
        content_hash=content_hash,
        model_name="fake-model",
        overall_prediction=overall_prediction,
        mispricing="Plausible",
        variant_perception="Plausible",
        upside="Potentially sufficient",
        business_quality="Plausible",
        downside="Acceptable",
        key_reasons_json="[]",
        key_concerns_json="[]",
        critical_questions_json="[]",
        confidence="MEDIUM",
    )


# --- basic selection / assignment -------------------------------------------------


def test_blind_review_assigns_up_to_limit_oldest_first(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    ids = [
        insert_extracted_source(conn, external_id=str(i), discovered_at=f"2026-01-01T00:0{i}:00+00:00")
        for i in range(8)
    ]
    conn.close()

    exit_code = cli.cmd_blind_review(config, limit=4)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    assigned = conn.execute("SELECT source_id FROM blind_review_assignments ORDER BY assignment_id").fetchall()
    conn.close()
    assert [row["source_id"] for row in assigned] == ids[:4]


def test_no_candidates_prints_clear_message(tmp_path, capsys):
    config = make_config(tmp_path)
    exit_code = cli.cmd_blind_review(config, limit=4)
    assert exit_code == 0
    assert "No new blind-review candidates" in capsys.readouterr().out


# --- exposure persistence / rerun stability ---------------------------------------


def test_assignment_persisted_on_display(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_extracted_source(conn, external_id="1")
    conn.close()

    cli.cmd_blind_review(config, limit=1)

    conn = db.connect(config.database_path)
    count = conn.execute("SELECT COUNT(*) FROM blind_review_assignments").fetchone()[0]
    conn.close()
    assert count == 1


def test_rerun_does_not_create_a_fresh_unseen_status(tmp_path):
    """Rerunning with the same limit, without judging anything in between,
    must re-show the SAME assignments (stable BR refs), never substitute
    or duplicate them.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    for i in range(8):
        insert_extracted_source(conn, external_id=str(i), discovered_at=f"2026-01-01T00:0{i}:00+00:00")
    conn.close()

    cli.cmd_blind_review(config, limit=4)
    conn = db.connect(config.database_path)
    first_ids = {row["assignment_id"] for row in conn.execute("SELECT assignment_id FROM blind_review_assignments")}
    conn.close()

    cli.cmd_blind_review(config, limit=4)
    conn = db.connect(config.database_path)
    second_ids = {row["assignment_id"] for row in conn.execute("SELECT assignment_id FROM blind_review_assignments")}
    conn.close()

    assert first_ids == second_ids
    assert len(second_ids) == 4


def test_stable_br_refs_across_reruns(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_extracted_source(conn, external_id="1")
    conn.close()

    cli.cmd_blind_review(config, limit=1)
    first_output = capsys.readouterr().out
    cli.cmd_blind_review(config, limit=1)
    second_output = capsys.readouterr().out

    assert "BR-1" in first_output
    assert "BR-1" in second_output


def test_increasing_limit_adds_new_items_without_disturbing_existing(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    for i in range(8):
        insert_extracted_source(conn, external_id=str(i), discovered_at=f"2026-01-01T00:0{i}:00+00:00")
    conn.close()

    cli.cmd_blind_review(config, limit=4)
    conn = db.connect(config.database_path)
    first_ids = {row["assignment_id"] for row in conn.execute("SELECT assignment_id FROM blind_review_assignments")}
    conn.close()

    cli.cmd_blind_review(config, limit=6)
    conn = db.connect(config.database_path)
    second_ids = {row["assignment_id"] for row in conn.execute("SELECT assignment_id FROM blind_review_assignments")}
    conn.close()

    assert first_ids <= second_ids
    assert len(second_ids) == 6


def test_judged_item_frees_a_slot_for_a_new_one(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    for i in range(8):
        insert_extracted_source(conn, external_id=str(i), discovered_at=f"2026-01-01T00:0{i}:00+00:00")
    conn.close()

    cli.cmd_blind_review(config, limit=4)

    conn = db.connect(config.database_path)
    first_assignment_id = conn.execute("SELECT MIN(assignment_id) FROM blind_review_assignments").fetchone()[0]
    ref = blind_review.format_review_ref(first_assignment_id)
    db.insert_message(
        conn, message_id="msg_judge_1", inbox_id="inbox_abc", thread_id="thread_1",
        received_at="2026-01-02T00:00:00+00:00", sender="brad@example.com",
        recipients="ideas@yourdomain.agentmail.to", subject="IdeaScout Blind Review",
        body_raw=f"{ref} - LIKE\ngood", body_format="text", size_bytes=10,
        stored_at="2026-01-02T00:00:05+00:00", sender_authenticated="AUTHENTICATED",
    )
    conn.close()
    cli.cmd_parse_mail(config)

    cli.cmd_blind_review(config, limit=4)
    conn = db.connect(config.database_path)
    total = conn.execute("SELECT COUNT(*) FROM blind_review_assignments").fetchone()[0]
    conn.close()
    assert total == 5  # 4 original + 1 new, filling the freed slot


# --- contamination exclusions -----------------------------------------------------


def test_not_yet_extracted_sources_are_excluded(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_extracted_source(conn, external_id="1", extraction_status="PENDING")
    conn.close()

    cli.cmd_blind_review(config, limit=4)
    conn = db.connect(config.database_path)
    count = conn.execute("SELECT COUNT(*) FROM blind_review_assignments").fetchone()[0]
    conn.close()
    assert count == 0


def test_digest_exposed_sources_are_excluded(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    shown_id = insert_extracted_source(conn, external_id="1")
    not_shown_id = insert_extracted_source(conn, external_id="2")
    db.mark_source_shown_in_digest(conn, shown_id, "2026-01-01T00:00:00+00:00")
    conn.close()

    cli.cmd_blind_review(config, limit=4)
    conn = db.connect(config.database_path)
    assigned = {row["source_id"] for row in conn.execute("SELECT source_id FROM blind_review_assignments")}
    conn.close()
    assert shown_id not in assigned
    assert not_shown_id in assigned


def test_screen_reviewed_sources_are_excluded(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    reviewed_id = insert_extracted_source(conn, external_id="1", ticker=None, company=None)
    screening_id = insert_screening(conn, source_id=reviewed_id, overall_prediction="WATCH")
    db.insert_message(
        conn, message_id="msg_sr", inbox_id="inbox_abc", thread_id="thread_sr",
        received_at="2026-01-01T00:00:00+00:00", sender="brad@example.com",
        recipients="ideas@yourdomain.agentmail.to", subject="IdeaScout Screen Review",
        body_raw="x", body_format="text", size_bytes=1, stored_at="2026-01-01T00:00:05+00:00",
        sender_authenticated="AUTHENTICATED",
    )
    db.insert_feedback_events(
        conn, message_id="msg_sr",
        events=[{
            "event_type": "FEEDBACK", "verdict": "PASS", "ticker": None, "company": None,
            "novelty": None, "user_comment": "reviewed", "parsed_json": "{}",
            "parser_version": "screen-review-v1", "model_name": "deterministic", "confidence": None,
            "created_at": "2026-01-01T00:00:00+00:00", "feedback_origin": "SCREEN_REVIEW",
            "holdout_eligible": False, "source_id": reviewed_id, "screening_id": screening_id,
        }],
    )
    not_reviewed_id = insert_extracted_source(conn, external_id="2", ticker=None, company=None)
    conn.close()

    cli.cmd_blind_review(config, limit=4)
    conn = db.connect(config.database_path)
    assigned = {row["source_id"] for row in conn.execute("SELECT source_id FROM blind_review_assignments")}
    conn.close()
    assert reviewed_id not in assigned
    assert not_reviewed_id in assigned


def test_prior_direct_feedback_ticker_excludes_source(tmp_path):
    """A ticker with prior learning-eligible DIRECT feedback must never be
    offered as a blind-review candidate, even from a totally different
    source_id/message.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    already_judged_id = insert_extracted_source(conn, external_id="1", ticker="XYZ", company="Xyz Corp")
    fresh_id = insert_extracted_source(conn, external_id="2", ticker="ABC", company="Abc Corp")
    db.insert_message(
        conn, message_id="msg_direct", inbox_id="inbox_abc", thread_id="thread_direct",
        received_at="2026-01-01T00:00:00+00:00", sender="brad@example.com",
        recipients="ideas@yourdomain.agentmail.to", subject="An idea",
        body_raw="LIKE, XYZ", body_format="text", size_bytes=1, stored_at="2026-01-01T00:00:05+00:00",
        sender_authenticated="AUTHENTICATED",
    )
    db.insert_feedback(
        conn, message_id="msg_direct", event_type="FEEDBACK", verdict="LIKE", ticker="XYZ",
        company=None, novelty="UNKNOWN", user_comment="already judged", parsed_json="{}",
        parser_version="v1", model_name="claude-haiku-4-5", confidence=0.9,
        created_at="2026-01-01T00:00:00+00:00",
    )
    db.set_feedback_parse_status(conn, "msg_direct", "PARSED")
    conn.close()

    cli.cmd_blind_review(config, limit=4)
    conn = db.connect(config.database_path)
    assigned = {row["source_id"] for row in conn.execute("SELECT source_id FROM blind_review_assignments")}
    conn.close()
    assert already_judged_id not in assigned
    assert fresh_id in assigned


def test_prior_blind_review_exposure_excludes_re_assignment(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = insert_extracted_source(conn, external_id="1")
    conn.close()

    cli.cmd_blind_review(config, limit=1)  # assigns source_id=1

    conn = db.connect(config.database_path)
    candidates = db.get_blind_review_candidate_sources(conn)
    conn.close()
    assert source_id not in {row["source_id"] for row in candidates}


def test_smoke_test_records_are_excluded(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    smoke_id = insert_extracted_source(conn, external_id="smoke1", ticker="TESTCO", company="Test Company Inc")
    real_id = insert_extracted_source(conn, external_id="2", ticker="ABC", company="Abc Corp")
    conn.close()

    cli.cmd_blind_review(config, limit=4)
    conn = db.connect(config.database_path)
    assigned = {row["source_id"] for row in conn.execute("SELECT source_id FROM blind_review_assignments")}
    conn.close()
    assert smoke_id not in assigned
    assert real_id in assigned


def test_superseded_source_version_is_excluded(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    old_id = insert_extracted_source(
        conn, external_id="dup", ticker="DUP", discovered_at="2026-01-01T00:00:00+00:00"
    )
    new_id = insert_extracted_source(
        conn, external_id="dup", ticker="DUP", discovered_at="2026-01-02T00:00:00+00:00",
        previous_version_source_id=old_id,
    )
    conn.close()

    cli.cmd_blind_review(config, limit=4)
    conn = db.connect(config.database_path)
    assigned = {row["source_id"] for row in conn.execute("SELECT source_id FROM blind_review_assignments")}
    conn.close()
    assert old_id not in assigned
    assert new_id in assigned


# --- screening-score independence (THE critical test) -----------------------------


def test_selection_is_independent_of_screening_prediction_change(tmp_path):
    """Changing a source_screening's prediction from PASS to
    INVESTIGATE_NOW must NEVER change blind-review eligibility or
    ordering -- selection never reads source_screenings at all.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    ids = [insert_extracted_source(conn, external_id=str(i), discovered_at=f"2026-01-01T00:0{i}:00+00:00") for i in range(3)]
    # A screening exists for one of them, currently PASS.
    insert_screening(conn, source_id=ids[1], overall_prediction="PASS", content_hash="hash-1-v1")
    candidates_before = db.get_blind_review_candidate_sources(conn)
    order_before = [row["source_id"] for row in candidates_before]
    conn.close()

    conn = db.connect(config.database_path)
    # Flip the SAME screening's prediction (simulating a later re-screen).
    conn.execute("UPDATE source_screenings SET overall_prediction = 'INVESTIGATE_NOW' WHERE source_id = ?", (ids[1],))
    conn.commit()
    candidates_after = db.get_blind_review_candidate_sources(conn)
    order_after = [row["source_id"] for row in candidates_after]
    conn.close()

    assert order_before == order_after
    assert set(order_before) == set(ids)


def test_selection_query_never_references_source_screenings_columns(tmp_path):
    """A direct proof of independence: the EXACT SQL text actually
    executed by get_blind_review_candidate_sources contains no reference
    to overall_prediction/confidence/mispricing/variant_perception/
    key_reasons/key_concerns -- it cannot be influenced by them because it
    never reads them, not merely because a test happens not to exercise
    that path. Captured via sqlite3's trace callback rather than static
    source inspection, so this can't be fooled by comments/docstrings.
    """
    conn = db.connect(tmp_path / "ideas.db")
    executed_queries: list[str] = []
    conn.set_trace_callback(executed_queries.append)

    db.get_blind_review_candidate_sources(conn)
    conn.set_trace_callback(None)
    conn.close()

    combined = "\n".join(executed_queries).lower()
    for forbidden in (
        "overall_prediction",
        "confidence",
        "mispricing",
        "variant_perception",
        "key_reasons",
        "key_concerns",
    ):
        assert forbidden not in combined, f"query text unexpectedly references {forbidden!r}"


# --- no-leakage display guarantee -------------------------------------------------


def test_display_contains_no_prediction_confidence_reasons_concerns(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = insert_extracted_source(conn, external_id="1", ticker="ABC", company="Abc Corp")
    insert_screening(conn, source_id=source_id, overall_prediction="INVESTIGATE_NOW")
    conn.close()

    cli.cmd_blind_review(config, limit=4)
    output = capsys.readouterr().out

    for forbidden in (
        "INVESTIGATE_NOW",
        "WATCH",
        "Taste v",
        "confidence",
        "Mispricing",
        "Variant perception",
    ):
        assert forbidden not in output
    assert "BR-1" in output
    assert "Abc Corp" in output


def test_taste_version_at_assignment_is_never_displayed(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()
    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    conn = db.connect(config.database_path)
    insert_extracted_source(conn, external_id="1")
    conn.close()

    capsys.readouterr()  # discard cmd_build_taste's own "Generated taste v1..." output
    cli.cmd_blind_review(config, limit=1)
    output = capsys.readouterr().out
    assert "v1" not in output.replace("BR-1", "").replace("2026-01-01", "")


# --- blind-review-results reveal gating -------------------------------------------


def test_results_never_shows_unjudged_assignments(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_extracted_source(conn, external_id="1")
    conn.close()
    cli.cmd_blind_review(config, limit=1)
    capsys.readouterr()

    exit_code = cli.cmd_blind_review_results(config, limit=20)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "No judged blind-review items yet." in output


def test_results_reveals_prediction_only_after_judgment(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id = insert_extracted_source(conn, external_id="1", ticker="ABC", company="Abc Corp")
    insert_screening(conn, source_id=source_id, overall_prediction="INVESTIGATE_NOW", taste_version=2)
    conn.close()
    cli.cmd_blind_review(config, limit=1)

    conn = db.connect(config.database_path)
    db.insert_message(
        conn, message_id="msg_1", inbox_id="inbox_abc", thread_id="thread_1",
        received_at="2026-01-02T00:00:00+00:00", sender="brad@example.com",
        recipients="ideas@yourdomain.agentmail.to", subject="IdeaScout Blind Review",
        body_raw="BR-1 - LIKE\ngood setup", body_format="text", size_bytes=10,
        stored_at="2026-01-02T00:00:05+00:00", sender_authenticated="AUTHENTICATED",
    )
    conn.close()
    cli.cmd_parse_mail(config)

    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exit_code = cli.cmd_blind_review_results(config, limit=20)
    output = buf.getvalue()

    assert exit_code == 0
    assert "BR-1" in output
    assert "LIKE" in output
    assert "INVESTIGATE_NOW" in output
    assert "Taste v2" in output
    assert "Categorical mapping matched: YES" in output


def test_blind_review_results_works_at_20_of_20_holdout(tmp_path, monkeypatch):
    """blind-review-results must remain fully usable once the clean
    holdout reaches 20/20 -- the holdout-governance fix (Taste stays
    frozen at 20/20 until explicitly unlocked) only affects build-taste,
    never this read-only reveal command.
    """
    from tests.test_cli_build_taste import insert_n_eligible as insert_n_eligible_direct
    from tests.test_cli_build_taste import patch_taste

    import io
    import contextlib

    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible_direct(conn, 15)
    conn.close()
    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    conn = db.connect(config.database_path)
    for i in range(20):
        source_id = insert_extracted_source(conn, external_id=f"br{i}", ticker=f"BR{i}", company=f"BR{i} Corp")
        assignment_id = db.record_blind_review_assignment(
            conn, source_id=source_id, assigned_at="2026-01-01T00:00:00+00:00", taste_version_at_assignment=1
        )
        ref = blind_review.format_review_ref(assignment_id)
        db.insert_message(
            conn, message_id=f"msg_blind_{i}", inbox_id="inbox_abc", thread_id=f"thread_{i}",
            received_at="2026-01-02T00:00:00+00:00", sender="brad@example.com",
            recipients="ideas@yourdomain.agentmail.to", subject="IdeaScout Blind Review",
            body_raw=f"{ref} - LIKE\njudgment {i}", body_format="text", size_bytes=10,
            stored_at="2026-01-02T00:00:05+00:00", sender_authenticated="AUTHENTICATED",
        )
    conn.close()
    cli.cmd_parse_mail(config)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exit_code = cli.cmd_blind_review_results(config, limit=20)
    output = buf.getvalue()

    assert exit_code == 0
    assert "BR-1" in output
    assert output.count("Brad's verdict: LIKE") == 20
