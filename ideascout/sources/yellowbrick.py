"""Yellowbrick source adapter -- the first authenticated website-source
adapter for IdeaScout.

Discovery and fetching are kept deliberately separate (per Stage 5 spec
section C): discover() parses the already-authenticated feed page's HTML
into cheap DiscoveredItem metadata with no LLM call and no page load
beyond the feed itself; fetch() opens exactly one pitch's own page and
captures its full, authoritative content -- Brad's own subscription
entitles him to see this, and nothing here bypasses or scrapes beyond
what his authenticated session can already reach.

The website source is authoritative source material (unlike an email that
mixes Brad's commentary with a forwarded writeup), so there is no
source-isolation problem here -- fetch()'s output text goes straight to
idea_extraction.extract_idea() unmodified.

CONFIRMED MARKUP (see Stage 5.1 fix): the recent-pitches feed lives at
/recent (not /pitches, which was an unverified guess), and pitch pages are
at /sp/<numeric-id>, sometimes shared as /sp/<numeric-id>/share -- both
canonicalize to the same https://www.joinyellowbrick.com/sp/<id> URL and
the same external_id. There is no evidence Yellowbrick renders data-ticker/
data-company/data-published attributes anywhere -- discovery no longer
depends on them at all.

CARD-METADATA FIX (Stage 5.2): a real dry run showed "Share this pitch" --
a generic action link at the bottom of every card -- being used as the
pitch title, because the old approach looked only at the /sp/ link's own
immediately-enclosing <a> text. Discovery now finds each link's NEAREST
REASONABLE ENCLOSING CARD (see _PitchCardParser/_is_card_boundary below)
and reads metadata from that card's own text only -- never from a
neighboring card, and never from a fixed-radius character window that
could spill across card boundaries. Known Yellowbrick UI action/nav
phrases (_ACTION_TEXT_PHRASES) are explicitly rejected as titles. Company/
ticker/author/dates remain opportunistic and are commonly None -- "no
reliable metadata" is always preferred over misleading metadata.

VERSION-IDENTITY FIX (Stage 5.4): a real immediate refetch of the SAME
unchanged pitch was wrongly treated as a changed version. The cause was
content_hash being computed over the FULL raw page HTML -- scripts,
hydration JSON, session state, and other per-request-volatile markup mean
two fetches of an identical pitch essentially never produce byte-identical
HTML. content_hash is now computed over build_canonical_source_text()'s
output -- a normalized, substantive-only rendering (title/thesis/body
text; scripts/styles/nav/footer/header and known UI action phrases
stripped) -- which is what actually determines whether a document has
materially changed. The exact raw HTML hash is kept separately as
raw_capture_hash (see base.SourceItem) for provenance only; it is expected
to differ between identical-content fetches and must never by itself
create a new version.

FULL-CAPTURE FIX (Stage 5.7): a real production capture of a pitch's
canonical text (1,035 chars) contained only the header/metadata/one-
paragraph summary and the literal line "Show full summary:" -- the label
of a collapsed-content control, not expanded pitch content. fetch() never
interacted with the page at all beyond loading it, so if Yellowbrick
renders the long-form thesis behind a click-to-expand control (the direct
implication of that control's own label surviving into the capture),
nothing ever obtained it. fetch() now checks for that control
(_looks_collapsed) before finalizing capture and, if present, clicks it
(_expand_full_summary_if_present, located by visible text -- never a
brittle CSS class) and gives the page a short, bounded moment to render
the expanded body before re-capturing. Both the click and the wait are
best-effort: if the control still shows as collapsed afterward --
regardless of why -- the resulting SourceItem is marked
content_complete=False. Extraction/screening must never run against an
incomplete capture (see cli.py's collect-source, which checks this on
collection_status before ever queuing extraction); raw HTML is still
saved as provenance either way.

LIVE DIAGNOSTIC (Stage 5.9): a real production run of the Stage 5.7 fix
against pitch 143618 still produced INCOMPLETE_CONTENT -- raw HTML grew
(~46,272 -> ~49,109 chars, so SOMETHING on the page changed after the
click), but canonical substantive text stayed ~1,016 chars and identical
across versions. This means the click-to-expand fix's assumption (that
clicking the visible "Show full summary" control renders the long-form
thesis inline) does not hold against the real page, for reasons not yet
known: multiple matching elements where the wrong one was targeted, the
control requiring a different interaction, expansion landing in a modal/
popup/new page instead of inline, content arriving via a network request
this code never waited for, or the long-form thesis never being present
in this page's DOM/network responses at all. Rather than guess again,
diagnose_live() (and `python run.py diagnose-source-live` in cli.py)
gathers direct DOM/network/gating evidence from the REAL logged-in page --
read-only, no DB writes, no raw artifact writes, no LLM calls, and
completely separate from fetch()/parse_pitch_page()/collect-source, which
this stage does NOT modify. See functions below this module's "LIVE
DIAGNOSTIC" section for what each piece of evidence covers.

LIVE DIAGNOSTIC EVIDENCE (Stage 5.10): Brad ran the Stage 5.9 diagnostic
against the real, logged-in page for pitch 143618. It found exactly ONE
visible+enabled "Show full summary" text match -- a plain
`<span class="mr-2 text-xs">Show full summary:</span>` with no role, no
href, no aria-expanded/aria-controls. Clicking that span directly produced
NO observable effect whatsoever (no URL change, no body/canonical text
change, no request, no popup, no gating, no aria change). This is direct
evidence that the span is only a text LABEL, not the actual interactive
control -- some ANCESTOR or SIBLING element is almost certainly the real
click target. diagnose_live() now inspects up to 6 ancestor levels and
both immediate siblings of every such span (_inspect_dom_context) and
only clicks a resolved candidate (_resolve_actionable_candidates) when
exactly one actionable element is found -- never the span itself, and
never a guess when more than one candidate is equally plausible. There
are also 11 Next.js `__next_f.push` hydration blocks on the real page;
current evidence does not establish whether they contain the long-form
thesis, so detect_hidden_data_blocks now also checks (shape/presence
only, never content) for a small set of thesis-specific phrases Brad
supplied from the real pitch. This stage did not yet change fetch()/
parse_pitch_page()/collect-source -- see the Stage 5.11 fix below.

PRODUCTION FIX (Stage 5.11): a follow-up live-diagnostic run against
pitch 143618 made the real DOM unambiguous. The "Show full summary:"
text is a plain `<span>` label; the actual control is its next sibling,
a `<button id="full-summary-switch" type="button" role="switch"
aria-checked="false" data-state="unchecked">`. Clicking that BUTTON (not
the label) produced a real, observable expansion (canonical text
1,016 -> 1,812 chars) with no request/popup/URL-change/gating -- but the
"Show full summary:" label REMAINED visible afterward, proving the old
Stage 5.7 completeness guard (based on that label's presence) was
fundamentally invalid. fetch() now identifies and clicks the switch via
its semantic role="switch" (id="full-summary-switch" and "follows the
label" are used only as supporting disambiguation signals -- see
_select_full_summary_switch), waits for its own aria-checked/data-state
to flip to checked (never a fixed sleep, never a character-count
threshold -- see _wait_for_switch_checked), and derives content_complete
SOLELY from that switch's own state (_content_is_complete) -- never from
whether the label text is still present. _looks_collapsed and the old
label-click _expand_full_summary_if_present (Stage 5.7) have been
replaced entirely by this switch-based logic, since the label-based
approach is now proven wrong, not merely superseded.
"""

from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from . import base

SOURCE_NAME = "yellowbrick"
SOURCE_TYPE = "stock_pitch"

HOME_URL = "https://www.joinyellowbrick.com/"
# Confirmed real feed path (was the unverified guess /pitches).
FEED_URL = "https://www.joinyellowbrick.com/recent"

# Confirmed real pitch-page shape: /sp/<numeric-id>, optionally followed
# by /share (a shareable-link variant of the exact same pitch). Both forms
# capture the same numeric external_id and canonicalize to the same URL.
PITCH_LINK_PATH_PATTERN = re.compile(r"/sp/(\d+)(?:/share)?\b")

# HTML5 void elements never get a matching end tag -- a naive open/close
# stack would desync on the very first <img>/<br>/<input>/etc. otherwise.
_VOID_ELEMENTS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})

