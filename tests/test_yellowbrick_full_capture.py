"""Unit tests for ideascout/sources/yellowbrick.py's full-pitch-capture
production fix (Stage 5.11): _select_full_summary_switch, _switch_is_checked,
_expand_full_summary_if_present, _content_is_complete, and fetch()'s
switch-click + completeness-guard behavior.

REAL production evidence (Stage 5.10 live diagnostic against pitch 143618)
made the DOM unambiguous: the visible "Show full summary:" text is a plain
<span> LABEL; the actual clickable control is its next sibling, a
`<button id="full-summary-switch" role="switch" aria-checked="false"
data-state="unchecked">`. Clicking that BUTTON (never the label) produced a
real expansion (canonical text grew materially) with no request/popup/URL
change/gating -- but the "Show full summary:" label REMAINED visible
afterward. This falsifies the Stage 5.7 completeness guard, which
(wrongly) inferred completeness from the label's absence. These tests pin
down the REPLACEMENT: fetch() now clicks the role="switch" element (never
the label), and completeness is derived SOLELY from that switch's own
aria-checked/data-state -- never from whether the label text is present.

Pure parsing/fake-page tests only -- no real Playwright, no network, no
live Yellowbrick site (see tests/browser_fakes.py's FakePage).
"""

from __future__ import annotations

from ideascout.sources import base, yellowbrick
from tests.browser_fakes import FakePage

DISCOVERED = base.DiscoveredItem(external_id="143618", canonical_url="https://www.joinyellowbrick.com/sp/143618")
PITCH_URL = "https://www.joinyellowbrick.com/sp/143618"

COLLAPSED_HTML = """
<html><head><title>Berner: XYZ Corp</title></head><body>
<article class="pitch-card">
<h1>XYZ Corp (XYZ)</h1>
<p>Author: Berner</p>
<p>Market cap: $1B, category: tech</p>
<span class="mr-2 text-xs">Show full summary:</span>
<button id="full-summary-switch" type="button" role="switch" aria-checked="false" data-state="unchecked"></button>
<p>Short one paragraph summary here.</p>
<p>(4 min)</p>
</article>
</body></html>
"""

EXPANDED_HTML = """
<html><head><title>Berner: XYZ Corp</title></head><body>
<article class="pitch-card">
<h1>XYZ Corp (XYZ)</h1>
<p>Author: Berner</p>
<p>Market cap: $1B, category: tech</p>
<span class="mr-2 text-xs">Show full summary:</span>
<button id="full-summary-switch" type="button" role="switch" aria-checked="true" data-state="checked"></button>
<p>Short one paragraph summary here.</p>
<p>Full thesis: XYZ Corp trades at 5x normalized earnings due to a
temporary loss-making segment. Valuation discussion follows -- the
market is mispricing a durable cash-generative core business. Catalysts
include a spinoff within 12 months. Key risks include execution and
customer concentration.</p>
<p>(4 min)</p>
</article>
</body></html>
"""

# The label REMAINS visible after expansion -- deliberately identical to
# EXPANDED_HTML's label/switch markup, proving the label's presence must
# never be used to infer incompleteness (that was the Stage 5.7 bug).
ALREADY_EXPANDED_HTML = """
<html><head><title>Berner: XYZ Corp</title></head><body>
<article class="pitch-card">
<h1>XYZ Corp (XYZ)</h1>
<p>Author: Berner</p>
<span class="mr-2 text-xs">Show full summary:</span>
<button id="full-summary-switch" type="button" role="switch" aria-checked="true" data-state="checked"></button>
<p>Full thesis: valuation, catalysts, and risks already visible because
this switch is already checked.</p>
</article>
</body></html>
"""

SHORT_BUT_COMPLETE_HTML = """
<html><head><title>Berner: Tiny Co</title></head><body>
<article class="pitch-card">
<h1>Tiny Co (TINY)</h1>
<p>Author: Berner</p>
<p>This is a genuinely short pitch: the company is a net-net trading
below cash with no debt and an activist has just filed a 13D.</p>
</article>
</body></html>
"""


def _switches(aria_checked="false", data_state="unchecked", **overrides):
    element = {"visible": True, "enabled": True, "aria_checked": aria_checked, "data_state": data_state}
    element.update(overrides)
    return {"switch": [element]}


# --- _select_full_summary_switch / _switch_is_checked -----------------------


def test_select_full_summary_switch_finds_the_single_switch():
    descriptor = yellowbrick._select_full_summary_switch(COLLAPSED_HTML)
    assert descriptor is not None
    assert descriptor["id"] == "full-summary-switch"
    assert descriptor["aria_checked"] == "false"
    assert descriptor["data_state"] == "unchecked"
    assert descriptor["index"] == 0


