"""Unit tests for ideascout/source_isolation.py -- Level 0 of Stage 4.

Pure functions, no DB, no LLM, no network. These are the tests that prove
the no-leakage guarantee at its most fundamental level: whatever Brad
wrote is never present in the isolated source_text.
"""

from __future__ import annotations

import pytest

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


# --- inline-pasted-source fallback (Stage 5.13) ------------------------------


def test_inline_pasted_source_simple_one_paragraph_comment():
    body = (
        "Maybe\n\n"
        "Interesting but risky, worth a deeper look.\n\n"
        "PRN is a company with hidden earnings power due to a temporary segment "
        "loss. Valuation implies 3-4x upside if margins normalize.\n"
    )
    result = source_isolation.isolate_source(body, user_comment="Interesting but risky, worth a deeper look.")

    assert result.scorable is True
    assert result.source_type == "inline_pasted_source"
    assert result.source_text.startswith("PRN is a company with hidden earnings power")
    assert "Maybe" not in result.source_text
    assert "Interesting but risky" not in result.source_text


def test_inline_pasted_source_multi_paragraph_comment():
    comment = (
        "I really do not like the balance sheet here.\n\n"
        "Also the management team has a bad track record."
    )
    body = (
        f"Strong Pass\n\n{comment}\n\n"
        "MANU trades at a premium despite weak fundamentals and high leverage "
        "across the group, and the market has not yet priced in refinancing risk.\n"
    )
    result = source_isolation.isolate_source(body, user_comment=comment)

    assert result.scorable is True
    assert result.source_type == "inline_pasted_source"
    assert result.source_text.startswith("MANU trades at a premium")
    assert "balance sheet" not in result.source_text
    assert "management team" not in result.source_text
    assert "Strong Pass" not in result.source_text


@pytest.mark.parametrize("verdict_line", ["maybe...", "strong pass", "slight Like", "Slight Pass", "STRONG LIKE", "pass:"])
def test_inline_pasted_source_accepts_verdict_capitalization_and_variants(verdict_line):
    body = (
        f"{verdict_line}\n\n"
        "Short comment here.\n\n"
        "SRI is a company with a compelling long-term growth story driven by "
        "demand tailwinds that the market appears to be underestimating.\n"
    )
    result = source_isolation.isolate_source(body, user_comment="Short comment here.")

    assert result.scorable is True, f"expected {verdict_line!r} to be recognized as verdict-like"
    assert result.source_type == "inline_pasted_source"
    assert result.source_text.startswith("SRI is a company")


def test_inline_pasted_source_explicitly_has_no_verdict_or_comment_in_source():
    body = (
        "Maybe\n\n"
        "This one seems overlooked by the market.\n\n"
        "KMAR has a hidden asset base that sell-side analysts consistently "
        "undervalue relative to replacement cost.\n"
    )
    result = source_isolation.isolate_source(body, user_comment="This one seems overlooked by the market.")

    assert result.scorable is True
    assert "Maybe" not in result.source_text
    assert "MAYBE" not in result.source_text.upper() or "MAYBE" not in result.source_text
    assert "This one seems overlooked by the market." not in result.source_text
    assert "overlooked by the market" not in result.source_text


def test_inline_pasted_source_fallback_is_lowest_priority_forwarded_still_wins():
    """A forwarded-message marker present anywhere means the fallback is
    never even attempted, regardless of a verdict-like first line.
    """
    body = (
        f"{BRADS_COMMENT}\n\n"
        "---------- Forwarded message ---------\n"
        "From: analyst@example.com\n"
        "Subject: XYZ Corp writeup\n\n"
        "XYZ Corp trades at 5x normalized earnings.\n"
    )
    result = source_isolation.isolate_source(body, user_comment="anything -- must not matter here")
    assert result.scorable is True
    assert result.source_type == "forwarded_email"


def test_inline_pasted_source_fallback_is_lowest_priority_explicit_source_still_wins():
    body = "Maybe\n\nSOURCE:\n\nGHI Co is a spin-off with obscured segment economics.\n"
    result = source_isolation.isolate_source(body, user_comment="anything -- must not matter here")
    assert result.scorable is True
    assert result.source_type == "explicit_source_section"


