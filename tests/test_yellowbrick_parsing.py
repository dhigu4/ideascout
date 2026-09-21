"""Unit tests for ideascout/sources/yellowbrick.py's PURE parsing
functions -- saved HTML fixtures only, no Playwright, no network, no live
Yellowbrick site involved anywhere.

Fixtures use Yellowbrick's CONFIRMED real URL shapes (/recent feed,
/sp/<numeric-id> pitch pages, /sp/<numeric-id>/share share links).
FULL_CARD_HTML models a realistic pitch card with every field section 7
asks for: source/author, Added to YB, Pitch date, ticker, sentiment,
company, a summary heading, and three UI action links ("Read full
article", "Share this pitch", "Report error for this pitch") -- the
whole point of the Stage 5.2 fix is that NONE of those three action
phrases must ever become the title.
"""

from __future__ import annotations

from ideascout.sources import base, yellowbrick

FULL_CARD_HTML = """
<article class="pitch-card">
  <h2 class="summary-heading">XYZ Corp: Hidden Value Play</h2>
  <div class="ticker-row">XYZ Corp (XYZ) <span class="sentiment">Bullish</span></div>
  <div class="byline">Source: Jane Doe</div>
  <div class="dates">Added to YB: 2026-09-01 Pitch date: 2026-08-15</div>
  <div class="actions">
    <a href="/sp/143411">Read full article</a>
    <a href="/sp/143411/share">Share this pitch</a>
    <a href="/sp/143411/report">Report error for this pitch</a>
  </div>
</article>
"""

SPARSE_CARD_HTML = """
<article class="pitch-card">
  <a href="/sp/143412">Share this pitch</a>
</article>
"""

TWO_CARD_FEED_HTML = f"""
<html><body>
<div class="feed">
{FULL_CARD_HTML}
<article class="pitch-card">
  <h2>ABC Inc: Spin-off Situation</h2>
  <div class="byline">Source: John Roe</div>
  <a href="/sp/133085">Read full article</a>
</article>
</div>
</body></html>
"""

SHARE_ONLY_HTML = """
<html><body>
<article class="pitch-card">
  <h2>DEF Ltd: Spin-off Special Situation</h2>
  <a href="/sp/124689/share">Share this pitch</a>
</article>
</body></html>
"""

LOGIN_PAGE_HTML = """
<html><body>
<form>
  <input type="email" name="email">
  <input type="password" name="password">
  <button>Log in</button>
</form>
</body></html>
"""

NO_PITCH_LINKS_HTML = """
<html><body>
<a href="/about">About</a>
<a href="/blog/some-article">Blog</a>
<a href="/pricing">Pricing</a>
</body></html>
"""

PITCH_PAGE_HTML = """
<html><head><title>XYZ Corp (XYZ): Hidden Value Play</title></head>
<body>
<nav>Navigation links here, should be excluded</nav>
<article>
  <time datetime="2026-09-05">September 5, 2026</time>
  <h1>XYZ Corp: Hidden Value Play</h1>
  <p>XYZ Corp trades at 5x normalized earnings due to a temporary
  loss-making segment expected to reach breakeven next year.</p>
</article>
<script>var tracking = true;</script>
<footer>Footer content, should be excluded</footer>
</body></html>
"""


def _item(items, external_id):
    return next(item for item in items if item.external_id == external_id)


# --- links found / URL shapes / dedup / limit ---------------------------------


def test_discover_pitches_from_html_finds_sp_links():
    items = yellowbrick.discover_pitches_from_html(TWO_CARD_FEED_HTML, limit=10)
    external_ids = {item.external_id for item in items}
    assert external_ids == {"143411", "133085"}


def test_multiple_sp_links_for_same_id_dedupe_to_one_item():
    """FULL_CARD_HTML has THREE links to /sp/143411 (read/share/report)."""
    items = yellowbrick.discover_pitches_from_html(TWO_CARD_FEED_HTML, limit=10)
    assert sum(1 for item in items if item.external_id == "143411") == 1


def test_share_link_canonicalizes_to_the_plain_sp_url():
    items = yellowbrick.discover_pitches_from_html(SHARE_ONLY_HTML, limit=10)
    assert len(items) == 1
    assert items[0].external_id == "124689"
    assert items[0].canonical_url == "https://www.joinyellowbrick.com/sp/124689"


