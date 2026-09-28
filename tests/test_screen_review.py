"""Unit tests for ideascout/screen_review.py: the deterministic, no-LLM
parser for "IdeaScout Screen Review" emails (Stage 10).

Unlike digest_reply.py's parser, review-reference resolution requires a
real database (a review ref can point at ANY past screening, not just a
small pre-fetched list) -- every test here uses a throwaway tmp_path
SQLite database via db.connect, never production state. No network, no
LLM, no AgentMail.
"""

from ideascout import db, screen_review

DISTINCTIVE_PHRASE = "ZZZSCREENONLYPHRASE12345"


def make_screening(
    conn,
    *,
    external_id: str,
    ticker: str | None = "ABC",
    company: str | None = "Abc Corp",
    overall_prediction: str = "WATCH",
    taste_version: int = 2,
) -> tuple[int, int]:
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
        content_hash=f"hash-{external_id}",
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
        content_hash=f"hash-{external_id}",
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


def insert_prior_screen_review_feedback(conn, screening_id: int, message_id: str = "msg_prior") -> None:
    db.insert_message(
        conn,
        message_id=message_id,
        inbox_id="inbox_abc",
        thread_id="thread_prior",
        received_at="2026-01-01T00:00:00+00:00",
        sender="brad@example.com",
        recipients="ideas@yourdomain.agentmail.to",
        subject="IdeaScout Screen Review",
        body_raw="already reviewed",
        body_format="text",
        size_bytes=10,
        stored_at="2026-01-01T00:00:05+00:00",
        sender_authenticated="AUTHENTICATED",
    )
    db.insert_feedback(
        conn,
        message_id=message_id,
        event_type="FEEDBACK",
        verdict="PASS",
        ticker=None,
        company=None,
        novelty=None,
        user_comment="already reviewed",
        parsed_json="{}",
        parser_version=screen_review.PARSER_VERSION,
        model_name=screen_review.MODEL_NAME,
        confidence=None,
        created_at="2026-01-01T00:00:00+00:00",
        feedback_origin="SCREEN_REVIEW",
        holdout_eligible=False,
        screening_id=screening_id,
    )


# --- review reference format / resolution -------------------------------------


def test_format_review_ref_uses_sr_prefix():
    assert screen_review.format_review_ref(12345) == "SR-12345"


def test_parse_review_ref_round_trips():
    assert screen_review.parse_review_ref("SR-12345") == 12345
    assert screen_review.parse_review_ref("sr-12345") == 12345
    assert screen_review.parse_review_ref(" SR-12345 ") == 12345


def test_parse_review_ref_rejects_malformed_text():
    assert screen_review.parse_review_ref("SR-") is None
    assert screen_review.parse_review_ref("SRX-123") is None
    assert screen_review.parse_review_ref("12345") is None
    assert screen_review.parse_review_ref("SR 12345") is None


