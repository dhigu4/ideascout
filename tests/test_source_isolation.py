"""Unit tests for ideascout/source_isolation.py -- Level 0 of Stage 4.

Pure functions, no DB, no LLM, no network. These are the tests that prove
the no-leakage guarantee at its most fundamental level: whatever Brad
wrote is never present in the isolated source_text.
"""

from __future__ import annotations

from ideascout import source_isolation


BRADS_COMMENT = "STRONG LIKE. I think this is a fantastic mispriced setup, way better than peers."


def test_gmail_forward_marker_isolates_source_and_drops_brads_comment():
    body = (
        f"{BRADS_COMMENT}\n\n"
        "---------- Forwarded message ---------\n"
        "From: analyst@example.com\n"
        "Subject: XYZ Corp writeup\n\n"
        "XYZ Corp trades at 5x normalized earnings due to a temporary loss-making segment.\n"
    )
    result = source_isolation.isolate_source(body)

    assert result.scorable is True
    assert result.source_type == "forwarded_email"
    assert BRADS_COMMENT not in result.source_text
    assert "XYZ Corp trades at 5x normalized earnings" in result.source_text


def test_outlook_original_message_marker_isolates_source():
    body = (
        f"{BRADS_COMMENT}\n\n"
        "-----Original Message-----\n"
        "From: analyst@example.com\n"
        "Subject: ABC Inc writeup\n\n"
        "ABC Inc has a hidden earnings power story driven by a segment divestiture.\n"
    )
    result = source_isolation.isolate_source(body)

    assert result.scorable is True
    assert BRADS_COMMENT not in result.source_text
    assert "hidden earnings power" in result.source_text


def test_outlook_header_block_without_explicit_marker_isolates_source():
    body = (
        f"{BRADS_COMMENT}\n\n"
        "From: analyst@example.com\n"
        "Sent: Monday, January 5, 2026 9:00 AM\n"
        "To: brad@example.com\n"
        "Subject: DEF Ltd writeup\n\n"
        "DEF Ltd has demonstrated a path to 3-4x upside based on simple math.\n"
    )
    result = source_isolation.isolate_source(body)

    assert result.scorable is True
    assert BRADS_COMMENT not in result.source_text
    assert "3-4x upside" in result.source_text


def test_explicit_source_marker_is_highest_priority():
    body = f"{BRADS_COMMENT}\n\nSOURCE:\n\nGHI Co is a spin-off with obscured segment economics.\n"
    result = source_isolation.isolate_source(body)

    assert result.scorable is True
    assert result.source_type == "explicit_source_section"
    assert BRADS_COMMENT not in result.source_text
    assert "GHI Co is a spin-off" in result.source_text


def test_trailing_quoted_block_isolates_source_and_strips_quote_markers():
    body = (
        f"{BRADS_COMMENT}\n\n"
        "> JKL Corp trades below peers due to a misunderstood cyclical trough.\n"
        "> Management has a credible plan to reach 10% margins by 2027.\n"
    )
    result = source_isolation.isolate_source(body)

    assert result.scorable is True
    assert result.source_type == "quoted_original"
    assert BRADS_COMMENT not in result.source_text
    assert ">" not in result.source_text
    assert "JKL Corp trades below peers" in result.source_text
    assert "10% margins by 2027" in result.source_text


def test_no_recognized_boundary_is_unscorable_and_does_not_guess():
    body = f"{BRADS_COMMENT} I also think MNO Corp is interesting for similar reasons."
    result = source_isolation.isolate_source(body)

    assert result.scorable is False
    assert result.source_text is None
    assert "no recognized" in result.unscorable_reason.lower()


def test_empty_body_is_unscorable():
    result = source_isolation.isolate_source("")
    assert result.scorable is False
    assert "empty" in result.unscorable_reason.lower()

    result_none = source_isolation.isolate_source(None)
    assert result_none.scorable is False


def test_multiple_forward_markers_is_ambiguous_and_unscorable():
    body = (
        "---------- Forwarded message ---------\n"
        "From: a@example.com\nSubject: First forward\n\n"
        "---------- Forwarded message ---------\n"
        "From: b@example.com\nSubject: Second forward (nested)\n\n"
        "Some content.\n"
    )
    result = source_isolation.isolate_source(body)

    assert result.scorable is False
    assert "ambiguous" in result.unscorable_reason.lower()


def test_interleaved_quoted_and_nonquoted_lines_is_ambiguous_and_unscorable():
    body = (
        f"{BRADS_COMMENT}\n\n"
        "> Quoted line one.\n"
        "This is Brad's own line stuck in the middle, not a quote.\n"
        "> Quoted line two.\n"
    )
    result = source_isolation.isolate_source(body)

    assert result.scorable is False
    assert "ambiguous" in result.unscorable_reason.lower()


def test_marker_found_but_no_substantive_content_is_unscorable():
    body = f"{BRADS_COMMENT}\n\n---------- Forwarded message ---------\n"
    result = source_isolation.isolate_source(body)

    assert result.scorable is False
    assert "no substantive content" in result.unscorable_reason.lower()


def test_ambiguous_strong_marker_is_not_overridden_by_a_weaker_heuristic():
    """Two Gmail forward markers (ambiguous) should not fall through to
    the quoted-line heuristic even if quoted lines also happen to be
    present somewhere in the body -- an ambiguous strong signal must never
    be silently resolved by a weaker one.
    """
    body = (
        "---------- Forwarded message ---------\n"
        "From: a@example.com\nSubject: First\n\n"
        "> some quoted text that could otherwise look isolable\n"
        "---------- Forwarded message ---------\n"
        "From: b@example.com\nSubject: Second\n\n"
        "more quoted text\n"
    )
    result = source_isolation.isolate_source(body)

    assert result.scorable is False
    assert "forwarded_email" in result.unscorable_reason
    assert "ambiguous" in result.unscorable_reason.lower()
