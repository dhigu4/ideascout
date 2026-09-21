"""Unit tests for ideascout/sources/yellowbrick.py's live diagnostic
(Stage 5.9/5.10): find_show_full_summary_candidates, DOM ancestor/sibling
inspection, actionable-candidate resolution, detect_hidden_data_blocks,
detect_gating_signals, and diagnose_live() end-to-end.

REAL production evidence from Brad's Stage 5.9 diagnostic run against pitch
143618 showed exactly one "Show full summary" text match -- a plain
<span class="mr-2 text-xs"> with no role/href/aria-expanded -- and clicking
that span directly had ZERO observable effect (no URL/body/canonical
change, no request, no popup, no gating, no aria change). This falsifies
the Stage 5.9 assumption that the text match itself is clickable. Stage
5.10's diagnose_live() now inspects up to 6 ancestor levels and both
immediate siblings of every such span and only clicks a resolved ancestor/
sibling when EXACTLY ONE actionable candidate is found -- never the span,
never a guess.

These tests prove the DIAGNOSTIC's own logic (DOM-context inspection,
tiered actionability classification, single-candidate resolution vs.
ambiguity, before/after metrics, network/popup capture and sanitization,
hidden-data-block + thesis-phrase reporting, gating detection) is correct
using controllable fakes -- they do NOT claim to know or fake the answer
to what the real Yellowbrick page's actual control is; that can only come
from Brad running `python run.py diagnose-source-live` against the real
site himself.

Pure/fake-page tests only -- no real Playwright, no network, no live
Yellowbrick site (see tests/browser_fakes.py).
"""

from __future__ import annotations

import pytest

from ideascout.sources import base, yellowbrick as yb
from tests.browser_fakes import FakeContext, FakePage, _FakeLocator

URL = "https://www.joinyellowbrick.com/sp/143618"
DISCOVERED = base.DiscoveredItem(
    external_id="143618", canonical_url=URL, ticker="BERNER-B.ST", company="XYZ Corp"
)

COLLAPSED_HTML = """
<html><head><title>Berner: XYZ Corp</title></head><body>
<article>
<h1>XYZ Corp (BERNER-B.ST)</h1>
<span class="mr-2 text-xs">Show full summary:</span>
<p>Short summary paragraph.</p>
</article>
</body></html>
"""

EXPANDED_HTML = """
<html><head><title>Berner: XYZ Corp</title></head><body>
<article>
<h1>XYZ Corp (BERNER-B.ST)</h1>
<p>Full thesis: valuation, catalysts, and risks all discussed here in
much greater length than the short summary paragraph ever was.</p>
</article>
</body></html>
"""


def _descriptor(**overrides) -> dict:
    base_descriptor = {
        "tag": "div",
        "class_name": None,
        "role": None,
        "href": None,
        "tabindex": None,
        "aria_expanded": None,
        "aria_controls": None,
        "has_onclick": False,
        "cursor": None,
        "visible": True,
        "enabled": True,
        "bounding_box": {"x": 0, "y": 0, "width": 100, "height": 20},
        "outer_html_start": "<div>",
        "text_excerpt": "",
    }
    base_descriptor.update(overrides)
    return base_descriptor


def _span_only_element(dom_context: dict | None = None) -> list[dict]:
    return [
        {
            "tag_name": "span",
            "visible": True,
            "enabled": True,
            "role": None,
            "href": None,
            "aria_expanded": None,
            "aria_controls": None,
            "type": None,
            "outer_html": '<span class="mr-2 text-xs">Show full summary:</span>',
            "bounding_box": {"x": 0, "y": 0, "width": 100, "height": 20},
            "dom_context": dom_context,
        }
    ]


def _empty_context() -> dict:
    return {"ancestors": [], "previous_sibling": None, "next_sibling": None, "container_outer_html": None}


# --- find_show_full_summary_candidates --------------------------------------


def test_find_candidates_reports_one_match_with_full_attributes():
    page = FakePage({URL: COLLAPSED_HTML}, show_full_summary_elements=_span_only_element(_empty_context()))
    page.goto(URL)

    candidates = yb.find_show_full_summary_candidates(page)

    assert len(candidates) == 1
    c = candidates[0]
    assert c["tag_name"] == "span"
    assert c["visible"] is True
    assert c["enabled"] is True
    assert "Show full summary" in c["outer_html_excerpt"]