# Generic Yellowbrick UI action/navigation text, confirmed from a real dry
# run where "Share this pitch" was wrongly used as a pitch title. Matched
# case-insensitively (see _is_action_text) -- NEVER used as a title, no
# matter where it's found.
_ACTION_TEXT_PHRASES = frozenset(
    {
        "share this pitch",
        "read full article",
        "show full summary",
        "report error for this pitch",
        "see all company data",
        "upgrade to yellowbrick premium",
    }
)

# Higher-confidence, narrow ticker conventions only -- e.g. "XYZ Corp
# (XYZ)" or "$XYZ" -- never a bare-uppercase-word guess, which would false-
# -positive constantly on ordinary capitalized text. The combined pattern
# captures company + ticker together when the common "Company (TICKER)"
# convention is used; the standalone ones are a ticker-only fallback.
_TICKER_WITH_COMPANY_PATTERN = re.compile(r"([A-Z][A-Za-z0-9&.,'’\- ]{1,58}?)\s*\(([A-Z]{1,5})\)")
_TICKER_PAREN_PATTERN = re.compile(r"\(([A-Z]{1,5})\)")
_TICKER_CASHTAG_PATTERN = re.compile(r"\$([A-Z]{1,5})\b")

# Conservative, label-anchored extraction only -- never a bare guess at
# free text. Each capture is additionally trimmed at the next recognized
# label word (_LABEL_STOPWORDS) so one field's value can't run on into the
# next field's label when a card's text has no clear separator between them.
_AUTHOR_LABEL_PATTERN = re.compile(r"(?:Source|Author|By)\s*[:\-]?\s*([A-Za-z0-9][\w .,&'\-]{1,60})", re.IGNORECASE)
_ADDED_LABEL_PATTERN = re.compile(r"Added to YB\s*[:\-]?\s*([A-Za-z0-9][\w .,/\-]{1,20})", re.IGNORECASE)
_PITCH_DATE_LABEL_PATTERN = re.compile(r"Pitch date\s*[:\-]?\s*([A-Za-z0-9][\w .,/\-]{1,20})", re.IGNORECASE)
_LABEL_STOPWORDS = frozenset(
    {"added", "pitch", "ticker", "company", "source", "author", "sentiment", "share", "read", "report", "see", "upgrade", "date"}
)

# A short, bounded wait for pitch links to render after DOMContentLoaded
# (the feed may be client-rendered) -- never an arbitrary sleep, never a
# retry loop, never scrolling.
PITCH_LINK_WAIT_MS = 5000

# The exact, literal label TEXT of Yellowbrick's full-summary control
# (confirmed from a real production capture -- see module docstring,
# Stage 5.7 fix). Real evidence (Stage 5.10) proved this label is only a
# static text node, NEVER itself the clickable element -- see
# FULL_SUMMARY_SWITCH_ROLE/_select_full_summary_switch below for the
# actual production expansion target (Stage 5.11).
SHOW_FULL_SUMMARY_TEXT = "Show full summary"

# The REAL clickable control (Stage 5.11 fix, from a real live-diagnostic
# run against pitch 143618): a `role="switch"` element immediately
# following the label span. id="full-summary-switch" is a real, observed
# id but used only as a SUPPORTING disambiguation signal (task section 1)
# -- never the sole selector -- since a differently-built page could omit
# or change it while keeping the same semantic role.
FULL_SUMMARY_SWITCH_ROLE = "switch"
FULL_SUMMARY_SWITCH_ID = "full-summary-switch"

# Bounded: a click timeout (the switch must already be present/visible/
# enabled; this is not a retry loop).
FULL_SUMMARY_SWITCH_CLICK_TIMEOUT_MS = 3000
# Bounded POLLING wait (task section 3) for the switch's own aria-checked/
# data-state to actually flip to checked -- NOT a fixed sleep, and NOT
# gated on any character-count threshold. Checks every POLL_INTERVAL_MS,
# returning as soon as the switch reports checked, but never waits past
# MAX_WAIT_MS, so this can never hang indefinitely.
FULL_SUMMARY_SWITCH_POLL_INTERVAL_MS = 300
FULL_SUMMARY_SWITCH_MAX_WAIT_MS = 3000

# Safe zero-result diagnostic: never dump full HTML, cookies, storage,
# tokens, or headers -- only counts and href PATHS (query strings/domains
# stripped), capped at this many samples.
MAX_DIAGNOSTIC_HREFS = 20

# Be polite to the site: a short pause between fetching individual pitch
# pages during a real (non-dry-run) collection run. Tests monkeypatch this
# to 0 so the suite doesn't actually sleep.
POLITE_DELAY_SECONDS = 1.0

_HREF_PATTERN = re.compile(r'href="([^"]*)"', re.IGNORECASE)

# Collapses meaningless repeated spaces/tabs (never newlines -- those are
# handled separately) within one line of canonical source text.
_WHITESPACE_RUN_PATTERN = re.compile(r"[ \t]+")


class _TextExtractor(HTMLParser):
    """Best-effort HTML-to-plain-text: concatenates visible text nodes,
    skipping script/style/nav/footer/header content. Good enough for
    handing a pitch page's body to the extraction LLM -- not a general-
    purpose readability algorithm.
    """

    _SKIP_TAGS = {"script", "style", "nav", "footer", "header"}

    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            stripped = data.strip()
            if stripped:
                self.parts.append(stripped)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return "\n".join(parser.parts)


_TITLE_TAG_PATTERN = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TIME_TAG_PATTERN = re.compile(r'<time[^>]*\bdatetime="([^"]+)"', re.IGNORECASE)
_LOGIN_PASSWORD_FIELD_PATTERN = re.compile(r'type="password"', re.IGNORECASE)


def extract_title(html: str) -> str | None:
    match = _TITLE_TAG_PATTERN.search(html)
    if not match:
        return None
    return html_to_text(match.group(1)).strip() or None


def extract_published_at(html: str) -> str | None:
    match = _TIME_TAG_PATTERN.search(html)
    return match.group(1) if match else None


def _looks_logged_out(html: str) -> bool:
    """Heuristic: a page with a password field and no recognizable pitch
    link looks like a login page, not the authenticated feed/pitch. A
    genuine feed/pitch page won't have both of those at once even if it
    happens to embed some unrelated password field.
    """
    return bool(_LOGIN_PASSWORD_FIELD_PATTERN.search(html)) and not PITCH_LINK_PATH_PATTERN.search(html)


def _absolute_url(url: str) -> str:
    return urljoin(HOME_URL, url)


def _is_action_text(text: str | None) -> bool:
    """True for known Yellowbrick UI action/navigation phrases (e.g.
    "Share this pitch") -- these must NEVER be used as a title, no matter
    where in a card they're found. Case-insensitive; trailing punctuation
    ignored (including a trailing colon -- a real production capture
    showed "Show full summary:" surviving into canonical text because the
    colon wasn't stripped here, so this line was never recognized as
    action text).
    """
    if not text:
        return False
    return text.strip().lower().rstrip(".!…:") in _ACTION_TEXT_PHRASES


def _is_card_boundary(tag: str, attrs: dict) -> bool:
    """A "reasonable enclosing card/container" for one pitch link -- not
    tied to any specific (often auto-generated, brittle) CSS class name.
    Semantic list/item/row tags always qualify; any other tag qualifies
    only if its class name contains "card". Deliberately NOT "has any
    class/id at all" -- a real card's inner sub-sections (a byline div,
    an actions div, a dates div, ...) almost always have their OWN class
    names too, and would otherwise be mistaken for the card itself,
    capturing only one small slice of it instead of the whole card.
    """
    if tag in ("article", "li", "tr"):
        return True
    class_attr = (attrs.get("class") or "").lower()
    return "card" in class_attr