def test_discover_pitches_from_html_respects_limit():
    items = yellowbrick.discover_pitches_from_html(TWO_CARD_FEED_HTML, limit=1)
    assert len(items) == 1


def test_discover_pitches_from_html_ignores_unrelated_links():
    items = yellowbrick.discover_pitches_from_html(TWO_CARD_FEED_HTML, limit=10)
    urls = {item.canonical_url for item in items}
    assert not any("about" in url or "blog" in url for url in urls)


def test_discover_pitches_from_html_no_links_returns_empty():
    assert yellowbrick.discover_pitches_from_html(NO_PITCH_LINKS_HTML, limit=10) == []


# --- the actual bug: action text must never become the title -----------------


def test_action_text_is_never_used_as_title():
    items = yellowbrick.discover_pitches_from_html(TWO_CARD_FEED_HTML, limit=10)
    xyz = _item(items, "143411")
    assert xyz.title != "Share this pitch"
    assert xyz.title != "Read full article"
    assert xyz.title != "Report error for this pitch"


def test_action_links_ignored_for_title_selection_leaving_heading_to_win():
    items = yellowbrick.discover_pitches_from_html(TWO_CARD_FEED_HTML, limit=10)
    xyz = _item(items, "143411")
    assert xyz.title == "XYZ Corp: Hidden Value Play"


def test_bare_action_link_with_no_other_signal_leaves_title_null():
    """SPARSE_CARD_HTML's only text at all is "Share this pitch" -- since
    that's explicitly rejected and nothing else is present, title must be
    None, never substituted with the action text just to fill the field.
    """
    items = yellowbrick.discover_pitches_from_html(SPARSE_CARD_HTML, limit=10)
    assert len(items) == 1
    assert items[0].title is None


def test_is_action_text_matches_all_six_known_phrases_case_insensitively():
    phrases = [
        "Share this pitch",
        "READ FULL ARTICLE",
        "Show Full Summary",
        "report error for this pitch",
        "See All Company Data",
        "Upgrade to Yellowbrick Premium",
    ]
    for phrase in phrases:
        assert yellowbrick._is_action_text(phrase) is True
        assert yellowbrick._is_action_text(phrase + ".") is True  # trailing punctuation tolerated
    assert yellowbrick._is_action_text("XYZ Corp: Hidden Value Play") is False
    assert yellowbrick._is_action_text(None) is False


# --- opportunistic card metadata: ticker / company / author / dates ----------


def test_ticker_and_company_extracted_when_clearly_present():
    items = yellowbrick.discover_pitches_from_html(TWO_CARD_FEED_HTML, limit=10)
    xyz = _item(items, "143411")
    assert xyz.ticker == "XYZ"
    assert xyz.company == "XYZ Corp"


def test_source_author_extracted_when_clearly_present():
    items = yellowbrick.discover_pitches_from_html(TWO_CARD_FEED_HTML, limit=10)
    xyz = _item(items, "143411")
    assert xyz.author == "Jane Doe"


def test_dates_extracted_conservatively_and_distinctly():
    items = yellowbrick.discover_pitches_from_html(TWO_CARD_FEED_HTML, limit=10)
    xyz = _item(items, "143411")
    assert xyz.added_at == "2026-09-01"
    assert xyz.published_at == "2026-08-15"  # "Pitch date"


def test_sentiment_text_does_not_get_mistaken_for_ticker_or_company():
    """"Bullish" sits right next to the ticker in the card -- it must
    never leak into ticker/company.
    """
    items = yellowbrick.discover_pitches_from_html(TWO_CARD_FEED_HTML, limit=10)
    xyz = _item(items, "143411")
    assert xyz.ticker == "XYZ"
    assert "Bullish" not in (xyz.company or "")
    assert xyz.ticker != "Bullish"


def test_summary_heading_becomes_source_title_even_with_ticker_and_company_present():
    """Priority A (heading) beats priority B (TICKER -- Company) even
    when both are available.
    """
    items = yellowbrick.discover_pitches_from_html(TWO_CARD_FEED_HTML, limit=10)
    xyz = _item(items, "143411")
    assert xyz.title == "XYZ Corp: Hidden Value Play"


def test_title_falls_back_to_ticker_and_company_without_a_heading():
    no_heading_html = """
    <article class="pitch-card">
      <div class="ticker-row">Acme Corp (ACME)</div>
      <a href="/sp/555555">Read full article</a>
    </article>
    """
    items = yellowbrick.discover_pitches_from_html(no_heading_html, limit=10)
    assert items[0].title == "ACME — Acme Corp"


