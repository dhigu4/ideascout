"""Unit tests for ideascout/sources/yellowbrick.py's page-validity gate
(production fix): classify_page_validity, and fetch()'s bounded transient-
404 recovery (wait -> re-check -> ONE reload -> re-check).

Real production evidence: yellowbrick/144271 was captured as Yellowbrick's
own "Page not found" page and wrongly treated as a valid, complete pitch
(content_complete was True because a 404 page has no full-summary switch,
and "no switch" was -- correctly -- never itself treated as incomplete).
It went through extraction and screening, producing a meaningless
INSUFFICIENT_INFORMATION result. A LATER fetch of the EXACT SAME
external_id returned a real pitch (Instone Real Estate Group SE),
confirming the 404 was transient, not permanent.

Pure parsing/fake-page tests only -- no real Playwright, no network, no
live Yellowbrick site (see tests/browser_fakes.py's FakePage).
"""

from __future__ import annotations

from ideascout.sources import base, yellowbrick
from tests.browser_fakes import FakePage

DISCOVERED = base.DiscoveredItem(external_id="144271", canonical_url="https://www.joinyellowbrick.com/sp/144271")
PITCH_URL = "https://www.joinyellowbrick.com/sp/144271"

# The real production 404 page's distinguishing content.
NOT_FOUND_HTML = """
<html><head><title>Yellowbrick Investing</title></head><body>
<h1>Page not found</h1>
<p>The page you are looking for does not exist, has been removed, or is
temporarily unavailable.</p>
</body></html>
"""

# A generic error/shell page with NO explicit "not found" wording at all --
# still not a real pitch, and still carrying the generic shell title.
GENERIC_SHELL_HTML = """
<html><head><title>Yellowbrick Investing</title></head><body>
<p>Oops, something went wrong. Please try again later.</p>
</body></html>
"""

# A completely blank shell -- no title, almost no body text.
BLANK_SHELL_HTML = "<html><head><title></title></head><body></body></html>"

REAL_PITCH_HTML = """
<html><head><title>INS.DE stock pitch - 2026-09-28</title></head><body>
<article class="pitch-card">
<h1>Instone Real Estate Group SE (INS.DE)</h1>
<p>Instone trades at a steep discount to NAV after a sector-wide
derating. The core land bank is conservatively marked and recent presales
suggest the market is too pessimistic about near-term cash generation.
Catalysts include asset disposals and a return to dividend payments.
Key risks include financing costs and continued weakness in German
residential construction.</p>
</article>
</body></html>
"""

# A genuinely short, real pitch with its own specific title and no
# full-summary switch -- must remain valid despite being well under the
# generic-shell length threshold.
SHORT_REAL_PITCH_HTML = """
<html><head><title>Berner: Tiny Co</title></head><body>
<article class="pitch-card">
<h1>Tiny Co (TINY)</h1>
<p>This is a genuinely short pitch: the company is a net-net trading
below cash with no debt and an activist has just filed a 13D.</p>
</article>
</body></html>
"""

REAL_PITCH_WITH_SWITCH_HTML = """
<html><head><title>Berner: XYZ Corp</title></head><body>
<article class="pitch-card">
<h1>XYZ Corp (XYZ)</h1>
<span class="mr-2 text-xs">Show full summary:</span>
<button id="full-summary-switch" type="button" role="switch" aria-checked="true" data-state="checked"></button>
<p>Full thesis: XYZ Corp trades at 5x normalized earnings due to a
temporary loss-making segment. Valuation discussion follows -- the
market is mispricing a durable cash-generative core business. Catalysts
include a spinoff within 12 months. Key risks include execution and
customer concentration.</p>
</article>
</body></html>
"""


def _switches(aria_checked="true", data_state="checked", **overrides):
    element = {"visible": True, "enabled": True, "aria_checked": aria_checked, "data_state": data_state}
    element.update(overrides)
    return {"switch": [element]}


# --- VALIDITY: classify_page_validity ------------------------------------------


def test_exact_yellowbrick_page_not_found_is_invalid():
    result = yellowbrick.classify_page_validity(NOT_FOUND_HTML)
    assert not result.valid
    assert result.reason == "PAGE_NOT_FOUND"