def test_find_candidates_reports_multiple_matches_explicitly():
    elements = [
        {"tag_name": "div", "visible": False, "enabled": True, "outer_html": "<div>Show full summary</div>"},
        {"tag_name": "span", "visible": True, "enabled": True, "outer_html": "<span>Show full summary</span>"},
    ]
    page = FakePage({URL: COLLAPSED_HTML}, show_full_summary_elements=elements)
    page.goto(URL)

    candidates = yb.find_show_full_summary_candidates(page)
    assert len(candidates) == 2
    assert candidates[0]["visible"] is False
    assert candidates[1]["visible"] is True


# --- outerHTML / URL sanitization -------------------------------------------


def test_sanitize_outer_html_strips_query_strings_and_event_handlers():
    dirty = '<a href="/sp/1?token=SECRET123" onclick="doThing()">Show full summary</a>'
    cleaned = yb._sanitize_outer_html(dirty)
    assert "SECRET123" not in cleaned
    assert "onclick" not in cleaned
    assert "Show full summary" in cleaned


def test_sanitize_outer_html_caps_length_to_given_max_chars():
    dirty = "<a>" + ("x" * 1000) + "</a>"
    cleaned = yb._sanitize_outer_html(dirty, max_chars=50)
    assert len(cleaned) <= 51


def test_sanitize_outer_html_default_cap_still_applies():
    dirty = "<a>" + ("x" * 1000) + "</a>"
    cleaned = yb._sanitize_outer_html(dirty)
    assert len(cleaned) <= yb._MAX_OUTER_HTML_EXCERPT_CHARS + 1


def test_sanitize_url_strips_query_string_and_fragment():
    assert yb._sanitize_url("https://www.joinyellowbrick.com/sp/1?auth_token=abc#frag") == (
        "https://www.joinyellowbrick.com/sp/1"
    )


def test_sanitize_url_handles_none():
    assert yb._sanitize_url(None) == "unknown"


# --- _classify_actionability / _classify_dom_context ------------------------


def test_classify_actionability_prioritizes_button_over_cursor_pointer():
    assert yb._classify_actionability(_descriptor(tag="button", cursor="pointer")) == "button_tag"


def test_classify_actionability_recognizes_role_button():
    assert yb._classify_actionability(_descriptor(tag="div", role="button")) == "role_button"


def test_classify_actionability_recognizes_tabindex():
    assert yb._classify_actionability(_descriptor(tag="div", tabindex="0")) == "tabindex"


def test_classify_actionability_recognizes_onclick():
    assert yb._classify_actionability(_descriptor(tag="div", has_onclick=True)) == "onclick"


def test_classify_actionability_recognizes_cursor_pointer_as_last_resort():
    assert yb._classify_actionability(_descriptor(tag="div", cursor="pointer")) == "cursor_pointer"


def test_classify_actionability_none_for_plain_div():
    assert yb._classify_actionability(_descriptor(tag="div")) is None


def test_classify_actionability_none_for_missing_descriptor():
    assert yb._classify_actionability(None) is None


def test_classify_dom_context_tags_relations_correctly():
    context = {
        "ancestors": [_descriptor(tag="div"), _descriptor(tag="button")],
        "previous_sibling": _descriptor(tag="a", href="#"),
        "next_sibling": None,
    }
    found = yb._classify_dom_context(context)
    relations = {f["relation"] for f in found}
    assert relations == {"ancestor[1]", "previous_sibling"}


# --- _resolve_actionable_candidates ------------------------------------------


def test_resolve_actionable_candidates_returns_none_when_empty():
    resolution = yb._resolve_actionable_candidates([])
    assert resolution == {"resolved": None, "ambiguous": []}


def test_resolve_actionable_candidates_picks_single_best_tier_match():
    candidates = [
        {"relation": "ancestor[0]", "tier": "cursor_pointer"},
        {"relation": "ancestor[1]", "tier": "button_tag"},
    ]
    resolution = yb._resolve_actionable_candidates(candidates)
    assert resolution["resolved"]["relation"] == "ancestor[1]"
    assert resolution["ambiguous"] == []