def test_inline_pasted_source_does_not_trigger_on_ambiguous_interleaved_reply():
    """Reproduces the real BAR-reply shape: a new unquoted sentence at
    top, a Gmail 'On ... wrote:' header, and interleaved '> ' quoted
    lines. Must remain UNSCORABLE_SOURCE, never forced through the
    inline-paste fallback even though the top line looks verdict-like.
    """
    body = (
        "Maybe\n\n"
        "One more thought on this, following up on my earlier note.\n\n"
        "On Mon, Jan 5, 2026 at 3:00 PM Brad <brad@example.com> wrote:\n"
        "> Maybe\n"
        ">\n"
        "> Original comment here.\n"
        "A line stuck in the middle that is not quoted.\n"
        "> BAR is interesting because of a spin-off catalyst.\n"
    )
    result = source_isolation.isolate_source(body, user_comment="One more thought on this, following up on my earlier note.")

    assert result.scorable is False
    assert "ambiguous" in result.unscorable_reason.lower()


def test_inline_pasted_source_gmail_on_wrote_header_alone_blocks_the_fallback():
    """Even without interleaved '> ' lines, a Gmail reply header anywhere
    in the body must refuse the inline-paste fallback -- it's a strong
    reply-chain signal on its own.
    """
    body = (
        "Maybe\n\n"
        "Some comment.\n\n"
        "On Mon, Jan 5, 2026 at 3:00 PM Brad <brad@example.com> wrote:\n"
        "Some prior content that never got quote-marked.\n"
    )
    result = source_isolation.isolate_source(body, user_comment="Some comment.")

    assert result.scorable is False
    assert "gmail" in result.unscorable_reason.lower() or "wrote" in result.unscorable_reason.lower()


def test_inline_pasted_source_no_substantial_remainder_stays_unscorable():
    body = "Like\n\nGreat idea, love it.\n\n"
    result = source_isolation.isolate_source(body, user_comment="Great idea, love it.")

    assert result.scorable is False
    assert result.source_text is None


def test_inline_pasted_source_comment_mismatch_fails_closed():
    """If the parsed user_comment doesn't match what's actually in the
    body right after the verdict line (e.g. a paraphrase, not a verbatim
    reproduction), the fallback must refuse rather than guess.
    """
    body = "Pass\n\nThe balance sheet worries me a lot.\n\nDNLM has weak fundamentals across the board.\n"
    result = source_isolation.isolate_source(body, user_comment="A completely different summary that does not match.")

    assert result.scorable is False
    assert "could not be confidently aligned" in result.unscorable_reason


def test_inline_pasted_source_missing_user_comment_fails_closed():
    body = "Maybe\n\nSome comment.\n\nEurocell is interesting due to a structural recovery in volumes.\n"
    result = source_isolation.isolate_source(body, user_comment=None)

    assert result.scorable is False
    assert "no parsed user_comment" in result.unscorable_reason


def test_inline_pasted_source_non_verdict_first_line_falls_through_to_generic_message():
    body = "This is not a verdict line at all, just prose.\n\nKendrion looks interesting for various reasons.\n"
    result = source_isolation.isolate_source(body, user_comment="This is not a verdict line at all, just prose.")

    assert result.scorable is False
    assert "no recognized" in result.unscorable_reason.lower()


# --- paragraph-alignment refinement (Stage 5.14) -----------------------------
#
# Realistic reconstructions of the 5 production shapes where the original
# exact-prefix matcher failed: BAR/SRI/KMAR/Eurocell/Kendrion. Source
# paragraphs are deliberately full-length (~120-180 words), matching real
# investment-writeup scale -- a one-sentence stand-in would understate how
# much the alignment score actually drops once real source content begins,
# giving a falsely thin safety margin.