class _PitchCardParser(HTMLParser):
    """Single forward pass that finds every /sp/<id> link and, for each,
    the text of its NEAREST reasonable enclosing card (_is_card_boundary)
    -- never a fixed-radius character window, and never text belonging to
    a sibling/neighboring card. Multiple links for the same id (e.g. a
    title link and a "Share this pitch" action link inside the same card)
    resolve to the SAME card, because "nearest enclosing card" for either
    occurrence is the same shared ancestor; whichever qualifying ancestor
    frame closes FIRST (i.e. the truly nearest one, since HTML closing
    tags fire innermost-first) wins and is never overwritten by an outer
    ancestor afterward -- "best enclosing card, not necessarily the first
    anchor."

    Tolerant of void elements (img/br/input/...) -- a naive stack would
    otherwise desync on the very first one -- and of malformed/mismatched
    closing tags (best-effort recovery instead of crashing).
    """

    def __init__(self):
        super().__init__()
        self._stack: list[dict] = []
        self._heading_depth = 0
        self.order: list[str] = []
        self.href_by_id: dict[str, str] = {}
        self.card_text_by_id: dict[str, str] = {}
        self.card_heading_by_id: dict[str, str] = {}

    def _push_frame(self, tag, attrs):
        # A "\n" boundary marker in every currently-open ancestor's text --
        # NOT just a space -- so two sibling elements' text (e.g. a <h2>
        # heading immediately followed by a sibling metadata <div>) can
        # never blend into what looks like one continuous run of words to
        # a regex. "\n" is deliberately outside every extraction pattern's
        # character class, so it always acts as a hard stop.
        for frame in self._stack:
            frame["text_parts"].append("\n")
            if self._heading_depth > 0:
                frame["heading_parts"].append("\n")
        self._stack.append({"tag": tag, "attrs": attrs, "text_parts": [], "heading_parts": [], "pending_ids": []})

    def _pop_frame(self):
        frame = self._stack.pop()
        if not frame["pending_ids"]:
            return
        if _is_card_boundary(frame["tag"], frame["attrs"]):
            card_text = " ".join(frame["text_parts"]).strip()
            card_heading = " ".join(frame["heading_parts"]).strip()
            for pid in frame["pending_ids"]:
                if pid not in self.card_text_by_id:
                    self.card_text_by_id[pid] = card_text
                    if card_heading:
                        self.card_heading_by_id[pid] = card_heading
        elif self._stack:
            self._stack[-1]["pending_ids"].extend(
                pid for pid in frame["pending_ids"] if pid not in self.card_text_by_id
            )

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)

        if tag in _HEADING_TAGS:
            self._heading_depth += 1

        if tag == "a":
            href = attrs_dict.get("href") or ""
            match = PITCH_LINK_PATH_PATTERN.search(href)
            if match:
                external_id = match.group(1)
                if external_id not in self.href_by_id:
                    self.href_by_id[external_id] = href
                    self.order.append(external_id)
                for frame in self._stack:
                    frame["pending_ids"].append(external_id)

        if tag not in _VOID_ELEMENTS:
            self._push_frame(tag, attrs_dict)

    def handle_startendtag(self, tag, attrs):
        # Explicit self-closing form (e.g. <img/>) -- no end tag will
        # ever fire for it, exactly like a void element.
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        stripped = data.strip()
        if not stripped:
            return
        for frame in self._stack:
            frame["text_parts"].append(stripped)
            if self._heading_depth > 0:
                frame["heading_parts"].append(stripped)

    def handle_endtag(self, tag):
        if tag in _HEADING_TAGS and self._heading_depth > 0:
            self._heading_depth -= 1

        if tag in _VOID_ELEMENTS or not self._stack:
            return

        if self._stack[-1]["tag"] == tag:
            self._pop_frame()
        else:
            # Malformed/mismatched HTML -- unwind up to (and including)
            # the nearest matching open frame rather than desyncing.
            for i in range(len(self._stack) - 1, -1, -1):
                if self._stack[i]["tag"] == tag:
                    while len(self._stack) > i:
                        self._pop_frame()
                    break


def _trim_at_next_label(raw: str, *, max_words: int = 6) -> str | None:
    """Stops a label-anchored capture (author/date) at the next word that
    looks like another field's label, so one field's value can't run on
    into the next field's label when a card has no clear separator
    between them (e.g. "By Jane Doe Added to YB Jan 5" should yield
    author "Jane Doe", not "Jane Doe Added to YB Jan 5").
    """
    kept = []
    for word in raw.split()[:max_words]:
        if word.strip(".,'-:").lower() in _LABEL_STOPWORDS:
            break
        kept.append(word)
    result = " ".join(kept).strip(" .,-")
    return result or None


def _extract_ticker_and_company(card_text: str) -> tuple[str | None, str | None]:
    combo = _TICKER_WITH_COMPANY_PATTERN.search(card_text)
    if combo:
        return combo.group(2), combo.group(1).strip()
    ticker_match = _TICKER_CASHTAG_PATTERN.search(card_text) or _TICKER_PAREN_PATTERN.search(card_text)
    return (ticker_match.group(1) if ticker_match else None), None


def _extract_author(card_text: str) -> str | None:
    match = _AUTHOR_LABEL_PATTERN.search(card_text)
    return _trim_at_next_label(match.group(1)) if match else None


def _extract_dates(card_text: str) -> tuple[str | None, str | None]:
    """Returns (added_at, pitch_date) -- Yellowbrick's two distinct dates
    ("Added to YB" vs "Pitch date"), each only when clearly labeled.
    """
    added_match = _ADDED_LABEL_PATTERN.search(card_text)
    pitch_match = _PITCH_DATE_LABEL_PATTERN.search(card_text)
    added_at = _trim_at_next_label(added_match.group(1)) if added_match else None
    pitch_date = _trim_at_next_label(pitch_match.group(1)) if pitch_match else None
    return added_at, pitch_date


def _select_source_title(card_heading: str | None, ticker: str | None, company: str | None) -> str | None:
    """Priority order (spec section 3) -- a closed list ending in "leave
    null": a full-summary heading beats a reconstructed "TICKER --
    Company" beats company alone beats ticker alone beats nothing.
    Deliberately does NOT fall back to raw anchor text: that is exactly
    the unreliable signal that produced "Share this pitch" as a title.
    """
    if card_heading and not _is_action_text(card_heading):
        return card_heading
    if ticker and company:
        return f"{ticker} — {company}"
    if company:
        return company
    if ticker:
        return ticker
    return None


def discover_pitches_from_html(html: str, *, limit: int) -> list[base.DiscoveredItem]:
    """Pure parsing, no network/browser involved. Finds every /sp/<id>
    (and /sp/<id>/share) link, resolves each to its nearest enclosing
    card's own text (see _PitchCardParser), and derives title/ticker/
    company/author/dates from THAT card only -- never from a neighboring
    card. Deduplicates by numeric id (one pitch id = one DiscoveredItem),
    in first-seen order. A link is discovered on its href alone;
    everything else is optional, opportunistic metadata that is commonly
    None and never blocks discovery.
    """
    parser = _PitchCardParser()
    parser.feed(html)

    items: list[base.DiscoveredItem] = []
    for external_id in parser.order:
        card_text = parser.card_text_by_id.get(external_id, "")
        card_heading = parser.card_heading_by_id.get(external_id)

        ticker, company = _extract_ticker_and_company(card_text)
        source_title = _select_source_title(card_heading, ticker, company)
        author = _extract_author(card_text)
        added_at, pitch_date = _extract_dates(card_text)

        items.append(
            base.DiscoveredItem(
                external_id=external_id,
                canonical_url=_absolute_url(f"/sp/{external_id}"),
                title=source_title,
                author=author,
                published_at=pitch_date,
                added_at=added_at,
                ticker=ticker,
                company=company,
            )
        )
        if len(items) >= limit:
            break
    return items


def format_discovered_item_for_dry_run(item: base.DiscoveredItem) -> str:
    """Human-auditable dry-run line(s) (spec section 5). Prints only
    fields actually known. If neither ticker, company, nor a reliable
    title could be determined, prints "[metadata unavailable]" instead of
    a blank or misleading label -- external_id and canonical_url are the
    only two fields a valid discovery requires, and both are always shown.
    """
    header_parts = [item.external_id]
    if item.ticker:
        header_parts.append(item.ticker)
    if item.company:
        header_parts.append(item.company)
    if len(header_parts) == 1:
        header_parts.append(item.title or "[metadata unavailable]")

    lines = [" | ".join(header_parts)]
    if item.author:
        lines.append(f"  Source: {item.author}")
    if item.added_at:
        lines.append(f"  Added: {item.added_at}")
    if item.published_at:
        lines.append(f"  Pitch date: {item.published_at}")
    lines.append(f"  URL: {item.canonical_url}")
    return "\n".join(lines)


