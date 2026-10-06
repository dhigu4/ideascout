"""Unit tests for ideascout/blind_review.py: the deterministic, no-LLM
parser for "IdeaScout Blind Review" emails (Stage 11), and the reveal-only
categorical mapping used by blind-review-results.

Like screen_review.py, review-reference resolution requires a real
database (a review ref points at ANY assignment, not a small pre-fetched
list) -- every test here uses a throwaway tmp_path SQLite database via
db.connect, never production state. No network, no LLM, no AgentMail.
"""

from ideascout import blind_review, db

DISTINCTIVE_PHRASE = "ZZZBLINDONLYPHRASE12345"


def make_assignment(
    conn,
    *,
    external_id: str,
    ticker: str | None = "ABC",
    company: str | None = "Abc Corp",
    taste_version_at_assignment: int | None = 2,
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
    db.update_collected_source_extracted(
        conn,
        source_id=source_id,
        company=company,
        ticker=ticker,
        source_title=company,
        source_date=None,
        business_summary="x",
        core_thesis="x",
        why_mispriced="x",
        future_earnings_change="x",
        upside_case="x",
        downside_or_key_risks="x",
        catalysts="x",
        what_must_be_true="x",
        evidence_of_market_misunderstanding="x",
        known_unknowns="x",
        extraction_model_name="fake-model",
        extracted_at="2026-01-01T00:00:00+00:00",
    )
    assignment_id = db.record_blind_review_assignment(
        conn,
        source_id=source_id,
        assigned_at="2026-01-01T00:00:00+00:00",
        taste_version_at_assignment=taste_version_at_assignment,
    )
    return source_id, assignment_id


def insert_prior_blind_review_feedback(conn, assignment_id: int, message_id: str = "msg_prior") -> None:
    db.insert_message(
        conn,
        message_id=message_id,
        inbox_id="inbox_abc",
        thread_id="thread_prior",
        received_at="2026-01-01T00:00:00+00:00",
        sender="brad@example.com",
        recipients="ideas@yourdomain.agentmail.to",
        subject="IdeaScout Blind Review",
        body_raw="already judged",
        body_format="text",
        size_bytes=10,
        stored_at="2026-01-01T00:00:05+00:00",
        sender_authenticated="AUTHENTICATED",
    )
    db.insert_feedback_events(
        conn,
        message_id=message_id,
        events=[
            {
                "event_type": "FEEDBACK",
                "verdict": "LIKE",
                "ticker": None,
                "company": None,
                "novelty": None,
                "user_comment": "already judged",
                "parsed_json": "{}",
                "parser_version": blind_review.PARSER_VERSION,
                "model_name": blind_review.MODEL_NAME,
                "confidence": None,
                "created_at": "2026-01-01T00:00:00+00:00",
                "feedback_origin": "BLIND_REVIEW",
                "holdout_eligible": True,
                "blind_review_assignment_id": assignment_id,
            }
        ],
    )


# --- review reference format / resolution -------------------------------------


def test_format_review_ref_uses_br_prefix():
    assert blind_review.format_review_ref(123) == "BR-123"


def test_parse_review_ref_round_trips():
    assert blind_review.parse_review_ref("BR-123") == 123
    assert blind_review.parse_review_ref("br-123") == 123
    assert blind_review.parse_review_ref(" BR-123 ") == 123


def test_parse_review_ref_rejects_malformed_text():
    assert blind_review.parse_review_ref("BR-") is None
    assert blind_review.parse_review_ref("BRX-123") is None
    assert blind_review.parse_review_ref("123") is None


def test_unknown_assignment_id_resolves_to_none(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    assert db.get_blind_review_assignment_by_id(conn, 999999) is None
    conn.close()


# --- single/multi block parsing ------------------------------------------------


def test_single_block_parses_with_verbatim_comment(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    _, aid = make_assignment(conn, external_id="1")
    ref = blind_review.format_review_ref(aid)

    body = f"{ref} — LIKE\nI like the hidden earnings power.\nNeed to understand leverage better.\n"
    result = blind_review.parse_blind_review(conn, body)
    conn.close()

    assert result.ok
    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.verdict == "LIKE"
    assert block.comment == "I like the hidden earnings power.\nNeed to understand leverage better."
    assert block.assignment_row["assignment_id"] == aid


def test_multi_block_parses_subset(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    aids = [make_assignment(conn, external_id=str(i), ticker=f"T{i}")[1] for i in range(5)]
    ref_a = blind_review.format_review_ref(aids[0])
    ref_c = blind_review.format_review_ref(aids[2])

    body = f"{ref_a} — LIKE\nGood setup.\n\n{ref_c} — PASS\nNo real mispricing.\n"
    result = blind_review.parse_blind_review(conn, body)
    conn.close()

    assert result.ok
    assert len(result.blocks) == 2
    assert result.blocks[0].assignment_row["assignment_id"] == aids[0]
    assert result.blocks[0].verdict == "LIKE"
    assert result.blocks[0].comment == "Good setup."
    assert result.blocks[1].assignment_row["assignment_id"] == aids[2]
    assert result.blocks[1].verdict == "PASS"
    assert result.blocks[1].comment == "No real mispricing."


def test_verdict_capitalization_variants(tmp_path):
    variants = {
        "strong like": "STRONG_LIKE",
        "Strong Pass!": "STRONG_PASS",
        "MAYBE:": "MAYBE",
        "pass": "PASS",
        "LiKe": "LIKE",
    }
    for i, (text, expected) in enumerate(variants.items()):
        conn = db.connect(tmp_path / f"ideas_{i}.db")
        _, aid = make_assignment(conn, external_id="v")
        ref = blind_review.format_review_ref(aid)
        result = blind_review.parse_blind_review(conn, f"{ref} - {text}\ncomment")
        conn.close()
        assert result.ok, f"expected {text!r} to parse"
        assert result.blocks[0].verdict == expected


def test_accepted_header_separators(tmp_path):
    for i, sep in enumerate(["-", "--", "—", ":"]):
        conn = db.connect(tmp_path / f"ideas_sep_{i}.db")
        _, aid = make_assignment(conn, external_id="1")
        ref = blind_review.format_review_ref(aid)
        result = blind_review.parse_blind_review(conn, f"{ref} {sep} LIKE\ncomment")
        conn.close()
        assert result.ok, f"separator {sep!r} should be accepted"


# --- fail-closed cases ----------------------------------------------------------


def test_unknown_review_ref_fails_whole_message_closed(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    _, aid = make_assignment(conn, external_id="1")
    ref = blind_review.format_review_ref(aid)

    body = f"{ref} — LIKE\ngood\n\nBR-999999 — PASS\nunknown\n"
    result = blind_review.parse_blind_review(conn, body)
    conn.close()

    assert not result.ok
    assert result.blocks == []
    assert "BR-999999" in result.reason


def test_ticker_only_feedback_is_rejected(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    make_assignment(conn, external_id="1", ticker="ABC")
    result = blind_review.parse_blind_review(conn, "ABC - LIKE\nno ref given\n")
    conn.close()
    assert not result.ok


def test_bare_verdict_with_no_ref_is_rejected(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    make_assignment(conn, external_id="1")
    result = blind_review.parse_blind_review(conn, "LIKE\nno ref at all\n")
    conn.close()
    assert not result.ok


def test_invalid_verdict_fails_closed(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    _, aid = make_assignment(conn, external_id="1")
    ref = blind_review.format_review_ref(aid)
    result = blind_review.parse_blind_review(conn, f"{ref} — KINDA LIKE\nweird\n")
    conn.close()
    assert not result.ok


def test_no_partial_inserts_when_one_block_is_malformed(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    _, aid = make_assignment(conn, external_id="1", ticker="AAA")
    ref = blind_review.format_review_ref(aid)
    body = f"{ref} — LIKE\ngood\n\nBBB - PASS\nno ref for this one\n"
    result = blind_review.parse_blind_review(conn, body)
    conn.close()
    assert not result.ok
    assert result.blocks == []


def test_duplicate_ref_within_same_message_fails_closed(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    _, aid = make_assignment(conn, external_id="1")
    ref = blind_review.format_review_ref(aid)
    body = f"{ref} — LIKE\nfirst\n\n{ref} — PASS\nsecond, contradicts first\n"
    result = blind_review.parse_blind_review(conn, body)
    conn.close()
    assert not result.ok


def test_no_recognized_header_fails_closed(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    make_assignment(conn, external_id="1")
    result = blind_review.parse_blind_review(conn, "Just some unrelated text.\n")
    conn.close()
    assert not result.ok


# --- duplicate-judgment (cross-message) safety ----------------------------------


def test_second_independent_judgment_of_same_assignment_fails_closed(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    _, aid = make_assignment(conn, external_id="1")
    insert_prior_blind_review_feedback(conn, aid)

    ref = blind_review.format_review_ref(aid)
    result = blind_review.parse_blind_review(conn, f"{ref} — PASS\na second, independent opinion\n")
    conn.close()

    assert not result.ok
    assert "DUPLICATE_BLIND_REVIEW" in result.reason


def test_excluded_prior_judgment_still_blocks_a_fresh_review(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    _, aid = make_assignment(conn, external_id="1")
    insert_prior_blind_review_feedback(conn, aid)
    prior_feedback_id = conn.execute(
        "SELECT feedback_id FROM feedback WHERE blind_review_assignment_id = ?", (aid,)
    ).fetchone()["feedback_id"]
    db.set_feedback_excluded_from_learning(conn, prior_feedback_id, True)

    ref = blind_review.format_review_ref(aid)
    result = blind_review.parse_blind_review(conn, f"{ref} — LIKE\na fresh opinion\n")
    conn.close()

    assert not result.ok
    assert "DUPLICATE_BLIND_REVIEW" in result.reason


# --- quote/signature isolation reuse (shared with digest_reply.py) -------------


def test_quoted_content_never_leaks_into_comment(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    _, aid = make_assignment(conn, external_id="1")
    ref = blind_review.format_review_ref(aid)

    body = (
        f"{ref} — MAYBE\n\nNeed more detail.\n\n"
        "On Thu, Jan 1, 2026 at 9:00 AM Brad <brad@farviewcapitalmgmt.com> wrote:\n"
        f"> {DISTINCTIVE_PHRASE} some quoted content\n"
    )
    result = blind_review.parse_blind_review(conn, body)
    conn.close()

    assert result.ok
    assert DISTINCTIVE_PHRASE not in result.blocks[0].comment


def test_signature_stripped(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    _, aid = make_assignment(conn, external_id="1")
    ref = blind_review.format_review_ref(aid)

    body = f"{ref} — LIKE\nGood pick.\n\n--\nBrad\nFar View Capital Management\n"
    result = blind_review.parse_blind_review(conn, body)
    conn.close()

    assert result.ok
    assert "Far View" not in result.blocks[0].comment


# --- categorical mapping (reveal-only) ------------------------------------------


def test_categorize_verdict_buckets():
    assert blind_review.categorize_verdict("STRONG_LIKE") == "positive"
    assert blind_review.categorize_verdict("LIKE") == "positive"
    assert blind_review.categorize_verdict("MAYBE") == "neutral"
    assert blind_review.categorize_verdict("PASS") == "negative"
    assert blind_review.categorize_verdict("STRONG_PASS") == "negative"
    assert blind_review.categorize_verdict(None) is None


def test_categorize_screen_prediction_buckets():
    assert blind_review.categorize_screen_prediction("INVESTIGATE_NOW") == "positive"
    assert blind_review.categorize_screen_prediction("WATCH") == "neutral"
    assert blind_review.categorize_screen_prediction("PASS") == "negative"
    assert blind_review.categorize_screen_prediction(None) is None


def test_categorical_mapping_matches_true_and_false():
    assert blind_review.categorical_mapping_matches("LIKE", "INVESTIGATE_NOW") is True
    assert blind_review.categorical_mapping_matches("PASS", "INVESTIGATE_NOW") is False
    assert blind_review.categorical_mapping_matches("MAYBE", "WATCH") is True


def test_categorical_mapping_matches_none_when_not_comparable():
    assert blind_review.categorical_mapping_matches("LIKE", None) is None
    assert blind_review.categorical_mapping_matches(None, "PASS") is None


# --- subject heuristic (only routing signal) ------------------------------------


def test_subject_exact_match():
    assert blind_review.looks_like_blind_review_subject("IdeaScout Blind Review")


def test_subject_accepts_re_and_fwd_prefixes():
    assert blind_review.looks_like_blind_review_subject("Re: IdeaScout Blind Review")
    assert blind_review.looks_like_blind_review_subject("Fwd: IdeaScout Blind Review")
    assert blind_review.looks_like_blind_review_subject("Re: Fwd: IdeaScout Blind Review")


def test_subject_does_not_match_unrelated_or_other_review_subjects():
    assert not blind_review.looks_like_blind_review_subject("Re: Dinner plans")
    assert not blind_review.looks_like_blind_review_subject("IdeaScout Screen Review")
    assert not blind_review.looks_like_blind_review_subject(
        "Re: IdeaScout — New Ideas Worth Attention (2026-01-01)"
    )
    assert not blind_review.looks_like_blind_review_subject(None)
    assert not blind_review.looks_like_blind_review_subject("")