def test_generic_error_shell_with_no_pitch_is_invalid():
    result = yellowbrick.classify_page_validity(GENERIC_SHELL_HTML)
    assert not result.valid
    assert result.reason == "NO_PITCH_CONTENT"


def test_blank_shell_with_no_title_is_invalid():
    result = yellowbrick.classify_page_validity(BLANK_SHELL_HTML)
    assert not result.valid
    assert result.reason == "NO_PITCH_CONTENT"


def test_real_short_pitch_with_no_full_summary_switch_remains_valid():
    """The pre-existing rule ("no switch may be a legitimate short pitch")
    still holds -- but only once page validity is established. A real
    pitch's title is always specific to the company, never the generic
    Yellowbrick shell title, so it is never caught by the length check.
    """
    result = yellowbrick.classify_page_validity(SHORT_REAL_PITCH_HTML)
    assert result.valid
    assert result.reason is None
    assert yellowbrick._content_is_complete(SHORT_REAL_PITCH_HTML) is True


def test_real_pitch_with_full_summary_switch_remains_valid():
    result = yellowbrick.classify_page_validity(REAL_PITCH_WITH_SWITCH_HTML)
    assert result.valid
    assert result.reason is None


def test_real_long_pitch_is_valid():
    result = yellowbrick.classify_page_validity(REAL_PITCH_HTML)
    assert result.valid
    assert result.reason is None


def test_validity_does_not_rely_on_length_alone():
    """Neither signal is sufficient by itself: a short page with a REAL,
    company-specific title stays valid (length alone never condemns it),
    and a page with the generic shell title but comfortably past the
    generic-shell length floor is treated as valid too (the generic title
    alone, without also being short, never condemns it either) -- only
    the TWO signals TOGETHER (generic title AND short) mark a page
    invalid, which is exactly what distinguishes this from "rely only on
    text length."
    """
    assert yellowbrick.classify_page_validity(SHORT_REAL_PITCH_HTML).valid

    long_generic_titled_html = (
        '<html><head><title>Yellowbrick Investing</title></head><body><p>'
        + ("This is unusually long filler text that is not an error page. " * 10)
        + "</p></body></html>"
    )
    assert yellowbrick.classify_page_validity(long_generic_titled_html).valid


# --- TRANSIENT RECOVERY: fetch()'s bounded wait/reload retry --------------------


def test_fetch_recovers_when_retry_returns_valid_pitch():
    page = FakePage(
        html_by_url={},
        content_sequence_by_url={PITCH_URL: [NOT_FOUND_HTML, NOT_FOUND_HTML, REAL_PITCH_HTML]},
    )
    item = yellowbrick.fetch(page, DISCOVERED, discovered_at="2026-01-01T00:00:00+00:00")

    assert item.content_complete is True
    assert item.metadata == {}
    assert "Instone" in item.raw_html
    assert len(page.reload_calls) == 1  # exactly one retry
    assert len(page.wait_for_timeout_calls) == 1


def test_fetch_stays_incomplete_when_retry_still_invalid():
    page = FakePage(
        html_by_url={},
        content_sequence_by_url={PITCH_URL: [NOT_FOUND_HTML, NOT_FOUND_HTML, NOT_FOUND_HTML]},
    )
    item = yellowbrick.fetch(page, DISCOVERED, discovered_at="2026-01-01T00:00:00+00:00")

    assert item.content_complete is False
    assert item.metadata == {"invalid_reason": "PAGE_NOT_FOUND"}
    # Raw provenance is still saved even though it's incomplete.
    assert item.raw_html == NOT_FOUND_HTML


def test_fetch_retries_exactly_once():
    page = FakePage(
        html_by_url={},
        content_sequence_by_url={PITCH_URL: [NOT_FOUND_HTML, NOT_FOUND_HTML, NOT_FOUND_HTML]},
    )
    yellowbrick.fetch(page, DISCOVERED, discovered_at="2026-01-01T00:00:00+00:00")

    assert len(page.reload_calls) == 1
    assert len(page.wait_for_timeout_calls) == 1