def _build_zero_result_diagnostic(page, html: str) -> str:
    """Safe to print: current URL, page title, link counts, and up to
    MAX_DIAGNOSTIC_HREFS href PATHS only (query strings/domains stripped).
    Never includes cookies, storage, tokens, headers, full HTML, or
    account/profile information -- there is nothing in this function that
    could even access those.
    """
    all_hrefs = _HREF_PATTERN.findall(html)
    sp_count = sum(1 for href in all_hrefs if "/sp/" in href)

    sample_paths: list[str] = []
    for href in all_hrefs:
        path = urlparse(href).path or href
        if path not in sample_paths:
            sample_paths.append(path)
        if len(sample_paths) >= MAX_DIAGNOSTIC_HREFS:
            break

    lines = [
        "Yellowbrick discovery diagnostic:",
        f"Current URL: {getattr(page, 'url', None) or 'unknown'}",
        f"Page title: {extract_title(html) or 'unknown'}",
        f"Anchors containing /sp/: {sp_count}",
        f"Total anchors: {len(all_hrefs)}",
        "Sample href paths:",
    ]
    lines.extend(f"  {path}" for path in sample_paths)
    return "\n".join(lines)


def build_canonical_source_text(html: str) -> str:
    """Deterministic, normalized rendering of a pitch page's SUBSTANTIVE
    content only -- this, never the raw HTML, is what content_hash is
    computed from (see module docstring, Stage 5.4 fix).

    Starts from html_to_text(), which already excludes script/style/nav/
    footer/header content -- that alone already drops the great majority
    of volatile markup (hydration JSON and tracking scripts live inside
    <script> tags; page chrome lives inside <nav>/<footer>/<header>).
    Additionally drops any line that is exactly a known Yellowbrick UI
    action/navigation phrase (_is_action_text -- "Share this pitch" and
    friends), since those are controls, not pitch content, and their
    presence/order is not substantive.

    Normalization is deliberately minimal and deterministic: normalize
    line endings, collapse runs of meaningless repeated whitespace
    (spaces/tabs) within a line to one space, strip each line, drop blank
    lines, join with a single "\\n", strip the result. Does NOT lowercase
    and does NOT strip numbers/punctuation -- those can matter to the
    thesis.
    """
    text = html_to_text(html)
    lines = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = _WHITESPACE_RUN_PATTERN.sub(" ", raw_line).strip()
        if not line or _is_action_text(line):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


class _SwitchStateParser(HTMLParser):
    """Single forward pass (Stage 5.11) collecting every element with
    role="switch" -- tag, id, aria-checked, data-state, and its position
    in document order -- plus the document positions where the "Show full
    summary" label text itself appears. Together these let
    _select_full_summary_switch resolve the ONE real production switch
    using the exact priority task section 1 asks for: role="switch" as
    the base signal, id="full-summary-switch" as a supporting (never
    sole) disambiguator, and "comes right after the label in document
    order" as a last-resort fallback when still ambiguous.
    """

    def __init__(self):
        super().__init__()
        self._position = 0
        self.switches: list[dict] = []
        self.label_positions: list[int] = []

    def handle_starttag(self, tag, attrs):
        self._position += 1
        attrs_dict = dict(attrs)
        if (attrs_dict.get("role") or "").strip().lower() == FULL_SUMMARY_SWITCH_ROLE:
            self.switches.append(
                {
                    "tag": tag,
                    "id": attrs_dict.get("id"),
                    "aria_checked": attrs_dict.get("aria-checked"),
                    "data_state": attrs_dict.get("data-state"),
                    "position": self._position,
                }
            )

    def handle_data(self, data):
        stripped = data.strip().lower().rstrip(".!…:")
        if stripped == SHOW_FULL_SUMMARY_TEXT.lower():
            self.label_positions.append(self._position)


def _select_full_summary_switch(html: str) -> dict | None:
    """Identifies the ONE full-summary switch on the page, if any (task
    section 1). Returns a descriptor dict with an "index" field (into
    document-order-of-appearance among role="switch" elements) that
    _locate_full_summary_switch uses to click the SAME element live, or
    None when no switch can be confidently identified -- callers then
    treat the page as potentially already complete (task section 2),
    never automatically incomplete just because nothing was found to
    click.
    """
    parser = _SwitchStateParser()
    parser.feed(html)
    switches = parser.switches
    if not switches:
        return None

    for i, switch in enumerate(switches):
        switch["index"] = i

    if len(switches) == 1:
        return switches[0]

    by_id = [s for s in switches if s.get("id") == FULL_SUMMARY_SWITCH_ID]
    if len(by_id) == 1:
        return by_id[0]

    if parser.label_positions:
        label_position = parser.label_positions[0]
        after_label = [s for s in switches if s["position"] > label_position]
        if len(after_label) == 1:
            return after_label[0]

    return None  # still ambiguous -- never guess which switch is the real one


def _switch_is_checked(descriptor: dict) -> bool | None:
    """Task section 2's authoritative state rule: aria-checked and
    data-state are each treated as sufficient on their own (either
    signals True/False the same way); returns None only when NEITHER
    attribute is present/recognized, so callers can distinguish "known
    unchecked" from "genuinely unknown state" (treated as incomplete,
    never silently assumed complete).
    """
    aria_checked = (descriptor.get("aria_checked") or "").strip().lower()
    data_state = (descriptor.get("data_state") or "").strip().lower()
    if aria_checked == "true" or data_state == "checked":
        return True
    if aria_checked == "false" or data_state == "unchecked":
        return False
    return None


def _locate_full_summary_switch(page, descriptor: dict):
    """The live Playwright locator for the SAME switch _select_full_
    summary_switch identified from HTML -- role="switch" is the base
    semantic locator (task section 1's preferred approach); "index"
    (computed from the same document-order/id/label-relation resolution)
    picks out the correct one among however many role="switch" elements
    the live page has.
    """
    return page.get_by_role(FULL_SUMMARY_SWITCH_ROLE).nth(descriptor["index"])


def _wait_for_switch_checked(page, *, html_before: str) -> str:
    """Bounded polling wait (task section 3) for the switch's own aria-
    checked/data-state to flip to checked -- the real DOM state is
    authoritative, never a fixed sleep and never an arbitrary character-
    count threshold. Returns the latest page.content() either way: if the
    switch never reports checked within FULL_SUMMARY_SWITCH_MAX_WAIT_MS,
    the caller's own _content_is_complete check on the returned HTML will
    correctly find it still unchecked.
    """
    html = html_before
    elapsed = 0
    while elapsed < FULL_SUMMARY_SWITCH_MAX_WAIT_MS:
        try:
            page.wait_for_timeout(FULL_SUMMARY_SWITCH_POLL_INTERVAL_MS)
        except Exception:
            pass
        elapsed += FULL_SUMMARY_SWITCH_POLL_INTERVAL_MS
        html = page.content()
        descriptor = _select_full_summary_switch(html)
        if descriptor is not None and _switch_is_checked(descriptor) is True:
            return html
    return html


def _expand_full_summary_if_present(page, html: str) -> str:
    """Locates Yellowbrick's full-summary switch (task section 1) and
    clicks it if present and not already checked, then waits (task
    section 3) for its own state to confirm expansion, returning the
    resulting HTML either way.

    NEVER clicks the "Show full summary" text label itself -- real
    production evidence (Stage 5.10) showed clicking that span directly
    has zero effect; the actual control is the switch. A no-op when no
    switch can be identified at all, and a no-op when the switch is
    already checked (never re-toggles an expanded switch closed).
    Best-effort otherwise: if the switch can't actually be clicked (not
    visible/enabled, or Playwright's own click fails), the ORIGINAL html
    is returned unchanged -- completeness is judged separately, from
    whatever HTML this function returns, by the caller re-checking switch
    state. Never hangs indefinitely.
    """
    descriptor = _select_full_summary_switch(html)
    if descriptor is None:
        return html

    if _switch_is_checked(descriptor) is True:
        return html  # already expanded -- never re-toggle it closed

    try:
        switch_locator = _locate_full_summary_switch(page, descriptor)
        if not switch_locator.is_visible() or not switch_locator.is_enabled():
            return html  # not actually actionable -- caller will find it still unchecked
        switch_locator.click(timeout=FULL_SUMMARY_SWITCH_CLICK_TIMEOUT_MS)
    except Exception:
        return html  # switch wasn't actually clickable -- caller will find it still unchecked

    return _wait_for_switch_checked(page, html_before=html)


def _content_is_complete(html: str) -> bool:
    """Task section 2's authoritative completeness rule. Deliberately
    NEVER based on whether the "Show full summary" label text is still
    present -- real evidence proved that label remains visible in BOTH
    the collapsed and expanded states, so the Stage 5.7 label-presence
    guard was invalid. If a switch exists, its own checked state decides
    completeness (unknown state is treated as incomplete, not silently
    assumed complete). If no switch exists at all, the page is treated as
    potentially already complete -- e.g. a genuinely short pitch with
    nothing to expand -- never automatically incomplete.
    """
    descriptor = _select_full_summary_switch(html)
    if descriptor is None:
        return True
    return _switch_is_checked(descriptor) is True