SRI_SOURCE = (
    "1) Investment Thesis SRI represents a compelling opportunity trading well below the "
    "value of its owned real estate and dairy production assets, with a resilient consumer "
    "staples business generating stable cash flow across economic cycles in Southeastern "
    "Europe. The company owns a substantial portfolio of production facilities and land that "
    "sell-side analysts consistently exclude from their sum-of-the-parts models, treating the "
    "entire enterprise as a low-multiple commodity dairy producer rather than recognizing the "
    "embedded real estate optionality. Management has hinted at a potential sale-leaseback "
    "transaction on select properties, which would crystallize a meaningful portion of this "
    "hidden value and could serve as a re-rating catalyst over the next 12 to 18 months if "
    "executed as discussed on recent earnings calls."
)
KMAR_SOURCE = (
    "Introduction and Business Model Kongsberg Maritime is a leading global supplier of "
    "technology for the maritime industry, spanning positioning, navigation, and vessel "
    "automation systems across commercial and defense end markets. The business was recently "
    "spun out from its parent as a standalone listed entity, giving management fresh "
    "incentives tied directly to the performance of this specific segment rather than a "
    "diversified conglomerate. Roughly sixty percent of revenue comes from long-cycle defense "
    "contracts with multi-year backlogs, providing unusual visibility for a company of this "
    "size, while the remaining commercial maritime business offers exposure to a recovering "
    "shipbuilding cycle globally."
)
KENDRION_SOURCE = (
    "The turnaround at Kendrion N.V. is no longer hidden and the stock has already re-rated "
    "significantly over the past year, leaving limited room for further multiple expansion "
    "absent a fresh catalyst that the market has not yet priced in. Margins have recovered "
    "from a low base as restructuring actions taken over the prior two years worked through "
    "the cost base, and the balance sheet has been repaired through a combination of asset "
    "sales and free cash flow generation, but consensus estimates already appear to reflect "
    "most of this recovery, and further upside likely requires a genuinely new development "
    "rather than continued execution on the existing plan."
)
BAR_SOURCE = (
    "*Barco\n<https://example.com/barco>\n(Brussels: BAR) holds a leading global position in "
    "projection and visualization technology across cinema, enterprise, and healthcare end "
    "markets, with a balance sheet that provides ample flexibility for continued investment "
    "through the cycle. The company has been diversifying away from its legacy cinema "
    "projection business, which has structurally declined since the pandemic, toward higher-"
    "margin enterprise collaboration and medical imaging display products that now represent "
    "the majority of segment profit. Recent quarters have shown early signs of margin "
    "inflection as this mix shift continues, though the market still prices the stock as if "
    "cinema remained the dominant driver of group economics."
)
EUROCELL_SOURCE = (
    "*Exec summary: the market is valuing roughly 15p of earnings as normal, but we believe "
    "normalized earnings power is closer to 20p once cost-cutting initiatives are fully "
    "realized and volumes recover from cyclical trough levels reached during the recent "
    "housing downturn. The company has taken material self-help actions including footprint "
    "rationalization and procurement savings that are only partially reflected in reported "
    "numbers to date, and a modest recovery in UK RMI volumes from currently depressed levels "
    "would be sufficient to drive earnings meaningfully above current consensus without "
    "requiring any assumption of a broader housing market recovery."
)


def test_sri_style_verdict_duplicated_plus_omitted_short_paragraph_excludes_both():
    """Pattern A + the SRI-specific nuance: verdict duplicated in
    user_comment, AND a second short Brad paragraph the parser omitted
    entirely from user_comment. BOTH must be excluded from source_text --
    the furthest defensible boundary is right before the real heading.
    """
    para0 = (
        "Love the hidden asset angle here and the potential for a re-rating once the market "
        "recognizes it. Cyclicality is not ideal but manageable given the balance sheet."
    )
    para1_omitted = "Only real negative is need to refinance the debt."
    body = f"Like\n\n{para0}\n\n{para1_omitted}\n\n{SRI_SOURCE}\n"
    comment = f"Like. {para0}"  # parser never captured para1_omitted at all

    result = source_isolation.isolate_source(body, user_comment=comment)

    assert result.scorable is True
    assert result.source_type == "inline_pasted_source"
    assert result.source_text.startswith("1) Investment Thesis SRI represents")
    assert "Love the hidden asset" not in result.source_text
    assert "refinance the debt" not in result.source_text  # the omitted paragraph
    assert "Like" not in result.source_text.split("\n")[0]


