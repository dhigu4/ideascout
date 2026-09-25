"""Unit tests for ideascout/digest_reply.py: the deterministic, no-LLM
reply-text isolator and verdict/header parser (Stage 9).

Pure module-level tests -- no database, no filesystem, no network, no
LLM. Every "distinctive phrase" test exists specifically to prove
IdeaScout's own quoted digest content (screen result/reasons/concerns)
can never leak into what gets treated as Brad's own words.
"""

from ideascout.digest_reply import (
    DeliveryItem,
    isolate_reply_text,
    looks_like_digest_reply_subject,
    parse_digest_reply,
)

DISTINCTIVE_PHRASE = "ZZZQUOTEDDIGESTONLYPHRASE12345"


# --- reply isolation ---------------------------------------------------------


def test_isolation_strips_gmail_on_wrote_quote_block():
    body = (
        "LIKE\n\nGood margins, worth tracking.\n\n"
        "On Tue, Sep 23, 2026 at 9:00 AM Brad <brad@farviewcapitalmgmt.com> wrote:\n"
        f"> {DISTINCTIVE_PHRASE} screen result: PASS\n> reasons: xyz\n"
    )
    result = isolate_reply_text(body)
    assert result.ok
    assert DISTINCTIVE_PHRASE not in result.reply_text
    assert "LIKE" in result.reply_text


def test_isolation_strips_outlook_header_block():
    body = (
        "PASS - not for us right now.\n\n"
        "From: IdeaScout <ideas@example.com>\n"
        "Sent: Tuesday, September 23, 2026 9:00 AM\n"
        "To: Brad\n"
        "Subject: IdeaScout Digest\n\n"
        f"{DISTINCTIVE_PHRASE} screen result: LIKE\n"
    )
    result = isolate_reply_text(body)
    assert result.ok
    assert DISTINCTIVE_PHRASE not in result.reply_text
    assert "PASS" in result.reply_text


def test_isolation_strips_outlook_original_message_marker():
    body = f"MAYBE\n\n----- Original Message -----\n{DISTINCTIVE_PHRASE} some digest content\n"
    result = isolate_reply_text(body)
    assert result.ok
    assert DISTINCTIVE_PHRASE not in result.reply_text


def test_isolation_strips_trailing_quote_block():
    body = f"MAYBE, need more data.\n\n> {DISTINCTIVE_PHRASE} original text\n> more text\n"
    result = isolate_reply_text(body)
    assert result.ok
    assert DISTINCTIVE_PHRASE not in result.reply_text
    assert "MAYBE" in result.reply_text


def test_isolation_strips_signature_delimiter():
    body = "LIKE, strong balance sheet.\n\n--\nBrad\nFar View Capital Management\n"
    result = isolate_reply_text(body)
    assert result.ok
    assert "Far View" not in result.reply_text
    assert "LIKE" in result.reply_text


def test_isolation_strips_known_mobile_footer():
    body = "PASS\n\nSent from my iPhone"
    result = isolate_reply_text(body)
    assert result.ok
    assert "iPhone" not in result.reply_text


def test_isolation_fails_closed_on_interleaved_quoting():
    body = "LIKE this one.\n> quoted line\nnot quoted again\n> quoted line 2\n"
    result = isolate_reply_text(body)
    assert not result.ok


def test_isolation_fails_closed_on_empty_body():
    result = isolate_reply_text("   \n\n  ")
    assert not result.ok


def test_isolation_fails_closed_on_none_body():
    result = isolate_reply_text(None)
    assert not result.ok


def test_isolation_fails_closed_when_only_quoted_content_remains():
    body = f"> {DISTINCTIVE_PHRASE} entirely quoted\n"
    result = isolate_reply_text(body)
    assert not result.ok


# --- single-item reply parsing -----------------------------------------------


def _single_item():
    return DeliveryItem(source_id=101, ticker="ABC", company="Abc Corp", position=0)


def test_single_item_like_with_multiline_reason():
    body = "LIKE\n\nGreat margins.\nWorth a deeper look next week."
    result = parse_digest_reply(body, [_single_item()])
    assert result.ok
    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.verdict == "LIKE"
    assert block.comment == "Great margins.\nWorth a deeper look next week."
    assert block.item.source_id == 101


def test_single_item_maybe():
    result = parse_digest_reply("MAYBE\nneed more data", [_single_item()])
    assert result.ok
    assert result.blocks[0].verdict == "MAYBE"


def test_single_item_pass():
    result = parse_digest_reply("PASS\nnot for us", [_single_item()])
    assert result.ok
    assert result.blocks[0].verdict == "PASS"


def test_single_item_verdict_case_and_punctuation_insensitive():
    variants = {
        "strong like.": "STRONG_LIKE",
        "Strong Pass!": "STRONG_PASS",
        "maybe:": "MAYBE",
        "PASS": "PASS",
        "LiKe": "LIKE",
    }
    for text, expected in variants.items():
        result = parse_digest_reply(f"{text}\nsome comment", [_single_item()])
        assert result.ok, f"expected {text!r} to parse"
        assert result.blocks[0].verdict == expected, f"{text!r} -> {result.blocks[0].verdict}"