def parse_pitch_page(
    html: str, discovered: base.DiscoveredItem, *, discovered_at: str, content_complete: bool = True
) -> base.SourceItem:
    """Pure parsing, no network/browser involved. Falls back to whatever
    the (cheaper) discovery step already found when the pitch page itself
    doesn't offer something better -- company/ticker are ultimately
    determined by idea_extraction from raw_text regardless, so these
    fields are for indexing/display, not for screening.

    content_hash (the version-identity hash) is computed from
    build_canonical_source_text()'s output, NOT from raw html -- see
    module docstring. raw_capture_hash records the exact raw bytes
    captured this time, for provenance only; it is expected to differ
    between two fetches of an unchanged pitch and must never by itself
    be treated as a content change. content_complete is decided by the
    caller (fetch(), from a live page-state check) and passed straight
    through -- this function never re-derives it.
    """
    text = html_to_text(html)
    title = extract_title(html) or discovered.title
    published_at = extract_published_at(html) or discovered.published_at
    raw_capture_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
    canonical_source_text = build_canonical_source_text(html)
    content_hash = hashlib.sha256(canonical_source_text.encode("utf-8")).hexdigest()

    return base.SourceItem(
        source_name=SOURCE_NAME,
        external_id=discovered.external_id,
        canonical_url=discovered.canonical_url,
        title=title,
        author=None,
        published_at=published_at,
        discovered_at=discovered_at,
        ticker=discovered.ticker,
        company=discovered.company,
        source_type=SOURCE_TYPE,
        raw_text=text,
        raw_html=html,
        content_hash=content_hash,
        raw_capture_hash=raw_capture_hash,
        content_complete=content_complete,
        metadata={},
    )


# --- live-page glue (never unit tested with a real browser; kept thin) ------

_LOGIN_REDIRECT_PATTERN = re.compile(r"/(login|signin|sign-in|auth)(?:/|$|\?)", re.IGNORECASE)


def _looks_blocked(page, html: str) -> bool:
    """Two independent, deliberately narrow signals -- either is enough:
    (1) the browser ended up at a URL that clearly looks like a login/auth
    page (the most reliable signal when the site redirects), or (2)
    _looks_logged_out's existing page-content heuristic (a password field
    with no pitch links). A generic "Login" link elsewhere on an otherwise
    normal, accessible page trips NEITHER of these -- that alone is never
    treated as AUTH_REQUIRED.
    """
    current_url = getattr(page, "url", None)
    if current_url and _LOGIN_REDIRECT_PATTERN.search(current_url):
        return True
    return _looks_logged_out(html)


def is_authenticated(page) -> bool:
    page.goto(FEED_URL, wait_until="domcontentloaded")
    return not _looks_blocked(page, page.content())


def discover(page, *, limit: int) -> list[base.DiscoveredItem]:
    page.goto(FEED_URL, wait_until="domcontentloaded")
    try:
        # Short, bounded wait only -- the feed may be client-rendered, so
        # links might not exist yet immediately after DOMContentLoaded.
        # Never a sleep, never a retry loop, never scrolling.
        page.wait_for_selector('a[href*="/sp/"]', timeout=PITCH_LINK_WAIT_MS)
    except Exception:
        pass  # fall through -- what actually happened is decided below

    html = page.content()
    if _looks_blocked(page, html):
        raise base.AuthRequiredError(
            f"Yellowbrick session appears blocked or logged out at {FEED_URL} "
            f"(current URL: {getattr(page, 'url', None) or 'unknown'})."
        )

    items = discover_pitches_from_html(html, limit=limit)
    if not items:
        print(_build_zero_result_diagnostic(page, html))
    return items


def fetch(page, discovered: base.DiscoveredItem, *, discovered_at: str) -> base.SourceItem:
    page.goto(discovered.canonical_url, wait_until="domcontentloaded")
    html = page.content()
    if _looks_blocked(page, html):
        raise base.AuthRequiredError(
            f"Yellowbrick session appears blocked or logged out while fetching "
            f"{discovered.canonical_url} (current URL: {getattr(page, 'url', None) or 'unknown'})."
        )

    html = _expand_full_summary_if_present(page, html)
    content_complete = _content_is_complete(html)

    return parse_pitch_page(html, discovered, discovered_at=discovered_at, content_complete=content_complete)


# --- LIVE DIAGNOSTIC (Stage 5.9) --------------------------------------------
#
# Everything below is READ-ONLY evidence-gathering for one specific pitch
# page, used only by `python run.py diagnose-source-live` (see cli.py's
# cmd_diagnose_source_live). Nothing here is called by fetch(), discover(),
# or collect-source -- it makes NO database writes, NO raw artifact writes,
# and NO LLM calls, and must never be treated as a capture-logic fix.

_GATING_PHRASES = (
    "log in",
    "log into your account",
    "sign in",
    "sign up",
    "subscribe",
    "subscription required",
    "upgrade to yellowbrick premium",
    "permission denied",
    "not authorized",
    "access denied",
)

_SCRIPT_BLOCK_PATTERN = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.IGNORECASE | re.DOTALL)
_SCRIPT_ID_PATTERN = re.compile(r'\bid="([^"]+)"', re.IGNORECASE)
_SCRIPT_TYPE_PATTERN = re.compile(r'\btype="([^"]+)"', re.IGNORECASE)
_HYDRATION_MARKERS = (
    "__NEXT_DATA__",
    "__next_f.push",
    "application/json",
    "application/ld+json",
    "window.__INITIAL_STATE__",
)
# Below this, an unremarkable inline script is not worth reporting unless it
# also matches a known hydration marker or a caller-supplied search term.
_LARGE_SCRIPT_CHAR_THRESHOLD = 2000

_ATTR_QUERY_STRIP_PATTERN = re.compile(r'\b(href|src)="([^"]*)"', re.IGNORECASE)
_INLINE_EVENT_HANDLER_PATTERN = re.compile(r'\s+on\w+="[^"]*"', re.IGNORECASE)
_MAX_OUTER_HTML_EXCERPT_CHARS = 300

# Bounded polling wait (diagnostic only): checks for a canonical-text-length
# change every POLL_INTERVAL_MS, returning as soon as one is observed rather
# than always waiting the full window -- but NEVER waits past MAX_WAIT_MS,
# so this can never hang indefinitely.
LIVE_DIAGNOSTIC_POLL_INTERVAL_MS = 300
LIVE_DIAGNOSTIC_MAX_WAIT_MS = 3000

# Stage 5.10: specific phrases Brad supplied from the REAL pitch 143618 page
# (task section 5) -- checked for PRESENCE only inside __next_f.push
# hydration blocks, never printed as content. Deliberately hardcoded here:
# this is a one-off diagnostic check for this specific live investigation,
# not a general-purpose content search.
_NEXT_F_PUSH_MARKER = "__next_f.push"
_THESIS_SEARCH_PHRASES = (
    "Berner Industrier",
    "Cervantes",
    "serial acquirer",
    "SEK 133.89",
    "Show full summary",
    "Why Berner Industrier share price is depressed",
)

# Task section 1: how far up the DOM to walk from a "Show full summary"
# text match when looking for its actual clickable ancestor. Six levels is
# generous for a typical card/component nesting depth without walking all
# the way to <body>.
_DOM_CONTEXT_ANCESTOR_LEVELS = 6
_DOM_CONTEXT_CONTAINER_EXCERPT_MAX_CHARS = 2000