def test_resolve_actionable_candidates_refuses_to_guess_between_equal_tier_matches():
    candidates = [
        {"relation": "ancestor[0]", "tier": "button_tag"},
        {"relation": "previous_sibling", "tier": "button_tag"},
    ]
    resolution = yb._resolve_actionable_candidates(candidates)
    assert resolution["resolved"] is None
    assert len(resolution["ambiguous"]) == 2


# --- detect_hidden_data_blocks (incl. Stage 5.10 thesis-phrase check) -------


def test_detect_hidden_data_blocks_finds_next_data_by_id_and_search_term():
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"ticker":"BERNER-B.ST"}}</script>'
    )
    blocks = yb.detect_hidden_data_blocks(html, search_terms=["BERNER-B.ST", None])
    assert len(blocks) == 1
    assert blocks[0]["block_id"] == "__NEXT_DATA__"
    assert blocks[0]["type"] == "application/json"
    assert "BERNER-B.ST" in blocks[0]["search_terms_found"]
    assert blocks[0]["thesis_phrases_found"] == []  # not a __next_f.push block


def test_detect_hidden_data_blocks_never_returns_block_content():
    padding = "x" * yb._LARGE_SCRIPT_CHAR_THRESHOLD
    html = f'<script id="__NEXT_DATA__">{{"secret": "should-never-appear-in-report", "pad": "{padding}"}}</script>'
    blocks = yb.detect_hidden_data_blocks(html, search_terms=[])
    assert len(blocks) == 1
    for value in blocks[0].values():
        assert "should-never-appear-in-report" not in str(value)


def test_detect_hidden_data_blocks_ignores_small_unremarkable_scripts():
    html = '<script>console.log("hi");</script>'
    blocks = yb.detect_hidden_data_blocks(html, search_terms=["BERNER-B.ST"])
    assert blocks == []


def test_detect_hidden_data_blocks_flags_large_scripts_even_without_a_marker():
    html = f"<script>{'x' * (yb._LARGE_SCRIPT_CHAR_THRESHOLD + 100)}</script>"
    blocks = yb.detect_hidden_data_blocks(html, search_terms=[])
    assert len(blocks) == 1
    assert blocks[0]["approx_chars"] > yb._LARGE_SCRIPT_CHAR_THRESHOLD


def test_detect_hidden_data_blocks_flags_thesis_phrases_in_next_f_push_blocks():
    html = '<script>self.__next_f.push([1,"Why Berner Industrier share price is depressed and Cervantes is a serial acquirer"])</script>'
    blocks = yb.detect_hidden_data_blocks(html, search_terms=[])
    assert len(blocks) == 1
    assert "__next_f.push" in blocks[0]["hydration_markers_found"]
    assert "Cervantes" in blocks[0]["thesis_phrases_found"]
    assert "serial acquirer" in blocks[0]["thesis_phrases_found"]
    assert "Why Berner Industrier share price is depressed" in blocks[0]["thesis_phrases_found"]


def test_detect_hidden_data_blocks_next_f_push_without_thesis_phrases_reports_empty():
    html = '<script>self.__next_f.push([1,"unrelated boilerplate hydration chunk with nothing notable"])</script>'
    blocks = yb.detect_hidden_data_blocks(html, search_terms=[])
    assert len(blocks) == 1
    assert blocks[0]["thesis_phrases_found"] == []


# --- detect_gating_signals ---------------------------------------------------


def test_detect_gating_signals_finds_known_phrases():
    html = "<html><body><p>Please subscribe to view this pitch.</p></body></html>"
    assert "subscribe" in yb.detect_gating_signals(html)


def test_detect_gating_signals_empty_when_none_present():
    assert yb.detect_gating_signals(EXPANDED_HTML) == []


# --- diagnose_live(): end-to-end diagnostic flow ----------------------------