def test_fetch_valid_on_first_try_never_waits_or_reloads():
    page = FakePage({PITCH_URL: REAL_PITCH_HTML})
    item = yellowbrick.fetch(page, DISCOVERED, discovered_at="2026-01-01T00:00:00+00:00")

    assert item.content_complete is True
    assert page.reload_calls == []
    assert page.wait_for_timeout_calls == []


def test_fetch_recovers_after_settle_wait_alone_without_needing_reload():
    """If the page becomes valid after just the first settle wait (before
    any reload), the reload must never happen at all.
    """
    page = FakePage(
        html_by_url={},
        content_sequence_by_url={PITCH_URL: [NOT_FOUND_HTML, REAL_PITCH_HTML]},
    )
    item = yellowbrick.fetch(page, DISCOVERED, discovered_at="2026-01-01T00:00:00+00:00")

    assert item.content_complete is True
    assert page.reload_calls == []
    assert len(page.wait_for_timeout_calls) == 1


def test_fetch_full_summary_expansion_runs_after_a_recovered_valid_page():
    """Once a retry recovers a valid page, normal full-summary expansion
    must still proceed exactly as before (task requirement).
    """
    page = FakePage(
        html_by_url={},
        content_sequence_by_url={PITCH_URL: [NOT_FOUND_HTML, NOT_FOUND_HTML, REAL_PITCH_WITH_SWITCH_HTML]},
        role_elements=_switches(aria_checked="true", data_state="checked"),
    )
    item = yellowbrick.fetch(page, DISCOVERED, discovered_at="2026-01-01T00:00:00+00:00")

    assert item.content_complete is True
    assert "Full thesis" in item.raw_html


def test_persistent_invalid_page_never_calls_extraction_or_screening_llm(monkeypatch):
    """No LLM call happens anywhere in fetch() itself for a persistently
    invalid page -- fetch() is pure browser/local classification. The
    actual extraction/screening gating (via content_complete ->
    collection_status) is exercised in the CLI-level tests, but this pins
    down that fetch() never calls out to idea_extraction/shadow at all.
    """
    from ideascout import idea_extraction, shadow

    def fail_if_called(*args, **kwargs):
        raise AssertionError("fetch() must never call an extraction/screening LLM")

    monkeypatch.setattr(idea_extraction, "build_client", fail_if_called)
    monkeypatch.setattr(idea_extraction, "extract_idea", fail_if_called)
    monkeypatch.setattr(shadow, "build_client", fail_if_called)
    monkeypatch.setattr(shadow, "screen_idea", fail_if_called)

    page = FakePage(
        html_by_url={},
        content_sequence_by_url={PITCH_URL: [NOT_FOUND_HTML, NOT_FOUND_HTML, NOT_FOUND_HTML]},
    )
    item = yellowbrick.fetch(page, DISCOVERED, discovered_at="2026-01-01T00:00:00+00:00")
    assert item.content_complete is False


# --- content-hash / version-identity regression ---------------------------------


def test_different_external_ids_with_identical_404_content_do_not_collide():
    """Several different external_ids can legitimately share the identical
    404 canonical hash -- content_hash alone is never the version identity
    across sources; (source_name, external_id, content_hash) is. This pins
    the pure-function invariant; the DB-level uniqueness scoping is
    covered by tests/test_cli_collect_source.py and the audit/repair tests.
    """
    discovered_a = base.DiscoveredItem(external_id="144269", canonical_url="https://www.joinyellowbrick.com/sp/144269")
    discovered_b = base.DiscoveredItem(external_id="144270", canonical_url="https://www.joinyellowbrick.com/sp/144270")

    item_a = yellowbrick.parse_pitch_page(
        NOT_FOUND_HTML, discovered_a, discovered_at="2026-01-01T00:00:00+00:00",
        content_complete=False, invalid_reason="PAGE_NOT_FOUND",
    )
    item_b = yellowbrick.parse_pitch_page(
        NOT_FOUND_HTML, discovered_b, discovered_at="2026-01-01T00:00:00+00:00",
        content_complete=False, invalid_reason="PAGE_NOT_FOUND",
    )

    assert item_a.content_hash == item_b.content_hash  # same canonical text -> same hash
    assert item_a.external_id != item_b.external_id  # but distinct identity by external_id