def test_incomplete_card_still_yields_valid_item_with_only_identity_fields():
    items = yellowbrick.discover_pitches_from_html(SPARSE_CARD_HTML, limit=10)
    assert len(items) == 1
    item = items[0]
    assert item.external_id == "143412"
    assert item.canonical_url == "https://www.joinyellowbrick.com/sp/143412"
    assert item.title is None
    assert item.ticker is None
    assert item.company is None
    assert item.author is None
    assert item.added_at is None
    assert item.published_at is None


def test_discover_does_not_require_data_attributes():
    """The old provisional data-ticker/data-company/data-published
    attributes never existed on the real site -- discovery must succeed
    purely from href + surrounding card text, with metadata simply absent
    when there's nothing reliable to find.
    """
    bare_html = '<html><body><a href="/sp/999999">Some Pitch Title</a></body></html>'
    items = yellowbrick.discover_pitches_from_html(bare_html, limit=10)
    assert len(items) == 1
    assert items[0].external_id == "999999"
    assert items[0].company is None


# --- neighboring cards must never leak into each other ------------------------


def test_neighboring_card_text_cannot_leak_into_another_pitchs_metadata():
    items = yellowbrick.discover_pitches_from_html(TWO_CARD_FEED_HTML, limit=10)
    xyz = _item(items, "143411")
    abc = _item(items, "133085")

    assert xyz.ticker == "XYZ"
    assert xyz.company == "XYZ Corp"
    assert xyz.author == "Jane Doe"

    assert abc.title == "ABC Inc: Spin-off Situation"
    assert abc.author == "John Roe"
    assert abc.ticker is None  # ABC's card has no ticker/parenthetical at all
    assert abc.company is None
    assert abc.added_at is None
    assert abc.published_at is None

    # Nothing from XYZ's card bled into ABC's, or vice versa.
    assert "Jane Doe" not in (abc.author or "")
    assert "XYZ" != abc.ticker
    assert "John Roe" not in (xyz.author or "")


# --- live-page glue helpers (pure) ---------------------------------------------


def test_html_to_text_excludes_script_style_nav_footer():
    text = yellowbrick.html_to_text(PITCH_PAGE_HTML)
    assert "loss-making segment" in text
    assert "Navigation links" not in text
    assert "Footer content" not in text
    assert "tracking" not in text


def test_extract_title_reads_title_tag():
    assert yellowbrick.extract_title(PITCH_PAGE_HTML) == "XYZ Corp (XYZ): Hidden Value Play"


def test_extract_title_missing_returns_none():
    assert yellowbrick.extract_title("<html><body>no title tag</body></html>") is None


def test_extract_published_at_reads_time_tag_datetime():
    assert yellowbrick.extract_published_at(PITCH_PAGE_HTML) == "2026-09-05"


def test_extract_published_at_missing_returns_none():
    assert yellowbrick.extract_published_at("<html><body>no time tag</body></html>") is None


def test_looks_logged_out_true_for_login_page():
    assert yellowbrick._looks_logged_out(LOGIN_PAGE_HTML) is True


def test_looks_logged_out_false_for_feed_page():
    assert yellowbrick._looks_logged_out(TWO_CARD_FEED_HTML) is False


def test_generic_login_link_alone_is_not_treated_as_logged_out():
    """Section 7 (previous fix): a public page with a plain "Login" link
    (no password field, no login-URL redirect) must not be mistaken for a
    blocked page.
    """
    page_with_login_link_html = TWO_CARD_FEED_HTML.replace("</div>", '<a href="/login">Login</a></div>', 1)
    assert yellowbrick._looks_logged_out(page_with_login_link_html) is False


def test_zero_result_diagnostic_is_safe_and_capped():
    class FakePageForDiagnostic:
        url = "https://www.joinyellowbrick.com/recent"

    hrefs_html = "".join(f'<a href="/x/{i}?token=SECRET{i}">link {i}</a>' for i in range(30))
    diagnostic = yellowbrick._build_zero_result_diagnostic(FakePageForDiagnostic(), hrefs_html)

    assert "Yellowbrick discovery diagnostic:" in diagnostic
    assert "Current URL:" in diagnostic
    assert "Anchors containing /sp/: 0" in diagnostic
    assert "Total anchors: 30" in diagnostic
    assert diagnostic.count("/x/") <= yellowbrick.MAX_DIAGNOSTIC_HREFS
    assert "SECRET" not in diagnostic
    assert "token=" not in diagnostic
    for forbidden in ("cookie", "session", "authorization", "bearer", "<html>"):
        assert forbidden not in diagnostic.lower()