def test_select_full_summary_switch_none_when_no_switch_present():
    assert yellowbrick._select_full_summary_switch(SHORT_BUT_COMPLETE_HTML) is None


def test_select_full_summary_switch_disambiguates_by_id_when_multiple_switches_exist():
    html = COLLAPSED_HTML.replace(
        "</article>",
        '<div role="switch" aria-checked="false"></div></article>',
    )
    descriptor = yellowbrick._select_full_summary_switch(html)
    assert descriptor is not None
    assert descriptor["id"] == "full-summary-switch"


def test_select_full_summary_switch_falls_back_to_label_relation_when_still_ambiguous():
    # Two switches, neither with the known id -- must fall back to
    # "comes right after the label" rather than guessing.
    html = """
    <html><body>
    <div role="switch" aria-checked="false" id="unrelated-switch-a"></div>
    <span>Show full summary:</span>
    <button role="switch" aria-checked="false" id="unrelated-switch-b"></button>
    </body></html>
    """
    descriptor = yellowbrick._select_full_summary_switch(html)
    assert descriptor is not None
    assert descriptor["id"] == "unrelated-switch-b"


def test_select_full_summary_switch_returns_none_when_genuinely_ambiguous():
    html = """
    <html><body>
    <span>Show full summary:</span>
    <div role="switch" aria-checked="false" id="a"></div>
    <div role="switch" aria-checked="false" id="b"></div>
    </body></html>
    """
    assert yellowbrick._select_full_summary_switch(html) is None


def test_switch_is_checked_true_from_aria_checked():
    assert yellowbrick._switch_is_checked({"aria_checked": "true", "data_state": None}) is True


def test_switch_is_checked_true_from_data_state():
    assert yellowbrick._switch_is_checked({"aria_checked": None, "data_state": "checked"}) is True


def test_switch_is_checked_false_from_either_signal():
    assert yellowbrick._switch_is_checked({"aria_checked": "false", "data_state": None}) is False
    assert yellowbrick._switch_is_checked({"aria_checked": None, "data_state": "unchecked"}) is False


def test_switch_is_checked_none_when_neither_attribute_present():
    assert yellowbrick._switch_is_checked({"aria_checked": None, "data_state": None}) is None


# --- _content_is_complete: the NEW authoritative completeness rule ---------


def test_content_is_complete_false_when_switch_unchecked():
    assert yellowbrick._content_is_complete(COLLAPSED_HTML) is False


def test_content_is_complete_true_when_switch_checked():
    assert yellowbrick._content_is_complete(EXPANDED_HTML) is True


def test_content_is_complete_true_when_no_switch_present_at_all():
    """Task section 2's explicit fallback: no switch -> potentially
    already complete, never automatically incomplete.
    """
    assert yellowbrick._content_is_complete(SHORT_BUT_COMPLETE_HTML) is True


def test_content_is_complete_label_remaining_visible_does_not_imply_incomplete():
    """The exact bug this fix corrects: the label is present in BOTH
    EXPANDED_HTML and ALREADY_EXPANDED_HTML, yet completeness must be
    driven by the switch's own state, not the label's presence.
    """
    assert "Show full summary" in EXPANDED_HTML
    assert yellowbrick._content_is_complete(EXPANDED_HTML) is True
    assert "Show full summary" in ALREADY_EXPANDED_HTML
    assert yellowbrick._content_is_complete(ALREADY_EXPANDED_HTML) is True


# --- _expand_full_summary_if_present: clicks the SWITCH, not the label -----


def test_expand_clicks_the_switch_not_the_label_and_returns_expanded_html():
    page = FakePage(
        {PITCH_URL: COLLAPSED_HTML},
        expanded_html_by_url={PITCH_URL: EXPANDED_HTML},
        role_elements=_switches(),
    )
    page.goto(PITCH_URL)
    result = yellowbrick._expand_full_summary_if_present(page, COLLAPSED_HTML)

    assert page.click_calls == ["role=switch[0]"]  # never a bare label click
    assert result == EXPANDED_HTML
    assert yellowbrick._content_is_complete(result) is True


def test_expand_is_a_no_op_when_switch_already_checked():
    page = FakePage({PITCH_URL: ALREADY_EXPANDED_HTML}, role_elements=_switches(aria_checked="true", data_state="checked"))
    page.goto(PITCH_URL)
    result = yellowbrick._expand_full_summary_if_present(page, ALREADY_EXPANDED_HTML)

    assert page.click_calls == []  # never re-toggles an already-checked switch
    assert result == ALREADY_EXPANDED_HTML