def test_kmar_style_verdict_duplicated_near_verbatim_otherwise():
    para0 = (
        "I like that it is a spinout with fresh incentives and a credible standalone "
        "strategy. Also don't like that current margins are close to target margins already."
    )
    body = f"slight Like\n\n{para0}\n\n{KMAR_SOURCE}\n"
    comment = f"slight Like. {para0}"

    result = source_isolation.isolate_source(body, user_comment=comment)

    assert result.scorable is True
    assert result.source_type == "inline_pasted_source"
    assert result.source_text.startswith("Introduction and Business Model")
    assert "spinout" not in result.source_text
    assert "target margins" not in result.source_text


def test_kendrion_style_verdict_duplicated_source_begins_as_ordinary_prose():
    """Proves the algorithm is not merely heading detection: the source
    here begins as plain prose, no markdown heading of any kind.
    """
    para0 = "Might have been interesting earlier in the cycle. Also not enough upside from here."
    body = f"Pass\n\n{para0}\n\n{KENDRION_SOURCE}\n"
    comment = f"Pass. {para0}"

    result = source_isolation.isolate_source(body, user_comment=comment)

    assert result.scorable is True
    assert result.source_type == "inline_pasted_source"
    assert result.source_text.startswith("The turnaround at Kendrion N.V.")
    assert "earlier in the cycle" not in result.source_text
    assert "not enough upside" not in result.source_text


def test_bar_style_paraphrased_comment_source_begins_with_title_and_link():
    para0 = (
        "Book value is generally not an interesting metric for me unless it is a completely "
        "tangible business. I am interested in the margin correction and the potential for a "
        "cyclical upturn. Also generally high existing dividend yields are not that "
        "interesting... prefer things right after dividend has been cut as that adds a lot of "
        "forced sellers.."
    )
    body = f"Maybe\n\n{para0}\n\n{BAR_SOURCE}\n"
    # A compressed paraphrase, NOT a verbatim copy -- this is exactly what
    # the LLM parser produces in practice.
    comment = (
        "Book value is generally not interesting unless completely tangible business. "
        "Interested in margin correction and potential cyclical upturn. "
        "High existing dividend yields not interesting; prefer things after dividend "
        "cut for forced sellers."
    )

    result = source_isolation.isolate_source(body, user_comment=comment)

    assert result.scorable is True
    assert result.source_type == "inline_pasted_source"
    assert result.source_text.startswith("*Barco")
    assert "book value" not in result.source_text.lower()
    assert "forced sellers" not in result.source_text
    assert "dividend yields" not in result.source_text


def test_eurocell_style_paraphrased_comment_source_begins_with_exec_summary():
    para0 = (
        "I like that this is an idea in a weak cyclical market with self help opportunities "
        "and that the core of the thesis is that earnings will be higher than people expect "
        "(as opposed to arguing for a higher multiple). However the potential upside is not "
        "enough given some of the thesis negatives...."
    )
    body = f"Maybe\n\n{para0}\n\n{EUROCELL_SOURCE}\n"
    comment = (
        "Likes that this is an idea in a weak cyclical market with self-help opportunities "
        "and that the core thesis is earnings will be higher than expected. However, the "
        "potential upside is not enough given some thesis negatives."
    )

    result = source_isolation.isolate_source(body, user_comment=comment)

    assert result.scorable is True
    assert result.source_type == "inline_pasted_source"
    assert result.source_text.startswith("*Exec summary")
    # Brad-only turns of phrase from his comment paragraph -- "self-help"
    # alone legitimately also appears in the real source text, so it is
    # not itself a valid leakage signal here.
    assert "opposed to arguing for a higher multiple" not in result.source_text
    assert "thesis negatives" not in result.source_text.lower()
    assert "Maybe" not in result.source_text


# --- adversarial: must remain UNSCORABLE ------------------------------------