def test_diagnose_live_span_only_no_actionable_ancestor_reports_unresolved():
    """Models the REAL production finding for pitch 143618: exactly one
    span match, no button/a/[role=button]/[tabindex]/onclick/cursor:pointer
    anywhere in its ancestors or siblings -- must be reported as
    unresolved, never guessed at.
    """
    page = FakePage({URL: COLLAPSED_HTML}, show_full_summary_elements=_span_only_element(_empty_context()))
    context = FakeContext(page)

    result = yb.diagnose_live(page, context, DISCOVERED)

    assert result["click_attempted"] is False
    assert result["actionable_resolution"]["resolved"] is None
    assert result["actionable_resolution"]["ambiguous"] == []
    assert "no actionable ancestor or sibling" in result["click_skipped_reason"]
    assert page.click_calls == []


def test_diagnose_live_text_label_inside_clickable_parent_button():
    dom_context = {
        "ancestors": [_descriptor(tag="button", cursor="pointer")],
        "previous_sibling": None,
        "next_sibling": None,
        "container_outer_html": "<div>...</div>",
    }
    page = FakePage(
        {URL: COLLAPSED_HTML},
        expanded_html_by_url={URL: EXPANDED_HTML},
        show_full_summary_elements=_span_only_element(dom_context),
    )
    context = FakeContext(page)

    result = yb.diagnose_live(page, context, DISCOVERED)

    assert result["click_attempted"] is True
    assert result["actionable_target"]["relation"] == "ancestor[0]"
    assert result["actionable_target"]["tier"] == "button_tag"
    assert result["meaningful_change_observed"] is True
    assert result["click_error"] is None


def test_diagnose_live_text_label_inside_clickable_div_role_button():
    dom_context = {
        "ancestors": [_descriptor(tag="div", role="button")],
        "previous_sibling": None,
        "next_sibling": None,
        "container_outer_html": None,
    }
    page = FakePage(
        {URL: COLLAPSED_HTML},
        expanded_html_by_url={URL: EXPANDED_HTML},
        show_full_summary_elements=_span_only_element(dom_context),
    )
    context = FakeContext(page)

    result = yb.diagnose_live(page, context, DISCOVERED)

    assert result["click_attempted"] is True
    assert result["actionable_target"]["tier"] == "role_button"
    assert result["meaningful_change_observed"] is True


def test_diagnose_live_clickable_sibling_rather_than_parent():
    dom_context = {
        "ancestors": [_descriptor(tag="div"), _descriptor(tag="div")],  # nothing actionable up the tree
        "previous_sibling": _descriptor(tag="button"),
        "next_sibling": None,
        "container_outer_html": None,
    }
    page = FakePage(
        {URL: COLLAPSED_HTML},
        expanded_html_by_url={URL: EXPANDED_HTML},
        show_full_summary_elements=_span_only_element(dom_context),
    )
    context = FakeContext(page)

    result = yb.diagnose_live(page, context, DISCOVERED)

    assert result["click_attempted"] is True
    assert result["actionable_target"]["relation"] == "previous_sibling"
    assert result["meaningful_change_observed"] is True


def test_diagnose_live_multiple_plausible_candidates_refuses_to_guess():
    """Two candidates at the SAME (highest-present) tier -- both tagged
    <button> -- is genuinely ambiguous. A lower-tier candidate elsewhere
    (e.g. an <a>) would NOT be ambiguous, since button_tag outranks
    anchor_tag; this test specifically needs an equal-tier tie.
    """
    dom_context = {
        "ancestors": [_descriptor(tag="button")],
        "previous_sibling": _descriptor(tag="button"),
        "next_sibling": None,
        "container_outer_html": None,
    }
    page = FakePage(
        {URL: COLLAPSED_HTML},
        expanded_html_by_url={URL: EXPANDED_HTML},
        show_full_summary_elements=_span_only_element(dom_context),
    )
    context = FakeContext(page)

    result = yb.diagnose_live(page, context, DISCOVERED)

    assert result["click_attempted"] is False
    assert len(result["actionable_resolution"]["ambiguous"]) == 2
    assert "refusing to guess" in result["click_skipped_reason"]
    assert page.click_calls == []


