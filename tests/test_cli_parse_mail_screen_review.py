"""Tests for SCREEN_REVIEW feedback (Stage 10).

An "IdeaScout Screen Review" email (or a Re:/Fwd: of one) is routed
through the deterministic, no-LLM ideascout/screen_review.py parser
instead of the LLM-based direct-feedback parser in cmd_parse_mail.
Everything it produces is LEARNING ELIGIBLE (feedback_origin=
'SCREEN_REVIEW', excluded_from_learning stays 0) but structurally
HOLDOUT INELIGIBLE (holdout_eligible=0) -- Brad has already seen
IdeaScout's own past screen result for the exact screening he's judging.

No real LLM call, no real AgentMail call, no production database --
everything lives under pytest's tmp_path, exactly like every other cli
test module.
"""

from __future__ import annotations

from ideascout import cli, db, parser, screen_review, taste
from tests.test_cli_build_taste import insert_n_eligible, patch_taste
from tests.test_cli_parse_mail import get_status, make_config


def insert_screening(
    conn,
    *,
    external_id: str,
    ticker: str | None = "ABC",
    company: str | None = "Abc Corp",
    overall_prediction: str = "WATCH",
    taste_version: int = 1,
) -> tuple[int, int]:
    content_hash = f"hash-{external_id}-v{taste_version}"
    source_id = db.insert_collected_source(
        conn,
        source_name="yellowbrick",
        external_id=external_id,
        canonical_url=f"https://www.joinyellowbrick.com/sp/{external_id}",
        discovered_at="2026-01-01T00:00:00+00:00",
        discovery_title=company,
        source_date=None,
        source_title=company,
        author=None,
        ticker=ticker,
        company=company,
        source_type="stock_pitch",
        content_hash=content_hash,
        raw_html_path=f"/x/{external_id}.html",
        metadata_json="{}",
        created_at="2026-01-01T00:00:00+00:00",
    )
    screening_id = db.insert_source_screening(
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
    return source_id, screening_id


def insert_review_message(
    conn,
    message_id: str,
    *,
    subject: str = "IdeaScout Screen Review",
    body_raw: str = "SR-1 - LIKE\ngood",
    sender: str = "brad@example.com",
    sender_authenticated: str | None = "AUTHENTICATED",
) -> None:
    db.insert_message(
        conn,
        message_id=message_id,
        inbox_id="inbox_abc",
        thread_id="thread_review",
        received_at="2026-01-02T12:00:00+00:00",
        sender=sender,
        recipients="ideas@yourdomain.agentmail.to",
        subject=subject,
        body_raw=body_raw,
        body_format="text",
        size_bytes=100,
        stored_at="2026-01-02T12:00:05+00:00",
        sender_authenticated=sender_authenticated,
    )


# --- routing --------------------------------------------------------------------


def test_exact_subject_routes_to_screen_review_parser(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    _, sid = insert_screening(conn, external_id="1")
    ref = screen_review.format_review_ref(sid)
    insert_review_message(conn, "msg_1", body_raw=f"{ref} - LIKE\ngood margins")
    conn.close()

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "PARSED"
    events = db.get_feedback_events_for_message(conn, "msg_1")
    assert len(events) == 1
    assert events[0]["feedback_origin"] == "SCREEN_REVIEW"
    conn.close()


def test_re_and_fwd_subject_prefixes_are_accepted(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    _, sid = insert_screening(conn, external_id="1")
    ref = screen_review.format_review_ref(sid)
    insert_review_message(conn, "msg_1", subject="Re: IdeaScout Screen Review", body_raw=f"{ref} - LIKE\ngood")
    insert_review_message(conn, "msg_2", subject="Fwd: IdeaScout Screen Review", body_raw=f"{ref} - PASS\nno")
    conn.close()

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    # msg_2 will fail closed as a duplicate of the same screening once
    # msg_1 succeeds -- that's fine, this test is only about subject
    # detection, not about two independent successful reviews.
    assert get_status(conn, "msg_1") == "PARSED"
    conn.close()


def test_llm_is_never_called_for_screen_review_message(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    _, sid = insert_screening(conn, external_id="1")
    ref = screen_review.format_review_ref(sid)
    insert_review_message(conn, "msg_1", body_raw=f"{ref} - LIKE\ngood")
    conn.close()

    calls = []

    def fail_if_called(client, model, subject, body):
        calls.append(1)
        raise AssertionError("must never call the LLM for a screen-review message")

    monkeypatch.setattr(parser, "build_client", lambda api_key: object())
    monkeypatch.setattr(parser, "parse_message", fail_if_called)

    cli.cmd_parse_mail(config)
    assert calls == []


def test_malformed_screen_review_becomes_needs_review_with_zero_llm_calls(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_screening(conn, external_id="1")
    insert_review_message(conn, "msg_1", body_raw="ABC - LIKE\nno ref given")
    conn.close()

    calls = []

    def fail_if_called(client, model, subject, body):
        calls.append(1)
        raise AssertionError("must never call the LLM")

    monkeypatch.setattr(parser, "build_client", lambda api_key: object())
    monkeypatch.setattr(parser, "parse_message", fail_if_called)

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0
    assert calls == []

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "NEEDS_REVIEW"
    assert db.get_feedback_events_for_message(conn, "msg_1") == []
    row = db.get_message(conn, "msg_1")
    assert "SCREEN_REVIEW_PARSE_FAILED" in row["error"]
    conn.close()


# --- reference resolution ---------------------------------------------------------


def test_review_ref_resolves_to_exact_screening_not_ticker(tmp_path):
    """Two DIFFERENT screenings share the same ticker (re-screened after a
    Taste update) -- the review ref must resolve to the EXACT one Brad
    references, never guessed from the ticker.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    _, sid_old = insert_screening(conn, external_id="1", ticker="ABC", overall_prediction="PASS", taste_version=1)
    _, sid_new = insert_screening(conn, external_id="1", ticker="ABC", overall_prediction="WATCH", taste_version=2)
    ref_old = screen_review.format_review_ref(sid_old)
    insert_review_message(conn, "msg_1", body_raw=f"{ref_old} - LIKE\ndisagree with the old PASS call")
    conn.close()

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    events = db.get_feedback_events_for_message(conn, "msg_1")
    assert len(events) == 1
    assert events[0]["screening_id"] == sid_old
    conn.close()


def test_unknown_review_ref_fails_closed(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_review_message(conn, "msg_1", body_raw="SR-999999 - LIKE\nunknown\n")
    conn.close()

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "NEEDS_REVIEW"
    assert db.get_feedback_events_for_message(conn, "msg_1") == []
    conn.close()


# --- single / multi block parsing -------------------------------------------------


def test_single_block_creates_screen_review_feedback(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    source_id, sid = insert_screening(conn, external_id="1", ticker="ABC", company="Abc Corp")
    ref = screen_review.format_review_ref(sid)
    insert_review_message(
        conn, "msg_1",
        body_raw=f"{ref} — PASS\nCorrect rejection. I don't see a reason for the mispricing.",
    )
    conn.close()

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "PARSED"
    events = db.get_feedback_events_for_message(conn, "msg_1")
    assert len(events) == 1
    event = events[0]
    assert event["verdict"] == "PASS"
    assert event["user_comment"] == "Correct rejection. I don't see a reason for the mispricing."
    assert event["feedback_origin"] == "SCREEN_REVIEW"
    assert event["holdout_eligible"] == 0
    assert event["excluded_from_learning"] == 0
    assert event["source_id"] == source_id
    assert event["screening_id"] == sid
    assert event["ticker"] == "ABC"
    conn.close()


def test_multi_block_creates_feedback_only_for_reviewed_screenings(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    sids = [insert_screening(conn, external_id=str(i), ticker=f"T{i}")[1] for i in range(20)]
    ref_a = screen_review.format_review_ref(sids[0])
    ref_g = screen_review.format_review_ref(sids[6])
    insert_review_message(
        conn, "msg_1",
        body_raw=f"{ref_a} — LIKE\nGood moat.\n\n{ref_g} — PASS\nToo levered.\n",
    )
    conn.close()

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    events = db.get_feedback_events_for_message(conn, "msg_1")
    assert len(events) == 2
    by_screening = {e["screening_id"]: e for e in events}
    assert by_screening[sids[0]]["verdict"] == "LIKE"
    assert by_screening[sids[0]]["user_comment"] == "Good moat."
    assert by_screening[sids[6]]["verdict"] == "PASS"
    assert by_screening[sids[6]]["user_comment"] == "Too levered."
    # Only 2 of the 20 reviewed -- no feedback for the other 18.
    assert sids[1] not in by_screening
    conn.close()


def test_parse_mail_rerun_is_idempotent_for_screen_review(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    _, sid = insert_screening(conn, external_id="1")
    ref = screen_review.format_review_ref(sid)
    insert_review_message(conn, "msg_1", body_raw=f"{ref} - LIKE\ngood")
    conn.close()

    first = cli.cmd_parse_mail(config)
    second = cli.cmd_parse_mail(config)
    assert first == 0
    assert second == 0

    conn = db.connect(config.database_path)
    count = conn.execute("SELECT COUNT(*) FROM feedback WHERE message_id = 'msg_1'").fetchone()[0]
    assert count == 1
    conn.close()


# --- duplicate-screening safety ---------------------------------------------------


def test_second_independent_review_of_same_screening_fails_closed(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    _, sid = insert_screening(conn, external_id="1")
    ref = screen_review.format_review_ref(sid)
    insert_review_message(conn, "msg_1", body_raw=f"{ref} - LIKE\nfirst opinion")
    conn.close()
    cli.cmd_parse_mail(config)

    conn = db.connect(config.database_path)
    insert_review_message(conn, "msg_2", body_raw=f"{ref} - PASS\nsecond, independent opinion")
    conn.close()
    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "PARSED"
    assert get_status(conn, "msg_2") == "NEEDS_REVIEW"
    row = db.get_message(conn, "msg_2")
    assert "DUPLICATE_SCREEN_REVIEW" in row["error"]
    count = conn.execute("SELECT COUNT(*) FROM feedback WHERE screening_id = ?", (sid,)).fetchone()[0]
    assert count == 1  # msg_2's attempt never created a second row
    conn.close()


# --- security / auth --------------------------------------------------------------


def test_non_brad_sender_is_never_processed_as_screen_review(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    _, sid = insert_screening(conn, external_id="1")
    ref = screen_review.format_review_ref(sid)
    insert_review_message(conn, "msg_1", sender="newsletter@example.com", body_raw=f"{ref} - LIKE\ngood")
    conn.close()

    calls = []
    monkeypatch.setattr(parser, "build_client", lambda api_key: object())
    monkeypatch.setattr(parser, "parse_message", lambda *a, **k: calls.append(1))

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0
    assert calls == []

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "SKIPPED_NOT_BRAD"
    assert db.get_feedback_events_for_message(conn, "msg_1") == []
    conn.close()


def test_unauthenticated_sender_is_never_processed_as_screen_review(tmp_path):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    _, sid = insert_screening(conn, external_id="1")
    ref = screen_review.format_review_ref(sid)
    insert_review_message(
        conn, "msg_1", sender_authenticated="UNAUTHENTICATED", body_raw=f"{ref} - LIKE\ngood"
    )
    conn.close()

    exit_code = cli.cmd_parse_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    assert get_status(conn, "msg_1") == "SKIPPED_UNAUTHENTICATED"
    assert db.get_feedback_events_for_message(conn, "msg_1") == []
    conn.close()


# --- holdout interaction ----------------------------------------------------------


def test_screen_review_feedback_never_increases_holdout_count(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()
    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    capsys.readouterr()
    cli.cmd_taste_status(config)
    before = capsys.readouterr().out
    assert "Holdout judgments collected: 0 / 20" in before
    assert "Taste frozen: YES" in before

    conn = db.connect(config.database_path)
    for i in range(5):
        _, sid = insert_screening(conn, external_id=f"sr{i}", ticker=f"SR{i}")
        ref = screen_review.format_review_ref(sid)
        insert_review_message(
            conn, f"msg_review_{i}", body_raw=f"{ref} - LIKE\nreview {i}"
        )
    conn.close()
    cli.cmd_parse_mail(config)

    capsys.readouterr()
    cli.cmd_taste_status(config)
    after = capsys.readouterr().out
    assert "Holdout judgments collected: 0 / 20" in after
    assert "Taste frozen: YES" in after
    assert "Training judgments: 15" in after
    assert "Screen-review feedback records: 5" in after

    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    learning_eligible = db.get_feedback_eligible_for_learning(conn)
    screen_review_ids = [r["feedback_id"] for r in learning_eligible if r["feedback_origin"] == "SCREEN_REVIEW"]
    holdout = db.get_eligible_feedback_after(conn, latest["checkpoint_feedback_id"])
    conn.close()

    assert len(screen_review_ids) == 5  # learning-eligible
    assert len(holdout) == 0  # never counted toward clean holdout


def test_direct_feedback_after_screen_review_still_increments_holdout(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()
    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    _, sid = insert_screening(conn, external_id="1")
    ref = screen_review.format_review_ref(sid)
    insert_review_message(conn, "msg_review", body_raw=f"{ref} - LIKE\ngood")
    conn.close()
    cli.cmd_parse_mail(config)

    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 1, prefix="msg_direct_holdout")
    holdout = db.get_eligible_feedback_after(conn, latest["checkpoint_feedback_id"])
    conn.close()

    assert len(holdout) == 1  # only the genuine DIRECT judgment counts


def test_shadow_score_ignores_screen_review_feedback(tmp_path, monkeypatch):
    from ideascout import source_isolation

    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()
    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    conn = db.connect(config.database_path)
    _, sid = insert_screening(conn, external_id="1")
    ref = screen_review.format_review_ref(sid)
    insert_review_message(conn, "msg_review", body_raw=f"{ref} - LIKE\ngood")
    conn.close()
    cli.cmd_parse_mail(config)

    calls = []
    from ideascout import shadow

    monkeypatch.setattr(shadow, "build_client", lambda api_key: object())

    def fail_if_called(*args, **kwargs):
        calls.append(1)
        raise AssertionError("shadow-score must never treat SCREEN_REVIEW feedback as a holdout case")

    monkeypatch.setattr(source_isolation, "isolate_source", fail_if_called)

    exit_code = cli.cmd_shadow_score(config)
    assert exit_code == 0
    assert calls == []


def test_build_taste_after_holdout_completion_includes_screen_review_feedback(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()
    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    conn = db.connect(config.database_path)
    _, sid = insert_screening(conn, external_id="1")
    ref = screen_review.format_review_ref(sid)
    insert_review_message(conn, "msg_review", body_raw=f"{ref} - LIKE\ngood")
    conn.close()
    cli.cmd_parse_mail(config)

    conn = db.connect(config.database_path)
    insert_n_eligible(conn, taste.HOLDOUT_SIZE, prefix="msg_holdout")
    # Holdout complete is NOT the same as build-unlocked (see db.py's
    # Migration 14 comment) -- record a RETRAIN decision so this test can exercise
    # what training looks like once Brad has authorized regeneration.
    from tests.test_cli_build_taste import unlock_latest_taste_version

    unlock_latest_taste_version(conn)
    conn.close()

    exit_code = cli.cmd_build_taste(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    training_ids = __import__("json").loads(latest["training_feedback_ids_json"])
    screen_review_rows = conn.execute(
        "SELECT feedback_id FROM feedback WHERE feedback_origin = 'SCREEN_REVIEW'"
    ).fetchall()
    conn.close()

    assert len(screen_review_rows) == 1
    assert screen_review_rows[0]["feedback_id"] in training_ids


# --- show-feedback / show-source-screenings display -------------------------------


def test_show_feedback_displays_screen_review_origin_and_provenance(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    _, sid = insert_screening(
        conn, external_id="1", ticker="ABC", company="Abc Corp", overall_prediction="PASS", taste_version=2
    )
    ref = screen_review.format_review_ref(sid)
    insert_review_message(conn, "msg_1", body_raw=f"{ref} - LIKE\nGood margins.")
    conn.close()

    cli.cmd_parse_mail(config)
    capsys.readouterr()

    exit_code = cli.cmd_show_feedback(config)
    assert exit_code == 0

    output = capsys.readouterr().out
    assert "Origin: SCREEN_REVIEW" in output
    assert "Holdout eligible: NO" in output
    assert "yellowbrick/ABC" in output
    assert "Reviewed screen: Taste v2 / PASS" in output
    assert f"Review ref: {ref}" in output


def test_show_feedback_direct_feedback_unaffected_by_screen_review_display(tmp_path, capsys):
    from tests.test_cli_parse_mail import insert_raw_message as insert_direct_raw_message

    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_direct_raw_message(conn, "msg_direct_1")
    conn.close()

    conn = db.connect(config.database_path)
    db.insert_feedback(
        conn, message_id="msg_direct_1", event_type="FEEDBACK", verdict="LIKE", ticker="XYZ",
        company=None, novelty="UNKNOWN", user_comment="Looks interesting.", parsed_json="{}",
        parser_version=parser.PARSER_VERSION, model_name="claude-haiku-4-5", confidence=0.9,
        created_at="2026-01-01T00:00:00+00:00",
    )
    conn.close()

    exit_code = cli.cmd_show_feedback(config)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Origin: SCREEN_REVIEW" not in output
    assert "Reviewed screen:" not in output