def test_adversarial_generic_word_overlap_without_sufficient_alignment_is_unscorable():
    """The comment shares a few generic investment words with the body's
    first paragraph, but the paragraph is really unrelated source-like
    prose, not a real match -- must not be accepted just because a handful
    of words coincide.
    """
    comment = "I like the margin trajectory and the balance sheet strength here, and the potential for a re-rating."
    unrelated_para = (
        "XYZ Corp margin profile has been volatile in recent years, and balance sheet "
        "leverage remains elevated relative to peers. Input costs are rising across the "
        "segment and the company faces significant unrelated headwinds in a totally "
        "different end market that has nothing to do with any prior discussion, including "
        "regulatory uncertainty and currency translation effects that are expected to weigh "
        "on margins over the next several quarters."
    )
    body = f"Maybe\n\n{unrelated_para}\n\nSome further unrelated closing remarks about the sector.\n"

    result = source_isolation.isolate_source(body, user_comment=comment)
    assert result.scorable is False


def test_adversarial_large_unexplained_paragraph_between_comment_and_source_is_unscorable():
    """A short, plausible Brad comment paragraph is followed by a LARGE
    paragraph the parser never captured -- unlike SRI's short omission,
    this is too large to safely assume is more of Brad's own commentary,
    so the whole message must stay unscorable rather than risk leaking it.
    """
    para0 = "Interesting idea but I have some concerns about the balance sheet."
    large_unexplained = (
        "Separately I have been thinking about a totally different sector rotation thesis "
        "involving commodity prices and how that might affect a range of unrelated names over "
        "the next few quarters, which is a long tangent about macro views that has nothing to "
        "do with this specific idea at all and just keeps going for a while to simulate a "
        "large unrelated paragraph of text that the parser never captured in user_comment."
    )
    source_para = (
        "The company at hand trades at a significant discount to its intrinsic value based on "
        "a sum-of-the-parts analysis of its various operating segments and real estate holdings."
    )
    body = f"Maybe\n\n{para0}\n\n{large_unexplained}\n\n{source_para}\n"
    comment = (
        "Interesting idea but I have some concerns about the balance sheet and also the debt "
        "maturity schedule coming up next year."
    )

    result = source_isolation.isolate_source(body, user_comment=comment)
    assert result.scorable is False


def test_adversarial_tiny_remainder_is_unscorable():
    body = "Like\n\nGreat idea, love it, worth digging into further at some point.\n\nToo short.\n"
    result = source_isolation.isolate_source(
        body, user_comment="Great idea, love it, worth digging into further at some point."
    )
    assert result.scorable is False


def test_adversarial_reply_chain_on_wrote_remains_unscorable():
    body = (
        "Maybe\n\nOne more thought on this.\n\n"
        "On Mon, Jan 5, 2026 at 3:00 PM Brad <brad@example.com> wrote:\n"
        "Some prior content never quote-marked at all.\n"
    )
    result = source_isolation.isolate_source(body, user_comment="One more thought on this.")
    assert result.scorable is False


def test_adversarial_interleaved_quote_remains_unscorable():
    body = (
        "Maybe\n\nSome comment.\n\n"
        "> Quoted line one.\n"
        "Not quoted, stuck in the middle.\n"
        "> Quoted line two.\n"
    )
    result = source_isolation.isolate_source(body, user_comment="Some comment.")
    assert result.scorable is False


def test_adversarial_unrelated_user_comment_is_unscorable():
    body = "Maybe\n\nThis looks like a decent setup given the recent selloff.\n\nDNLM has weak fundamentals across the board and a stretched balance sheet heading into a soft consumer environment.\n"
    result = source_isolation.isolate_source(
        body, user_comment="Completely different discussion about a football match last weekend and nothing about investing at all."
    )
    assert result.scorable is False


def test_adversarial_no_plausible_paragraph_boundary_is_unscorable():
    """Only ONE paragraph exists after the verdict line at all -- there is
    no candidate boundary to even evaluate.
    """
    body = "Like\n\nJust one paragraph of commentary and nothing else follows it at all.\n"
    result = source_isolation.isolate_source(
        body, user_comment="Just one paragraph of commentary and nothing else follows it at all."
    )
    assert result.scorable is False
    assert "no plausible paragraph boundary" in result.unscorable_reason


# --- diagnostic: the pure alignment helpers, directly measured --------------


def test_tokenize_ignores_case_and_punctuation_differences():
    assert source_isolation._tokenize("Self-help, opportunities!") == ["self", "help", "opportunities"]
    assert source_isolation._tokenize("SELF HELP OPPORTUNITIES") == ["self", "help", "opportunities"]