def test_diagnose_live_ancestor_click_produces_expanded_dom():
    dom_context = {
        "ancestors": [_descriptor(tag="button", aria_expanded="false")],
        "previous_sibling": None,
        "next_sibling": None,
        "container_outer_html": None,
    }
    page = FakePage(
        {URL: COLLAPSED_HTML},
        expanded_html_by_url={URL: EXPANDED_HTML},
        show_full_summary_elements=_span_only_element(dom_context),
    )
    context = FakeContext(page)

    result = yb.diagnose_live(page, context, DISCOVERED)

    assert result["meaningful_change_observed"] is True
    assert result["metrics_after"]["canonical_text_chars"] > result["metrics_before"]["canonical_text_chars"]
    assert "Full thesis" in yb.build_canonical_source_text(
        page.expanded_html_by_url[URL]
    )


def test_diagnose_live_ancestor_click_with_no_effect_is_reported_honestly():
    """The exact real-world shape of the Stage 5.9 finding but generalized
    to an ancestor click: clicking the resolved element is attempted, but
    nothing observably changes -- must never be reported as success.
    """
    dom_context = {
        "ancestors": [_descriptor(tag="button")],
        "previous_sibling": None,
        "next_sibling": None,
        "container_outer_html": None,
    }
    page = FakePage({URL: COLLAPSED_HTML}, show_full_summary_elements=_span_only_element(dom_context))
    context = FakeContext(page)

    result = yb.diagnose_live(page, context, DISCOVERED)

    assert result["click_attempted"] is True
    assert result["meaningful_change_observed"] is False
    assert result["metrics_before"]["canonical_text_chars"] == result["metrics_after"]["canonical_text_chars"]


def test_diagnose_live_click_failure_is_reported_not_raised():
    dom_context = {
        "ancestors": [_descriptor(tag="button")],
        "previous_sibling": None,
        "next_sibling": None,
        "container_outer_html": None,
    }
    page = FakePage({URL: COLLAPSED_HTML}, show_full_summary_elements=_span_only_element(dom_context))
    page.raise_on_click = True
    context = FakeContext(page)

    result = yb.diagnose_live(page, context, DISCOVERED)

    assert result["click_attempted"] is True
    assert result["click_error"] is not None
    assert result["meaningful_change_observed"] is False


def test_diagnose_live_captures_sanitized_requests_seen_during_click():
    """Simulates the click firing an API request -- test-only, not a claim
    about what the real page does. Patches _FakeLocator.click at the class
    level so it fires regardless of which relation's click path is taken.
    """
    dom_context = {
        "ancestors": [_descriptor(tag="button")],
        "previous_sibling": None,
        "next_sibling": None,
        "container_outer_html": None,
    }
    page = FakePage({URL: COLLAPSED_HTML}, show_full_summary_elements=_span_only_element(dom_context))
    context = FakeContext(page)

    original_evaluate = _FakeLocator.evaluate

    def evaluate_and_fire(self, script, arg=None):
        if "resolveRelatedNode" in script:
            page.simulate_request("POST", "https://api.joinyellowbrick.com/pitch/143618/expand?token=SECRET")
        return original_evaluate(self, script, arg)

    _FakeLocator.evaluate = evaluate_and_fire
    try:
        result = yb.diagnose_live(page, context, DISCOVERED)
    finally:
        _FakeLocator.evaluate = original_evaluate

    assert result["requests_seen_during_click"] == [
        {"method": "POST", "host": "api.joinyellowbrick.com", "path": "/pitch/143618/expand"}
    ]
    for req in result["requests_seen_during_click"]:
        assert "SECRET" not in str(req)


def test_diagnose_live_captures_a_popup_opened_during_click():
    dom_context = {
        "ancestors": [_descriptor(tag="button")],
        "previous_sibling": None,
        "next_sibling": None,
        "container_outer_html": None,
    }
    page = FakePage({URL: COLLAPSED_HTML}, show_full_summary_elements=_span_only_element(dom_context))
    context = FakeContext(page)

    original_evaluate = _FakeLocator.evaluate

    def evaluate_and_popup(self, script, arg=None):
        if "resolveRelatedNode" in script:
            context.simulate_popup("https://www.joinyellowbrick.com/sp/143618/full?ref=abc", "Full Pitch")
        return original_evaluate(self, script, arg)

    _FakeLocator.evaluate = evaluate_and_popup
    try:
        result = yb.diagnose_live(page, context, DISCOVERED)
    finally:
        _FakeLocator.evaluate = original_evaluate

    assert result["new_pages_opened"] == [
        {"url": "https://www.joinyellowbrick.com/sp/143618/full", "title": "Full Pitch"}
    ]