# A single evaluate() call (not one Playwright round-trip per ancestor)
# that walks up to _DOM_CONTEXT_ANCESTOR_LEVELS ancestors and inspects both
# immediate siblings of one element -- never clicks anything. Each
# describe()'d node's own "visible"/"enabled" fields are a computed-style/
# bounding-box heuristic (task section 2's actionability probe), not
# Playwright's own actionability engine, which only ever runs against the
# original text-match locator itself, never a hand-picked ancestor.
_DOM_CONTEXT_SCRIPT = """
(el) => {
    function describe(node) {
        if (!node) return null;
        const rect = node.getBoundingClientRect ? node.getBoundingClientRect() : null;
        const style = window.getComputedStyle ? window.getComputedStyle(node) : null;
        const visible = !!(rect && rect.width > 0 && rect.height > 0 && style &&
            style.visibility !== 'hidden' && style.display !== 'none');
        return {
            tag: node.tagName ? node.tagName.toLowerCase() : null,
            class_name: node.className || null,
            role: node.getAttribute ? node.getAttribute('role') : null,
            href: node.getAttribute ? node.getAttribute('href') : null,
            tabindex: node.getAttribute ? node.getAttribute('tabindex') : null,
            aria_expanded: node.getAttribute ? node.getAttribute('aria-expanded') : null,
            aria_controls: node.getAttribute ? node.getAttribute('aria-controls') : null,
            has_onclick: !!(node.onclick || (node.getAttribute && node.getAttribute('onclick'))),
            cursor: style ? style.cursor : null,
            visible: visible,
            enabled: !(node.disabled === true),
            bounding_box: rect ? {x: rect.x, y: rect.y, width: rect.width, height: rect.height} : null,
            outer_html_start: node.outerHTML ? node.outerHTML.slice(0, 200) : null,
            text_excerpt: node.textContent ? node.textContent.trim().slice(0, 80) : null,
        };
    }
    const ancestorNodes = [];
    let cur = el.parentElement;
    for (let i = 0; i < 6 && cur; i++) { ancestorNodes.push(cur); cur = cur.parentElement; }
    const containerNode = ancestorNodes[Math.min(2, ancestorNodes.length - 1)] || null;
    return {
        ancestors: ancestorNodes.map(describe),
        previous_sibling: describe(el.previousElementSibling),
        next_sibling: describe(el.nextElementSibling),
        container_outer_html: (containerNode && containerNode.outerHTML) ? containerNode.outerHTML.slice(0, 2000) : null,
    };
}
"""

# Resolves a relation string (e.g. "ancestor[1]", "previous_sibling") back
# to the SAME node _DOM_CONTEXT_SCRIPT described, and clicks it in-page
# (task section 3) -- never the text-match element itself, and only ever
# invoked when _resolve_actionable_candidates found exactly one candidate.
# Uses a raw in-page node.click() rather than Playwright's own
# actionability-checked locator.click(): acceptable for a bounded
# diagnostic probe of a hand-picked ancestor/sibling, but not necessarily
# how an eventual production fix should click it.
_RELATED_CLICK_SCRIPT = """
(el, relation) => {
    function resolveRelatedNode(start, rel) {
        if (rel === 'previous_sibling') return start.previousElementSibling;
        if (rel === 'next_sibling') return start.nextElementSibling;
        const m = /^ancestor\\[(\\d+)\\]$/.exec(rel);
        if (m) {
            let idx = parseInt(m[1], 10);
            let cur = start;
            for (let i = 0; i <= idx && cur; i++) { cur = cur.parentElement; }
            return cur;
        }
        return null;
    }
    const node = resolveRelatedNode(el, relation);
    if (!node) return {clicked: false};
    node.click();
    return {clicked: true};
}
"""

# Priority order for "nearest candidate actionable ancestor" (task section
# 1C): semantic/explicit-interactivity signals beat a bare cursor:pointer
# heuristic. Checked across BOTH ancestors (nearest first) and immediate
# siblings of every "Show full summary" text match.
_ACTIONABILITY_TIERS = ("button_tag", "anchor_tag", "role_button", "tabindex", "onclick", "cursor_pointer")


def _sanitize_url(url: str | None) -> str:
    """Scheme + host + path only -- strips any query string or fragment,
    which may carry session/auth tokens. Never includes headers or
    cookies (this function never has access to them).
    """
    if not url:
        return "unknown"
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _sanitize_outer_html(outer_html: str | None, *, max_chars: int = _MAX_OUTER_HTML_EXCERPT_CHARS) -> str | None:
    """A short, best-effort-sanitized excerpt of one element's outerHTML --
    strips query strings off href/src (may carry tokens) and inline event
    handler attributes, and caps length. Used both for one small UI
    control's markup (default cap) and for a slightly larger containing-
    component excerpt (task section 4, larger max_chars) -- never a whole
    page either way.
    """
    if not outer_html:
        return outer_html

    def _strip_query(match: re.Match) -> str:
        attr, val = match.group(1), match.group(2)
        return f'{attr}="{val.split("?")[0]}"'

    cleaned = _ATTR_QUERY_STRIP_PATTERN.sub(_strip_query, outer_html)
    cleaned = _INLINE_EVENT_HANDLER_PATTERN.sub("", cleaned)
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + "…"
    return cleaned