# --- dry-run formatting ---------------------------------------------------------


def test_format_dry_run_full_card_shows_ticker_company_and_dates():
    items = yellowbrick.discover_pitches_from_html(TWO_CARD_FEED_HTML, limit=10)
    xyz = _item(items, "143411")
    output = yellowbrick.format_discovered_item_for_dry_run(xyz)

    assert output.startswith("143411 | XYZ | XYZ Corp")
    assert "Source: Jane Doe" in output
    assert "Added: 2026-09-01" in output
    assert "Pitch date: 2026-08-15" in output
    assert "URL: https://www.joinyellowbrick.com/sp/143411" in output


def test_format_dry_run_incomplete_card_shows_metadata_unavailable():
    items = yellowbrick.discover_pitches_from_html(SPARSE_CARD_HTML, limit=10)
    output = yellowbrick.format_discovered_item_for_dry_run(items[0])

    assert output == (
        "143412 | [metadata unavailable]\n"
        "  URL: https://www.joinyellowbrick.com/sp/143412"
    )


def test_format_dry_run_never_prints_unknown_fields():
    item = base.DiscoveredItem(
        external_id="1", canonical_url="https://www.joinyellowbrick.com/sp/1", ticker="XYZ",
    )
    output = yellowbrick.format_discovered_item_for_dry_run(item)
    assert "Source:" not in output
    assert "Added:" not in output
    assert "Pitch date:" not in output


# --- parse_pitch_page (fetch stage -- untouched by this fix, still covered) ---


def test_parse_pitch_page_produces_source_item_with_content_hash():
    discovered = base.DiscoveredItem(
        external_id="139483", canonical_url="https://www.joinyellowbrick.com/sp/139483",
        title="Discovery-time title", ticker="XYZ", company=None, published_at="2026-09-01",
    )
    item = yellowbrick.parse_pitch_page(PITCH_PAGE_HTML, discovered, discovered_at="2026-09-10T00:00:00+00:00")

    assert item.source_name == "yellowbrick"
    assert item.external_id == "139483"
    assert item.source_type == "stock_pitch"
    assert "loss-making segment" in item.raw_text
    assert item.raw_html == PITCH_PAGE_HTML
    assert item.content_hash  # non-empty
    assert item.title == "XYZ Corp (XYZ): Hidden Value Play"
    assert item.published_at == "2026-09-05"
    assert item.ticker == "XYZ"


def test_parse_pitch_page_falls_back_to_discovered_title_and_date_when_page_lacks_them():
    discovered = base.DiscoveredItem(
        external_id="133085", canonical_url="https://www.joinyellowbrick.com/sp/133085",
        title="Discovery-time title", published_at="2026-08-01",
    )
    bare_html = "<html><body><p>Some pitch content with no title or time tag at all here.</p></body></html>"
    item = yellowbrick.parse_pitch_page(bare_html, discovered, discovered_at="2026-09-10T00:00:00+00:00")

    assert item.title == "Discovery-time title"
    assert item.published_at == "2026-08-01"


def test_content_hash_changes_when_html_changes():
    discovered = base.DiscoveredItem(external_id="139483", canonical_url="https://x/sp/139483")
    item1 = yellowbrick.parse_pitch_page(PITCH_PAGE_HTML, discovered, discovered_at="2026-01-01T00:00:00+00:00")
    item2 = yellowbrick.parse_pitch_page(PITCH_PAGE_HTML + "<p>edited</p>", discovered, discovered_at="2026-01-01T00:00:00+00:00")
    assert item1.content_hash != item2.content_hash


def test_content_hash_stable_for_identical_html():
    discovered = base.DiscoveredItem(external_id="139483", canonical_url="https://x/sp/139483")
    item1 = yellowbrick.parse_pitch_page(PITCH_PAGE_HTML, discovered, discovered_at="2026-01-01T00:00:00+00:00")
    item2 = yellowbrick.parse_pitch_page(PITCH_PAGE_HTML, discovered, discovered_at="2026-01-02T00:00:00+00:00")
    assert item1.content_hash == item2.content_hash