def test_strip_leading_verdict_phrase_removes_at_most_one_occurrence():
    assert source_isolation._strip_leading_verdict_phrase("Like. Love the hidden asset angle.") == (
        "Love the hidden asset angle."
    )
    assert source_isolation._strip_leading_verdict_phrase("slight Like. I like that it is a spinout.") == (
        "I like that it is a spinout."
    )
    assert source_isolation._strip_leading_verdict_phrase("Pass. Might have been interesting earlier.") == (
        "Might have been interesting earlier."
    )


def test_strip_leading_verdict_phrase_is_a_noop_when_absent():
    text = "Love the hidden asset angle here."
    assert source_isolation._strip_leading_verdict_phrase(text) == text


def test_alignment_score_reports_expected_shape():
    score = source_isolation._alignment_score(["a", "b", "c"], ["a", "b", "c"])
    assert score["similarity"] == 1.0
    assert score["comment_coverage"] == 1.0
    assert score["unmatched_prefix_tokens"] == 0


def test_alignment_score_empty_inputs_are_zero_not_an_error():
    assert source_isolation._alignment_score([], ["a"])["similarity"] == 0.0
    assert source_isolation._alignment_score(["a"], [])["similarity"] == 0.0


def test_alignment_scores_have_a_real_safety_margin_between_positive_and_negative_cases():
    """Directly measures _alignment_score on the SAME realistic fixtures
    used above, pinning down the actual numbers this module's thresholds
    were calibrated against (see module docstring / _MIN_ALIGNMENT_
    SIMILARITY etc.) -- so a future change to the metric or thresholds
    has a concrete regression guard, not just end-to-end pass/fail tests.
    """
    def score_for(para0: str, source: str, comment: str, extra_before_source: str = "") -> tuple[dict, dict]:
        comment_tokens = source_isolation._tokenize(source_isolation._strip_leading_verdict_phrase(comment))
        accept_prefix = para0 + (" " + extra_before_source if extra_before_source else "")
        reject_prefix = accept_prefix + " " + source
        accept_score = source_isolation._alignment_score(source_isolation._tokenize(accept_prefix), comment_tokens)
        reject_score = source_isolation._alignment_score(source_isolation._tokenize(reject_prefix), comment_tokens)
        return accept_score, reject_score

    fixtures = [
        (
            "Love the hidden asset angle here and the potential for a re-rating once the market "
            "recognizes it. Cyclicality is not ideal but manageable given the balance sheet.",
            SRI_SOURCE,
            "Like. Love the hidden asset angle here and the potential for a re-rating once the "
            "market recognizes it. Cyclicality is not ideal but manageable given the balance sheet.",
            "Only real negative is need to refinance the debt.",
        ),
        (
            "I like that it is a spinout with fresh incentives and a credible standalone "
            "strategy. Also don't like that current margins are close to target margins already.",
            KMAR_SOURCE,
            "slight Like. I like that it is a spinout with fresh incentives and a credible "
            "standalone strategy. Also don't like that current margins are close to target "
            "margins already.",
            "",
        ),
        (
            "Might have been interesting earlier in the cycle. Also not enough upside from here.",
            KENDRION_SOURCE,
            "Pass. Might have been interesting earlier in the cycle. Also not enough upside from here.",
            "",
        ),
    ]

    for para0, source, comment, extra in fixtures:
        accept_score, reject_score = score_for(para0, source, comment, extra)
        assert accept_score["similarity"] >= source_isolation._MIN_ALIGNMENT_SIMILARITY
        assert accept_score["comment_coverage"] >= source_isolation._MIN_COMMENT_COVERAGE
        assert accept_score["unmatched_prefix_tokens"] <= source_isolation._MAX_UNMATCHED_PREFIX_TOKENS
        assert reject_score["unmatched_prefix_tokens"] > source_isolation._MAX_UNMATCHED_PREFIX_TOKENS
        # Meaningful margin, not a threshold squeaking by.
        assert accept_score["unmatched_prefix_tokens"] + 30 < reject_score["unmatched_prefix_tokens"]


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
