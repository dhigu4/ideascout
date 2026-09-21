"""Standard types every website-source adapter produces, and the one
exception collectors use to fail clearly on an expired/missing login.

Nothing in this module knows about any specific website. Discovery
(cheap, per-item-list metadata) is deliberately a separate, lighter type
from a fully-fetched SourceItem -- see ideascout/sources/README-ish
docstring in cli.py's collection command for why discovery and fetching
are kept apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class AuthRequiredError(RuntimeError):
    """Raised by a source adapter when the authenticated session has
    expired, was never established, or a login/CAPTCHA/MFA/subscription
    gate blocks access. Adapters must raise this rather than attempting to
    bypass whatever is blocking them -- collectors treat it as a clear,
    expected "please log in again" signal, never a generic error.
    """


@dataclass(frozen=True)
class DiscoveredItem:
    """Cheap metadata about one item found while scanning a source's
    listing/feed page -- enough to decide whether it's already known,
    never anything requiring a second page load or an LLM call.
    """

    external_id: str
    canonical_url: str
    title: str | None = None
    author: str | None = None
    published_at: str | None = None
    added_at: str | None = None
    ticker: str | None = None
    company: str | None = None


@dataclass(frozen=True)
class SourceItem:
    """One fully-fetched source document, ready to be saved as permanent
    provenance and (later, separately) extracted into a compact idea
    record. raw_html is the complete captured page; raw_text is already
    reduced to plain text for the extraction LLM call.

    Two DELIBERATELY separate hashes (Stage 5.4 fix -- see
    ideascout/sources/yellowbrick.py's build_canonical_source_text):
      content_hash -- hash of a NORMALIZED, substantive-only rendering of
        the page. This is the ONE semantic version identity: collectors
        compare THIS to decide whether a document has materially changed.
      raw_capture_hash -- hash of the exact raw_html bytes captured this
        time, kept only as an extra provenance/diagnostic detail. It is
        expected to differ between two fetches of an unchanged pitch
        (volatile scripts, session/hydration state, timestamps, etc.) and
        must never by itself trigger a new version.

    content_complete (Stage 5.7 fix, completeness logic replaced in Stage
    5.11): True unless the adapter has a reliable, adapter-specific
    page-state signal that more substantive content exists but could not
    be obtained (e.g. Yellowbrick's full-summary switch still reporting
    aria-checked="false"/data-state="unchecked" after an expand attempt)
    -- see yellowbrick.py's _content_is_complete. When False, raw
    provenance is still saved, but callers must NOT run compact
    extraction or screening against it: a thin, incomplete capture must
    never be silently treated as the whole pitch.
    """

    source_name: str
    external_id: str
    canonical_url: str
    title: str | None
    author: str | None
    published_at: str | None
    discovered_at: str
    ticker: str | None
    company: str | None
    source_type: str
    raw_text: str
    raw_html: str
    content_hash: str
    raw_capture_hash: str
    content_complete: bool = True
    metadata: dict = field(default_factory=dict)