def test_review_ref_uniquely_resolves_exact_screening(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    _, sid_a = make_screening(conn, external_id="1", ticker="AAA")
    _, sid_b = make_screening(conn, external_id="2", ticker="BBB")

    row_a = db.get_source_screening_by_id(conn, sid_a)
    row_b = db.get_source_screening_by_id(conn, sid_b)
    conn.close()

    assert row_a["screening_id"] == sid_a
    assert row_a["ticker"] == "AAA"
    assert row_b["screening_id"] == sid_b
    assert row_b["ticker"] == "BBB"


def test_unknown_screening_id_resolves_to_none(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    assert db.get_source_screening_by_id(conn, 999999) is None
    conn.close()


# --- single/multi block parsing ------------------------------------------------


def test_single_block_parses_with_verbatim_comment(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    _, sid = make_screening(conn, external_id="1")
    ref = screen_review.format_review_ref(sid)

    body = f"{ref} — PASS\nCorrect rejection. I don't see a real reason for the mispricing.\n"
    result = screen_review.parse_screen_review(conn, body)
    conn.close()

    assert result.ok
    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.verdict == "PASS"
    assert block.comment == "Correct rejection. I don't see a real reason for the mispricing."
    assert block.screening_row["screening_id"] == sid


def test_multi_block_parses_subset_of_recent_screenings(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    sids = [make_screening(conn, external_id=str(i), ticker=f"T{i}")[1] for i in range(5)]
    ref_a = screen_review.format_review_ref(sids[0])
    ref_c = screen_review.format_review_ref(sids[2])

    body = f"{ref_a} — LIKE\nGood moat.\n\n{ref_c} — PASS\nToo levered.\n"
    result = screen_review.parse_screen_review(conn, body)
    conn.close()

    assert result.ok
    assert len(result.blocks) == 2
    assert result.blocks[0].screening_row["screening_id"] == sids[0]
    assert result.blocks[0].verdict == "LIKE"
    assert result.blocks[0].comment == "Good moat."
    assert result.blocks[1].screening_row["screening_id"] == sids[2]
    assert result.blocks[1].verdict == "PASS"
    assert result.blocks[1].comment == "Too levered."


def test_multiline_reasoning_preserved_verbatim(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    _, sid = make_screening(conn, external_id="1")
    ref = screen_review.format_review_ref(sid)

    body = f"{ref} — MAYBE\nLine one.\nLine two.\n\nLine four after a blank line.\n"
    result = screen_review.parse_screen_review(conn, body)
    conn.close()

    assert result.ok
    assert result.blocks[0].comment == "Line one.\nLine two.\n\nLine four after a blank line."


def test_verdict_capitalization_variants(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    _, sid = make_screening(conn, external_id="1")
    ref = screen_review.format_review_ref(sid)
    conn.close()

    variants = {
        "strong like": "STRONG_LIKE",
        "Strong Pass!": "STRONG_PASS",
        "MAYBE:": "MAYBE",
        "pass": "PASS",
        "LiKe": "LIKE",
    }
    for i, (text, expected) in enumerate(variants.items()):
        conn = db.connect(tmp_path / f"ideas_verdict_{i}.db")
        _, sid = make_screening(conn, external_id="v")
        ref = screen_review.format_review_ref(sid)
        result = screen_review.parse_screen_review(conn, f"{ref} - {text}\ncomment")
        conn.close()
        assert result.ok, f"expected {text!r} to parse"
        assert result.blocks[0].verdict == expected


def test_accepted_header_separators(tmp_path):
    for i, sep in enumerate(["-", "--", "—", ":"]):
        conn = db.connect(tmp_path / f"ideas_sep_{i}.db")
        _, sid = make_screening(conn, external_id="1")
        ref = screen_review.format_review_ref(sid)
        result = screen_review.parse_screen_review(conn, f"{ref} {sep} LIKE\ncomment")
        conn.close()
        assert result.ok, f"separator {sep!r} should be accepted"


def test_only_explicitly_included_ideas_generate_blocks(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    sids = [make_screening(conn, external_id=str(i), ticker=f"T{i}")[1] for i in range(20)]
    ref = screen_review.format_review_ref(sids[7])

    body = f"{ref} — LIKE\nonly this one\n"
    result = screen_review.parse_screen_review(conn, body)
    conn.close()

    assert result.ok
    assert len(result.blocks) == 1
    assert result.blocks[0].screening_row["screening_id"] == sids[7]


# --- fail-closed cases ----------------------------------------------------------


def test_unknown_review_ref_fails_whole_message_closed(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    _, sid = make_screening(conn, external_id="1")
    ref = screen_review.format_review_ref(sid)

    body = f"{ref} — LIKE\ngood\n\nSR-999999 — PASS\nunknown\n"
    result = screen_review.parse_screen_review(conn, body)
    conn.close()

    assert not result.ok
    assert result.blocks == []
    assert "SR-999999" in result.reason


def test_ticker_only_feedback_is_rejected(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    make_screening(conn, external_id="1", ticker="ABC")
    result = screen_review.parse_screen_review(conn, "ABC - LIKE\nno ref given\n")
    conn.close()

    assert not result.ok


def test_company_only_feedback_is_rejected(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    make_screening(conn, external_id="1", company="Abc Corp")
    result = screen_review.parse_screen_review(conn, "Abc Corp - LIKE\nno ref given\n")
    conn.close()

    assert not result.ok


def test_bare_verdict_with_no_ref_is_rejected(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    make_screening(conn, external_id="1")
    result = screen_review.parse_screen_review(conn, "LIKE\nno ref at all\n")
    conn.close()

    assert not result.ok


def test_invalid_verdict_fails_closed(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    _, sid = make_screening(conn, external_id="1")
    ref = screen_review.format_review_ref(sid)
    result = screen_review.parse_screen_review(conn, f"{ref} — KINDA LIKE\nweird\n")
    conn.close()

    assert not result.ok


def test_malformed_header_missing_separator_fails_closed(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    _, sid = make_screening(conn, external_id="1")
    ref = screen_review.format_review_ref(sid)
    result = screen_review.parse_screen_review(conn, f"{ref} LIKE\nno separator\n")
    conn.close()

    assert not result.ok


def test_no_partial_inserts_when_one_block_is_malformed(tmp_path):
    """A valid first block plus a malformed second block must fail the
    ENTIRE message -- the valid block's judgment is never silently kept.
    """
    conn = db.connect(tmp_path / "ideas.db")
    _, sid = make_screening(conn, external_id="1", ticker="AAA")
    ref = screen_review.format_review_ref(sid)
    body = f"{ref} — LIKE\ngood\n\nBBB - PASS\nno ref for this one\n"
    result = screen_review.parse_screen_review(conn, body)
    conn.close()

    assert not result.ok
    assert result.blocks == []


def test_duplicate_ref_within_same_message_fails_closed(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    _, sid = make_screening(conn, external_id="1")
    ref = screen_review.format_review_ref(sid)
    body = f"{ref} — LIKE\nfirst\n\n{ref} — PASS\nsecond, contradicts first\n"
    result = screen_review.parse_screen_review(conn, body)
    conn.close()

    assert not result.ok


def test_no_recognized_header_at_all_fails_closed(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    make_screening(conn, external_id="1")
    result = screen_review.parse_screen_review(conn, "Just some unrelated text with no header at all.\n")
    conn.close()

    assert not result.ok


# --- duplicate-screening (cross-message) safety --------------------------------


def test_second_independent_review_of_same_screening_fails_closed(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    _, sid = make_screening(conn, external_id="1")
    insert_prior_screen_review_feedback(conn, sid)

    ref = screen_review.format_review_ref(sid)
    result = screen_review.parse_screen_review(conn, f"{ref} — LIKE\na second, independent opinion\n")
    conn.close()

    assert not result.ok
    assert "DUPLICATE_SCREEN_REVIEW" in result.reason


def test_excluded_prior_review_allows_a_fresh_review(tmp_path):
    """A prior SCREEN_REVIEW row that Brad has since excluded from
    learning (excluded_from_learning=1) does not count as "already
    reviewed" -- an explicit exclusion means "don't use this one," not
    "no review was ever given."
    """
    conn = db.connect(tmp_path / "ideas.db")
    _, sid = make_screening(conn, external_id="1")
    insert_prior_screen_review_feedback(conn, sid)
    prior_feedback_id = conn.execute(
        "SELECT feedback_id FROM feedback WHERE screening_id = ?", (sid,)
    ).fetchone()["feedback_id"]
    db.set_feedback_excluded_from_learning(conn, prior_feedback_id, True)

    ref = screen_review.format_review_ref(sid)
    result = screen_review.parse_screen_review(conn, f"{ref} — LIKE\na fresh opinion\n")
    conn.close()

    assert result.ok


# --- quote/signature isolation reuse (shared with digest_reply.py) -------------


def test_quoted_screen_content_never_leaks_into_comment(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    _, sid = make_screening(conn, external_id="1")
    ref = screen_review.format_review_ref(sid)

    body = (
        f"{ref} — MAYBE\n\nNeed more detail on the pipeline.\n\n"
        "On Thu, Jan 1, 2026 at 9:00 AM IdeaScout <ideas@example.com> wrote:\n"
        f"> {DISTINCTIVE_PHRASE} model reasoning here\n"
    )
    result = screen_review.parse_screen_review(conn, body)
    conn.close()

    assert result.ok
    assert DISTINCTIVE_PHRASE not in result.blocks[0].comment


def test_signature_stripped_from_screen_review_email(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    _, sid = make_screening(conn, external_id="1")
    ref = screen_review.format_review_ref(sid)

    body = f"{ref} — LIKE\nGood pick.\n\n--\nBrad\nFar View Capital Management\n"
    result = screen_review.parse_screen_review(conn, body)
    conn.close()

    assert result.ok
    assert "Far View" not in result.blocks[0].comment


def test_ambiguous_isolation_fails_closed(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    make_screening(conn, external_id="1")
    result = screen_review.parse_screen_review(conn, "   \n\n  ")
    conn.close()

    assert not result.ok


# --- subject heuristic (only routing signal for this feature) -----------------


def test_subject_exact_match():
    assert screen_review.looks_like_screen_review_subject("IdeaScout Screen Review")


def test_subject_accepts_re_and_fwd_prefixes():
    assert screen_review.looks_like_screen_review_subject("Re: IdeaScout Screen Review")
    assert screen_review.looks_like_screen_review_subject("Fwd: IdeaScout Screen Review")
    assert screen_review.looks_like_screen_review_subject("FW: IdeaScout Screen Review")
    assert screen_review.looks_like_screen_review_subject("Re: Fwd: IdeaScout Screen Review")


def test_subject_case_insensitive():
    assert screen_review.looks_like_screen_review_subject("ideascout screen review")
    assert screen_review.looks_like_screen_review_subject("IDEASCOUT SCREEN REVIEW")


def test_subject_does_not_match_unrelated_or_digest_subjects():
    assert not screen_review.looks_like_screen_review_subject("Re: Dinner plans")
    assert not screen_review.looks_like_screen_review_subject(
        "Re: IdeaScout — New Ideas Worth Attention (2026-01-01)"
    )
    assert not screen_review.looks_like_screen_review_subject(None)
    assert not screen_review.looks_like_screen_review_subject("")