def test_single_item_labeled_header_also_accepted():
    result = parse_digest_reply("ABC — LIKE\ncomment here", [_single_item()])
    assert result.ok
    assert result.blocks[0].verdict == "LIKE"


def test_single_item_no_comment_is_valid_verbatim_empty():
    result = parse_digest_reply("PASS", [_single_item()])
    assert result.ok
    assert result.blocks[0].comment == ""


def test_single_item_ambiguous_reply_fails_closed():
    result = parse_digest_reply("Hmm not sure yet, will look later.", [_single_item()])
    assert not result.ok
    assert result.reason


def test_single_item_source_id_and_holdout_fields_come_from_delivery_item():
    result = parse_digest_reply("LIKE\ngood", [_single_item()])
    assert result.ok
    assert result.blocks[0].item.source_id == 101
    assert result.blocks[0].item.ticker == "ABC"


# --- multi-item reply parsing -------------------------------------------------


def _five_items():
    return [
        DeliveryItem(source_id=1, ticker="AAA", company="Aaa Inc", position=0),
        DeliveryItem(source_id=2, ticker="BBB", company="Bbb Inc", position=1),
        DeliveryItem(source_id=3, ticker="CCC", company="Ccc Inc", position=2),
        DeliveryItem(source_id=4, ticker="DDD", company="Ddd Inc", position=3),
        DeliveryItem(source_id=5, ticker="EEE", company="Eee Inc", position=4),
    ]


def test_multi_item_feedback_on_two_of_five_ideas():
    body = "AAA - LIKE\nGood moat, revisit next quarter.\n\nCCC: PASS\nToo levered for our taste.\n"
    result = parse_digest_reply(body, _five_items())
    assert result.ok
    assert len(result.blocks) == 2
    assert result.blocks[0].item.source_id == 1
    assert result.blocks[0].verdict == "LIKE"
    assert result.blocks[0].comment == "Good moat, revisit next quarter."
    assert result.blocks[1].item.source_id == 3
    assert result.blocks[1].verdict == "PASS"
    assert result.blocks[1].comment == "Too levered for our taste."


def test_multi_item_omitted_ideas_generate_no_feedback():
    body = "AAA - LIKE\ngood\n"
    result = parse_digest_reply(body, _five_items())
    assert result.ok
    assert len(result.blocks) == 1
    matched_source_ids = {block.item.source_id for block in result.blocks}
    assert matched_source_ids == {1}


def test_multi_item_company_name_fallback_when_ticker_absent():
    body = "Ccc Inc - LIKE\ngood fit\n"
    result = parse_digest_reply(body, _five_items())
    assert result.ok
    assert result.blocks[0].item.source_id == 3


def test_multi_item_accepts_dash_emdash_and_colon_separators():
    for sep in ["-", "--", "—", ":"]:
        body = f"AAA {sep} LIKE\ngood\n"
        result = parse_digest_reply(body, _five_items())
        assert result.ok, f"separator {sep!r} should be accepted"
        assert result.blocks[0].verdict == "LIKE"


def test_multi_item_ambiguous_ticker_fails_whole_reply_closed():
    ambiguous_items = [
        DeliveryItem(source_id=1, ticker="AAA", company="Aaa Inc", position=0),
        DeliveryItem(source_id=2, ticker="AAA", company="Aaa Two Inc", position=1),
    ]
    result = parse_digest_reply("AAA - LIKE\nsome comment", ambiguous_items)
    assert not result.ok
    assert result.blocks == []


def test_multi_item_unmatched_ticker_fails_whole_reply_closed():
    result = parse_digest_reply("ZZZ - LIKE\nsome comment", _five_items())
    assert not result.ok
    assert result.blocks == []


def test_multi_item_no_recognized_header_fails_closed():
    result = parse_digest_reply("Not sure about any of these this week.", _five_items())
    assert not result.ok


def test_multi_item_partial_ambiguity_fails_the_entire_reply():
    # AAA resolves fine, but ZZZ does not -- the whole reply must fail,
    # not silently drop just the bad block, so Brad's AAA judgment is
    # never silently lost.
    body = "AAA - LIKE\ngood\n\nZZZ - PASS\nbad\n"
    result = parse_digest_reply(body, _five_items())
    assert not result.ok
    assert result.blocks == []


# --- subject heuristic (safety-net only) -------------------------------------


def test_subject_heuristic_matches_expected_digest_reply_subject():
    assert looks_like_digest_reply_subject("Re: IdeaScout — New Ideas Worth Attention (2026-09-23)")
    assert looks_like_digest_reply_subject("RE: Re: IdeaScout - New Ideas Worth Attention")


def test_subject_heuristic_does_not_match_unrelated_subject():
    assert not looks_like_digest_reply_subject("Re: Dinner plans")
    assert not looks_like_digest_reply_subject(None)
    assert not looks_like_digest_reply_subject("")
