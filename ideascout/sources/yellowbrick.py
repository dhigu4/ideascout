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
    ignored.
    """
    if not text:
        return False
    return text.strip().lower().rstrip(".!…") in _ACTION_TEXT_PHRASES


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


def parse_pitch_page(html: str, discovered: base.DiscoveredItem, *, discovered_at: str) -> base.SourceItem:
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
    be treated as a content change.
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
    return parse_pitch_page(html, discovered, discovered_at=discovered_at)