def test_expand_is_a_no_op_when_no_switch_present():
    page = FakePage({PITCH_URL: SHORT_BUT_COMPLETE_HTML})
    page.goto(PITCH_URL)
    result = yellowbrick._expand_full_summary_if_present(page, SHORT_BUT_COMPLETE_HTML)

    assert page.click_calls == []
    assert result == SHORT_BUT_COMPLETE_HTML


def test_expand_waits_for_switch_to_become_checked_not_a_fixed_sleep():
    page = FakePage(
        {PITCH_URL: COLLAPSED_HTML},
        expanded_html_by_url={PITCH_URL: EXPANDED_HTML},
        role_elements=_switches(),
    )
    page.goto(PITCH_URL)
    result = yellowbrick._expand_full_summary_if_present(page, COLLAPSED_HTML)

    assert page.wait_for_timeout_calls  # a bounded polling wait actually happened
    assert result == EXPANDED_HTML
    assert yellowbrick._switch_is_checked(yellowbrick._select_full_summary_switch(result)) is True


def test_expand_returns_original_html_when_switch_never_becomes_clickable():
    page = FakePage(
        {PITCH_URL: COLLAPSED_HTML},
        expanded_html_by_url={PITCH_URL: EXPANDED_HTML},
        role_elements=_switches(),
    )
    page.goto(PITCH_URL)
    page.raise_on_click = True

    result = yellowbrick._expand_full_summary_if_present(page, COLLAPSED_HTML)

    assert result == COLLAPSED_HTML
    assert yellowbrick._content_is_complete(result) is False


def test_expand_returns_original_html_when_switch_not_visible_or_enabled():
    page = FakePage(
        {PITCH_URL: COLLAPSED_HTML},
        expanded_html_by_url={PITCH_URL: EXPANDED_HTML},
        role_elements={"switch": [{"visible": False, "enabled": True, "aria_checked": "false", "data_state": "unchecked"}]},
    )
    page.goto(PITCH_URL)

    result = yellowbrick._expand_full_summary_if_present(page, COLLAPSED_HTML)

    assert page.click_calls == []
    assert result == COLLAPSED_HTML


def test_expand_fails_to_transition_within_bounded_wait_returns_still_unchecked_html():
    """A click happens (no exception), but the switch never actually
    reports checked within the bounded wait -- e.g. expanded_html_by_url
    is never configured, so page.content() keeps returning collapsed
    HTML. Must not hang, and must not fabricate success.
    """
    page = FakePage({PITCH_URL: COLLAPSED_HTML}, role_elements=_switches())
    page.goto(PITCH_URL)

    result = yellowbrick._expand_full_summary_if_present(page, COLLAPSED_HTML)

    assert page.click_calls == ["role=switch[0]"]
    assert yellowbrick._content_is_complete(result) is False


# --- fetch(): end-to-end switch-click + completeness guard ------------------


def test_fetch_clicks_switch_and_marks_content_complete():
    page = FakePage(
        {PITCH_URL: COLLAPSED_HTML},
        expanded_html_by_url={PITCH_URL: EXPANDED_HTML},
        role_elements=_switches(),
    )
    item = yellowbrick.fetch(page, DISCOVERED, discovered_at="2026-01-01T00:00:00+00:00")

    assert item.content_complete is True
    assert "Full thesis" in item.raw_html
    assert "Full thesis" in yellowbrick.build_canonical_source_text(item.raw_html)
    assert page.click_calls == ["role=switch[0]"]


def test_fetch_already_expanded_pitch_works_without_extra_click():
    page = FakePage(
        {PITCH_URL: ALREADY_EXPANDED_HTML}, role_elements=_switches(aria_checked="true", data_state="checked")
    )
    item = yellowbrick.fetch(page, DISCOVERED, discovered_at="2026-01-01T00:00:00+00:00")

    assert page.click_calls == []
    assert item.content_complete is True


def test_fetch_aria_checked_false_to_true_accepted_as_successful_expansion():
    page = FakePage(
        {PITCH_URL: COLLAPSED_HTML},
        expanded_html_by_url={PITCH_URL: EXPANDED_HTML},
        role_elements=_switches(),
    )
    item = yellowbrick.fetch(page, DISCOVERED, discovered_at="2026-01-01T00:00:00+00:00")

    assert 'aria-checked="false"' in COLLAPSED_HTML
    assert 'aria-checked="true"' in item.raw_html
    assert item.content_complete is True