def test_diagnose_live_reports_hidden_data_blocks_from_the_raw_page():
    html_with_hydration = COLLAPSED_HTML.replace(
        "<article>",
        '<script id="__NEXT_DATA__" type="application/json">{"ticker":"BERNER-B.ST"}</script><article>',
    )
    page = FakePage({URL: html_with_hydration}, show_full_summary_elements=_span_only_element(_empty_context()))
    context = FakeContext(page)

    result = yb.diagnose_live(page, context, DISCOVERED)

    assert len(result["hidden_data_blocks"]) == 1
    assert result["hidden_data_blocks"][0]["block_id"] == "__NEXT_DATA__"
    assert "BERNER-B.ST" in result["hidden_data_blocks"][0]["search_terms_found"]


def test_diagnose_live_reports_gating_signals_before_and_after():
    gated_html = COLLAPSED_HTML.replace(
        "<p>Short summary paragraph.</p>", "<p>Please subscribe to unlock the full pitch.</p>"
    )
    page = FakePage({URL: gated_html}, show_full_summary_elements=_span_only_element(_empty_context()))
    context = FakeContext(page)

    result = yb.diagnose_live(page, context, DISCOVERED)

    assert "subscribe" in result["gating_signals_before"]


def test_diagnose_live_raises_auth_required_when_session_looks_blocked():
    login_html = '<html><body><input type="password"></body></html>'
    page = FakePage({URL: login_html})
    context = FakeContext(page)

    with pytest.raises(base.AuthRequiredError):
        yb.diagnose_live(page, context, DISCOVERED)


def test_diagnose_live_never_clicks_the_span_itself():
    """The core Stage 5.10 behavior change: even when the span is visible
    and enabled, diagnose_live() must never call .click() on the span
    locator directly -- only ever on a resolved ancestor/sibling relation.
    """
    dom_context = {
        "ancestors": [_descriptor(tag="button")],
        "previous_sibling": None,
        "next_sibling": None,
        "container_outer_html": None,
    }
    page = FakePage(
        {URL: COLLAPSED_HTML},
        expanded_html_by_url={URL: EXPANDED_HTML},
        show_full_summary_elements=_span_only_element(dom_context),
    )
    context = FakeContext(page)

    yb.diagnose_live(page, context, DISCOVERED)

    # click_calls records relation-qualified clicks (e.g. "text::ancestor[0]"),
    # never a bare span click.
    assert all("::" in call for call in page.click_calls)


# --- format_live_diagnostic_report never leaks sensitive data --------------


def test_format_report_never_prints_query_strings_or_full_script_bodies():
    page = FakePage({URL: COLLAPSED_HTML}, show_full_summary_elements=_span_only_element(_empty_context()))
    context = FakeContext(page)
    result = yb.diagnose_live(page, context, DISCOVERED)
    report = yb.format_live_diagnostic_report(result)

    assert "?" not in report or "http" not in report  # no query strings survive in any printed URL
    assert "cookie" not in report.lower()
    assert "token" not in report.lower()


def test_format_report_includes_ancestor_chain_and_resolution():
    dom_context = {
        "ancestors": [_descriptor(tag="button", cursor="pointer")],
        "previous_sibling": None,
        "next_sibling": None,
        "container_outer_html": "<div>container excerpt</div>",
    }
    page = FakePage(
        {URL: COLLAPSED_HTML},
        expanded_html_by_url={URL: EXPANDED_HTML},
        show_full_summary_elements=_span_only_element(dom_context),
    )
    context = FakeContext(page)
    result = yb.diagnose_live(page, context, DISCOVERED)
    report = yb.format_live_diagnostic_report(result)

    assert "ancestor chain" in report
    assert "Actionable target resolved" in report
    assert "container excerpt" in report