def _safe_call(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _count_collapsed_control_lines(html: str) -> int:
    """Same STANDALONE-line matching rule as _looks_collapsed (never a raw
    substring search) -- returns a COUNT instead of a bool. Used only by
    the diagnostic's before/after metrics; never by fetch()/collect-source,
    and does not replace or alter _looks_collapsed in any way.
    """
    target = SHOW_FULL_SUMMARY_TEXT.lower()
    return sum(
        1
        for line in html_to_text(html).split("\n")
        if line.strip().lower().rstrip(".!…:") == target
    )


def find_show_full_summary_candidates(page) -> list[dict]:
    """Enumerates EVERY element whose visible text matches/contains
    SHOW_FULL_SUMMARY_TEXT -- never clicks anything (task section 2).
    Visibility/enabled/bounding-box genuinely require a live rendered page
    (CSS layout, JS state), so this only works against a real (or
    Playwright-shaped fake) page -- it cannot be derived from a static
    HTML string.
    """
    locator = page.get_by_text(SHOW_FULL_SUMMARY_TEXT, exact=False)
    count = _safe_call(locator.count, 0) or 0
    candidates = []
    for i in range(count):
        item = locator.nth(i)
        candidates.append(
            {
                "index": i,
                "tag_name": _safe_call(lambda item=item: item.evaluate("el => el.tagName.toLowerCase()")),
                "visible": _safe_call(item.is_visible),
                "enabled": _safe_call(item.is_enabled),
                "role": _safe_call(lambda item=item: item.get_attribute("role")),
                "href": _safe_call(lambda item=item: item.get_attribute("href")),
                "aria_expanded": _safe_call(lambda item=item: item.get_attribute("aria-expanded")),
                "aria_controls": _safe_call(lambda item=item: item.get_attribute("aria-controls")),
                "type": _safe_call(lambda item=item: item.get_attribute("type")),
                "outer_html_excerpt": _sanitize_outer_html(
                    _safe_call(lambda item=item: item.evaluate("el => el.outerHTML"))
                ),
                "bounding_box": _safe_call(item.bounding_box),
            }
        )
    return candidates


def _inspect_dom_context(locator) -> dict:
    """Single evaluate() call (task section 1): walks up to
    _DOM_CONTEXT_ANCESTOR_LEVELS ancestors and inspects both immediate
    siblings of one "Show full summary" text match, WITHOUT assuming the
    match itself is clickable -- the real production evidence (Stage 5.10)
    showed clicking the span directly does nothing at all.
    """
    context = _safe_call(lambda: locator.evaluate(_DOM_CONTEXT_SCRIPT))
    if not context:
        return {"ancestors": [], "previous_sibling": None, "next_sibling": None, "container_outer_html": None}
    context["container_outer_html"] = _sanitize_outer_html(
        context.get("container_outer_html"), max_chars=_DOM_CONTEXT_CONTAINER_EXCERPT_MAX_CHARS
    )
    for descriptor in [*(context.get("ancestors") or []), context.get("previous_sibling"), context.get("next_sibling")]:
        if descriptor and descriptor.get("outer_html_start"):
            descriptor["outer_html_start"] = _sanitize_outer_html(descriptor["outer_html_start"], max_chars=200)
    return context


def _classify_actionability(descriptor: dict | None) -> str | None:
    """Task section 1C's priority order, as a single-tier classification
    for one ancestor/sibling descriptor. None means "not actionable by any
    of these signals" -- never a guess.
    """
    if not descriptor:
        return None
    tag = (descriptor.get("tag") or "").lower()
    if tag == "button":
        return "button_tag"
    if tag == "a":
        return "anchor_tag"
    if (descriptor.get("role") or "").lower() == "button":
        return "role_button"
    if descriptor.get("tabindex") not in (None, ""):
        return "tabindex"
    if descriptor.get("has_onclick"):
        return "onclick"
    if (descriptor.get("cursor") or "").lower() == "pointer":
        return "cursor_pointer"
    return None


def _classify_dom_context(dom_context: dict) -> list[dict]:
    """Every ancestor/sibling that matches SOME actionability tier, tagged
    with its relation string (e.g. "ancestor[1]", "previous_sibling") --
    the raw material _resolve_actionable_candidates picks from.
    """
    found = []
    for i, ancestor in enumerate(dom_context.get("ancestors") or []):
        tier = _classify_actionability(ancestor)
        if tier:
            found.append({"relation": f"ancestor[{i}]", "tier": tier, **ancestor})
    for relation in ("previous_sibling", "next_sibling"):
        descriptor = dom_context.get(relation)
        tier = _classify_actionability(descriptor)
        if tier:
            found.append({"relation": relation, "tier": tier, **descriptor})
    return found


def _get_relation_descriptor(dom_context: dict, relation: str) -> dict | None:
    """Looks a relation string (e.g. "ancestor[1]", "previous_sibling")
    back up in a _inspect_dom_context() result -- used to re-read the
    SAME resolved target's aria-expanded after a click, without assuming
    it was the span itself.
    """
    if relation in ("previous_sibling", "next_sibling"):
        return dom_context.get(relation)
    if relation.startswith("ancestor[") and relation.endswith("]"):
        try:
            index = int(relation[len("ancestor[") : -1])
        except ValueError:
            return None
        ancestors = dom_context.get("ancestors") or []
        return ancestors[index] if 0 <= index < len(ancestors) else None
    return None


def _resolve_actionable_candidates(candidates: list[dict]) -> dict:
    """Picks the SINGLE best-tier candidate across every ancestor/sibling
    of every "Show full summary" text match -- never guesses (task section
    3): if more than one candidate shares the best tier found, returns
    them all as 'ambiguous' instead of picking one arbitrarily.
    """
    if not candidates:
        return {"resolved": None, "ambiguous": []}
    best_tier_index = min(_ACTIONABILITY_TIERS.index(c["tier"]) for c in candidates)
    best_tier = _ACTIONABILITY_TIERS[best_tier_index]
    top = [c for c in candidates if c["tier"] == best_tier]
    if len(top) == 1:
        return {"resolved": top[0], "ambiguous": []}
    return {"resolved": None, "ambiguous": top}


def _click_related_node(locator, relation: str) -> None:
    """Clicks the SPECIFIC resolved ancestor/sibling node in-browser (task
    section 3) -- never the text-match element itself, and only ever
    called once _resolve_actionable_candidates found exactly one
    candidate. Raises if the node can't be resolved/clicked, exactly like
    a real Playwright locator.click() failure, so callers can handle both
    uniformly.
    """
    outcome = locator.evaluate(_RELATED_CLICK_SCRIPT, relation)
    if not outcome or not outcome.get("clicked"):
        raise RuntimeError(f"could not resolve/click node for relation={relation!r}")


def detect_gating_signals(html: str) -> list[str]:
    """Factual report only (task section 6) -- never used to bypass
    anything. Case-insensitive substring match against visible text.
    """
    text = html_to_text(html).lower()
    return [phrase for phrase in _GATING_PHRASES if phrase in text]


def detect_hidden_data_blocks(html: str, *, search_terms: list[str | None]) -> list[dict]:
    """Reports the SHAPE of each script/JSON block in the raw page -- id/
    type, approximate length, and whether a known hydration marker or a
    caller-supplied search term (e.g. the pitch's own discovered ticker or
    company -- never a hardcoded value) appears -- WITHOUT ever returning
    the block's actual content (task section 5: diagnosis only; never used
    to reconstruct or substitute for captured content).

    For any block that is itself a `__next_f.push` hydration/streaming
    chunk, ALSO reports (Stage 5.10) whether it contains any of the fixed
    _THESIS_SEARCH_PHRASES Brad supplied from the real pitch 143618 page --
    again, presence only, never the block's content.
    """
    terms = [t for t in search_terms if t]
    blocks = []
    for i, match in enumerate(_SCRIPT_BLOCK_PATTERN.finditer(html)):
        attrs_str, body = match.group(1), match.group(2)
        id_match = _SCRIPT_ID_PATTERN.search(attrs_str)
        type_match = _SCRIPT_TYPE_PATTERN.search(attrs_str)
        marker_hits = [marker for marker in _HYDRATION_MARKERS if marker in body]
        term_hits = [term for term in terms if term in body]
        if not marker_hits and not term_hits and len(body) < _LARGE_SCRIPT_CHAR_THRESHOLD:
            continue
        thesis_phrases_found = (
            [phrase for phrase in _THESIS_SEARCH_PHRASES if phrase in body]
            if _NEXT_F_PUSH_MARKER in marker_hits
            else []
        )
        blocks.append(
            {
                "block_id": id_match.group(1) if id_match else f"script#{i}",
                "type": type_match.group(1) if type_match else None,
                "approx_chars": len(body),
                "hydration_markers_found": marker_hits,
                "search_terms_found": term_hits,
                "thesis_phrases_found": thesis_phrases_found,
            }
        )
    return blocks


def _capture_diagnostic_metrics(page, html: str) -> dict:
    return {
        "url": _sanitize_url(getattr(page, "url", None)),
        "title": extract_title(html),
        "body_text_chars": len(html_to_text(html)),
        "canonical_text_chars": len(build_canonical_source_text(html)),
        "collapsed_control_line_count": _count_collapsed_control_lines(html),
    }


def _observe_click_effects(context, page, click_fn) -> dict:
    """Invokes `click_fn` (a zero-argument callable that performs exactly
    one click) and records exactly what was observed during it: sanitized
    network request metadata (method/host/path only -- never query
    strings, headers, cookies, or bodies) seen on `page`, and any new
    page/popup opened on `context` (task sections 3-4). Never assumes an
    outcome -- the caller separately re-measures page state after a
    bounded wait. `click_fn` is a callable (not a bare locator) so the
    SAME observation logic works whether the click is a plain
    locator.click() or a resolved-ancestor click via _click_related_node.
    """
    requests_seen: list[dict] = []
    new_pages: list[dict] = []

    def _on_request(request):
        try:
            parsed = urlparse(request.url)
            requests_seen.append({"method": request.method, "host": parsed.netloc, "path": parsed.path})
        except Exception:
            pass

    def _on_new_page(new_page):
        try:
            new_pages.append({"url": _sanitize_url(getattr(new_page, "url", None)), "title": new_page.title()})
        except Exception:
            pass

    page.on("request", _on_request)
    if context is not None:
        context.on("page", _on_new_page)

    click_error = None
    try:
        click_fn()
    except Exception as exc:
        click_error = str(exc)

    try:
        page.remove_listener("request", _on_request)
    except Exception:
        pass
    if context is not None:
        try:
            context.remove_listener("page", _on_new_page)
        except Exception:
            pass

    return {"click_error": click_error, "requests_seen": requests_seen, "new_pages": new_pages}


def _wait_for_meaningful_change(page, *, before_canonical_chars: int) -> bool:
    """Bounded polling wait (diagnostic only -- never used by fetch()):
    re-checks canonical text length every LIVE_DIAGNOSTIC_POLL_INTERVAL_MS
    and returns True as soon as it changes, instead of always sleeping the
    full window. Returns False -- never raises, never hangs -- if
    LIVE_DIAGNOSTIC_MAX_WAIT_MS elapses with no change.
    """
    elapsed = 0
    while elapsed < LIVE_DIAGNOSTIC_MAX_WAIT_MS:
        page.wait_for_timeout(LIVE_DIAGNOSTIC_POLL_INTERVAL_MS)
        elapsed += LIVE_DIAGNOSTIC_POLL_INTERVAL_MS
        if len(build_canonical_source_text(page.content())) != before_canonical_chars:
            return True
    return False


def diagnose_live(page, context, discovered: base.DiscoveredItem) -> dict:
    """READ-ONLY (task sections 1-6): navigates to exactly one pitch page
    and gathers direct evidence about its "Show full summary" text
    match(es), their DOM ancestors/siblings, and (only when exactly one
    actionable ancestor/sibling is found) what clicking THAT element
    actually does. Makes NO database writes, NO raw artifact writes, NO
    LLM calls, and never calls extract_idea/screen_idea -- completely
    separate from fetch()/parse_pitch_page(), which this does not touch
    or affect.

    Never clicks the "Show full summary" text match itself (Stage 5.10:
    real production evidence showed that span is only a label -- clicking
    it directly had zero observable effect) and never guesses when more
    than one ancestor/sibling looks equally plausible.
    """
    page.goto(discovered.canonical_url, wait_until="domcontentloaded")
    html_before = page.content()
    if _looks_blocked(page, html_before):
        raise base.AuthRequiredError(
            f"Yellowbrick session appears blocked or logged out while diagnosing "
            f"{discovered.canonical_url} (current URL: {getattr(page, 'url', None) or 'unknown'})."
        )

    candidates = find_show_full_summary_candidates(page)
    all_actionable_candidates: list[dict] = []
    for i, candidate in enumerate(candidates):
        locator_i = page.get_by_text(SHOW_FULL_SUMMARY_TEXT, exact=False).nth(i)
        dom_context = _inspect_dom_context(locator_i)
        candidate["dom_context"] = dom_context
        relation_candidates = _classify_dom_context(dom_context)
        for rc in relation_candidates:
            rc["span_index"] = i
        candidate["actionable_candidates"] = relation_candidates
        all_actionable_candidates.extend(relation_candidates)

    metrics_before = _capture_diagnostic_metrics(page, html_before)
    search_terms = [discovered.ticker, discovered.company, metrics_before["title"]]
    resolution = _resolve_actionable_candidates(all_actionable_candidates)

    result: dict = {
        "external_id": discovered.external_id,
        "canonical_url": _sanitize_url(discovered.canonical_url),
        "candidates": candidates,
        "actionable_resolution": resolution,
        "metrics_before": metrics_before,
        "gating_signals_before": detect_gating_signals(html_before),
        "hidden_data_blocks": detect_hidden_data_blocks(html_before, search_terms=search_terms),
        "click_attempted": False,
    }

    if len(candidates) == 0:
        result["click_skipped_reason"] = "no 'Show full summary' text match found on the page"
        return result

    if resolution["resolved"] is None:
        if resolution["ambiguous"]:
            result["click_skipped_reason"] = (
                f"{len(resolution['ambiguous'])} equally plausible actionable ancestors/siblings found -- "
                f"refusing to guess which one to click"
            )
        else:
            result["click_skipped_reason"] = (
                "no actionable ancestor or sibling found within "
                f"{_DOM_CONTEXT_ANCESTOR_LEVELS} levels or the immediate siblings of any "
                f"'{SHOW_FULL_SUMMARY_TEXT}' text match -- it is almost certainly only a label"
            )
        return result

    target = resolution["resolved"]
    result["actionable_target"] = target
    locator_for_click = page.get_by_text(SHOW_FULL_SUMMARY_TEXT, exact=False).nth(target["span_index"])
    click_effects = _observe_click_effects(
        context, page, lambda: _click_related_node(locator_for_click, target["relation"])
    )

    result["click_attempted"] = True
    result["click_error"] = click_effects["click_error"]
    result["requests_seen_during_click"] = click_effects["requests_seen"]
    result["new_pages_opened"] = click_effects["new_pages"]
    result["aria_expanded_before"] = target.get("aria_expanded")

    changed = _wait_for_meaningful_change(page, before_canonical_chars=metrics_before["canonical_text_chars"])
    html_after = page.content()

    result["meaningful_change_observed"] = changed
    result["metrics_after"] = _capture_diagnostic_metrics(page, html_after)
    result["gating_signals_after"] = detect_gating_signals(html_after)

    dom_context_after = _inspect_dom_context(locator_for_click)
    target_after = _get_relation_descriptor(dom_context_after, target["relation"])
    result["aria_expanded_after"] = target_after.get("aria_expanded") if target_after else None

    return result


def _format_descriptor(descriptor: dict | None) -> str:
    if not descriptor:
        return "(none)"
    return (
        f"tag={descriptor.get('tag')} class={descriptor.get('class_name')} role={descriptor.get('role')} "
        f"href={descriptor.get('href')} tabindex={descriptor.get('tabindex')} "
        f"aria-expanded={descriptor.get('aria_expanded')} aria-controls={descriptor.get('aria_controls')} "
        f"onclick={descriptor.get('has_onclick')} cursor={descriptor.get('cursor')} "
        f"visible={descriptor.get('visible')} enabled={descriptor.get('enabled')} "
        f"bounding_box={descriptor.get('bounding_box')} text={descriptor.get('text_excerpt')!r}\n"
        f"        outerHTML: {descriptor.get('outer_html_start')}"
    )


def format_live_diagnostic_report(result: dict) -> str:
    """Renders diagnose_live()'s findings into the report shape requested
    (task sections 1-7). Every value already flowed through this module's
    sanitization helpers -- this function never prints cookies,
    localStorage, auth headers, session tokens, full script contents, or
    query parameters, because none of those are present in `result`.
    """
    lines = [
        f"Yellowbrick live diagnostic for external_id={result['external_id']!r}",
        f"URL: {result['canonical_url']}",
        "",
        f"'{SHOW_FULL_SUMMARY_TEXT}' text matches found: {len(result['candidates'])}",
    ]
    for c in result["candidates"]:
        lines.append(f"  [{c['index']}] SPAN/label: tag={c['tag_name']} visible={c['visible']} enabled={c['enabled']}")
        lines.append(f"      role={c['role']} type={c['type']} href={c['href']}")
        lines.append(f"      aria-expanded={c['aria_expanded']} aria-controls={c['aria_controls']}")
        lines.append(f"      bounding_box={c['bounding_box']}")
        lines.append(f"      outerHTML: {c['outer_html_excerpt']}")

        dom_context = c.get("dom_context") or {}
        ancestors = dom_context.get("ancestors") or []
        lines.append(f"      ancestor chain ({len(ancestors)} level(s)):")
        for i, ancestor in enumerate(ancestors):
            lines.append(f"        [{i}] {_format_descriptor(ancestor)}")
        lines.append(f"      previous sibling: {_format_descriptor(dom_context.get('previous_sibling'))}")
        lines.append(f"      next sibling: {_format_descriptor(dom_context.get('next_sibling'))}")
        lines.append(f"      containing-component DOM excerpt: {dom_context.get('container_outer_html')}")

        actionable_here = c.get("actionable_candidates") or []
        if actionable_here:
            lines.append(f"      actionable ancestor/sibling candidate(s) found here: {len(actionable_here)}")
            for ac in actionable_here:
                lines.append(f"        relation={ac['relation']} tier={ac['tier']}")
        else:
            lines.append("      no actionable ancestor/sibling found for this text match")

    lines.append("")
    resolution = result["actionable_resolution"]
    if result["click_attempted"]:
        target = result["actionable_target"]
        lines.append(
            f"Actionable target resolved: relation={target['relation']!r} on span[{target['span_index']}] "
            f"(tier={target['tier']!r}, tag={target.get('tag')}, role={target.get('role')})"
        )
    elif resolution["ambiguous"]:
        lines.append(f"AMBIGUOUS -- refusing to guess. {result.get('click_skipped_reason')}")
        for ac in resolution["ambiguous"]:
            lines.append(f"  candidate: relation={ac['relation']} tier={ac['tier']} span_index={ac['span_index']}")
    else:
        lines.append(f"No click attempted: {result.get('click_skipped_reason')}")

    lines.append("")
    lines.append("BEFORE:")
    for key, value in result["metrics_before"].items():
        lines.append(f"  {key}: {value}")
    lines.append(f"  gating signals: {result['gating_signals_before'] or 'none'}")

    if result["click_attempted"]:
        lines.append("")
        lines.append(f"Click error: {result['click_error'] or 'none'}")
        lines.append(f"Requests seen during click: {result['requests_seen_during_click'] or 'none'}")
        lines.append(f"New pages/popups opened: {result['new_pages_opened'] or 'none'}")
        lines.append("")
        lines.append(
            f"Meaningful change observed within {LIVE_DIAGNOSTIC_MAX_WAIT_MS}ms: {result['meaningful_change_observed']}"
        )
        lines.append(f"aria-expanded before -> after: {result['aria_expanded_before']} -> {result['aria_expanded_after']}")
        lines.append("")
        lines.append("AFTER:")
        for key, value in result["metrics_after"].items():
            lines.append(f"  {key}: {value}")
        lines.append(f"  gating signals: {result['gating_signals_after'] or 'none'}")

    lines.append("")
    lines.append(f"Hidden data / script blocks of interest: {len(result['hidden_data_blocks'])}")
    for block in result["hidden_data_blocks"]:
        lines.append(
            f"  {block['block_id']} type={block['type']} approx_chars={block['approx_chars']} "
            f"hydration_markers={block['hydration_markers_found']} search_terms_found={block['search_terms_found']}"
        )
        if block["thesis_phrases_found"]:
            lines.append(f"    *** THESIS-SPECIFIC PHRASES FOUND in {block['block_id']}: {block['thesis_phrases_found']}")

    return "\n".join(lines)