def test_fetch_data_state_unchecked_to_checked_accepted_as_successful_expansion():
    collapsed_data_state_only = COLLAPSED_HTML.replace('aria-checked="false" ', "")
    expanded_data_state_only = EXPANDED_HTML.replace('aria-checked="true" ', "")
    page = FakePage(
        {PITCH_URL: collapsed_data_state_only},
        expanded_html_by_url={PITCH_URL: expanded_data_state_only},
        role_elements=_switches(aria_checked=None),
    )
    item = yellowbrick.fetch(page, DISCOVERED, discovered_at="2026-01-01T00:00:00+00:00")

    assert 'data-state="checked"' in item.raw_html
    assert item.content_complete is True


def test_fetch_label_remaining_visible_after_expansion_does_not_imply_incomplete():
    page = FakePage(
        {PITCH_URL: COLLAPSED_HTML},
        expanded_html_by_url={PITCH_URL: EXPANDED_HTML},
        role_elements=_switches(),
    )
    item = yellowbrick.fetch(page, DISCOVERED, discovered_at="2026-01-01T00:00:00+00:00")

    assert "Show full summary" in item.raw_html  # label still there
    assert item.content_complete is True  # but that alone is not incompleteness


def test_fetch_already_checked_switch_is_not_toggled_closed():
    page = FakePage(
        {PITCH_URL: ALREADY_EXPANDED_HTML}, role_elements=_switches(aria_checked="true", data_state="checked")
    )
    item = yellowbrick.fetch(page, DISCOVERED, discovered_at="2026-01-01T00:00:00+00:00")

    assert page.click_calls == []
    assert item.content_complete is True
    assert item.raw_html == ALREADY_EXPANDED_HTML


def test_fetch_failed_switch_transition_marks_content_incomplete():
    page = FakePage(
        {PITCH_URL: COLLAPSED_HTML},
        expanded_html_by_url={PITCH_URL: EXPANDED_HTML},
        role_elements=_switches(),
    )
    page.raise_on_click = True

    item = yellowbrick.fetch(page, DISCOVERED, discovered_at="2026-01-01T00:00:00+00:00")

    assert item.content_complete is False
    # Raw provenance is still saved even though it's incomplete.
    assert item.raw_html == COLLAPSED_HTML


def test_fetch_short_but_genuinely_complete_pitch_is_accepted():
    page = FakePage({PITCH_URL: SHORT_BUT_COMPLETE_HTML})
    item = yellowbrick.fetch(page, DISCOVERED, discovered_at="2026-01-01T00:00:00+00:00")

    assert item.content_complete is True
    assert page.click_calls == []


def test_fetch_expanded_raw_html_is_what_gets_persisted():
    page = FakePage(
        {PITCH_URL: COLLAPSED_HTML},
        expanded_html_by_url={PITCH_URL: EXPANDED_HTML},
        role_elements=_switches(),
    )
    item = yellowbrick.fetch(page, DISCOVERED, discovered_at="2026-01-01T00:00:00+00:00")

    # raw_html (what gets saved as provenance) is the EXPANDED capture --
    # role="switch" aria-checked="true" data-state="checked" and all.
    assert item.raw_html == EXPANDED_HTML
    assert 'aria-checked="true"' in item.raw_html
    assert 'data-state="checked"' in item.raw_html


def test_fetch_canonical_text_contains_expanded_substantive_content_and_excludes_ui_noise():
    page = FakePage(
        {PITCH_URL: COLLAPSED_HTML},
        expanded_html_by_url={PITCH_URL: EXPANDED_HTML},
        role_elements=_switches(),
    )
    item = yellowbrick.fetch(page, DISCOVERED, discovered_at="2026-01-01T00:00:00+00:00")

    canonical = yellowbrick.build_canonical_source_text(item.raw_html)
    assert "Valuation discussion" in canonical
    assert "Catalysts" in canonical and "spinoff within 12 months" in canonical
    assert "Key risks include execution" in canonical
    assert "Show full summary" not in canonical  # UI label stripped


def test_fetch_rerun_after_full_content_capture_is_idempotent():
    page1 = FakePage(
        {PITCH_URL: COLLAPSED_HTML},
        expanded_html_by_url={PITCH_URL: EXPANDED_HTML},
        role_elements=_switches(),
    )
    item1 = yellowbrick.fetch(page1, DISCOVERED, discovered_at="2026-01-01T00:00:00+00:00")

    page2 = FakePage({PITCH_URL: EXPANDED_HTML}, role_elements=_switches(aria_checked="true", data_state="checked"))
    item2 = yellowbrick.fetch(page2, DISCOVERED, discovered_at="2026-01-02T00:00:00+00:00")

    assert item1.content_hash == item2.content_hash
    assert item1.content_complete is True
    assert item2.content_complete is True
    assert page2.click_calls == []  # already checked -- no re-click on rerun
