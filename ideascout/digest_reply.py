"""Deterministic (NO LLM) parsing of Brad's direct replies to a
send-digest email (Stage 9).

CRITICAL METHODOLOGICAL RULE: a digest reply is NOT a clean holdout
judgment -- Brad has already seen IdeaScout's own screen result/rationale
before writing it. Feedback derived from a digest reply is always created
with feedback_origin='DIGEST_REPLY' and holdout_eligible=0 (see cli.py's
cmd_parse_mail and db.py's Migration 12) so it can still train a future
Taste build (get_feedback_eligible_for_learning is unchanged and does not
care about origin), but can never be miscounted as a genuine blind
holdout judgment or shadow-scored as one (db.get_eligible_feedback_after
filters on holdout_eligible).

This module NEVER calls an LLM anywhere. Everything here is regex/string
logic over the raw reply body, deliberately mirroring source_isolation.py's
own "fail closed rather than guess" philosophy: an ambiguous reply, an
unrecognized verdict, or an unresolvable ticker/company always produces a
failure the caller routes to NEEDS_REVIEW, never a guessed verdict and
never a partially-applied result. Brad's explicit verdict is required --
this module has no mechanism to infer one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

PARSER_VERSION = "digest-reply-v1"
MODEL_NAME = "deterministic"

# --- reply-text isolation ----------------------------------------------------
#
# Deliberately SEPARATE from source_isolation.py: that module solves the
# opposite-polarity problem (extract quoted/forwarded SOURCE material,
# discard Brad's leading commentary, for a totally different table/
# purpose). Here we want exactly Brad's own NEW reply text, discarding
# everything from the first quoted/forwarded boundary onward.

_GMAIL_FORWARD_MARKER = re.compile(r"-{2,}[ \t]*Forwarded message[ \t]*-{2,}", re.IGNORECASE)
_GMAIL_ON_WROTE_PATTERN = re.compile(r"^On .+ wrote:[ \t]*$", re.IGNORECASE | re.MULTILINE)
_OUTLOOK_ORIGINAL_MARKER = re.compile(r"-{2,}[ \t]*Original Message[ \t]*-{2,}", re.IGNORECASE)
_OUTLOOK_HEADER_BLOCK = re.compile(
    r"^From:.*\n(?:Sent|Date):.*\n(?:To:.*\n)?Subject:.*$", re.IGNORECASE | re.MULTILINE
)
_QUOTE_LINE = re.compile(r"^[ \t]*>")

# RFC 3676's standard signature delimiter -- "-- " (dash dash space) alone
# on its own line -- is the one reliably machine-recognizable signature
# boundary; used by Gmail, Outlook, Apple Mail, and many others when a
# signature block is configured. Conservative on purpose: only a handful
# of well-known auto-appended mobile footers are ALSO recognized as exact
# lines, never a guess at where Brad's own prose "sounds like" a sign-off.
_SIGNATURE_DELIMITER = re.compile(r"^--[ \t]?$", re.MULTILINE)
_MOBILE_FOOTER_PHRASES = frozenset(
    {
        "sent from my iphone",
        "sent from my ipad",
        "sent from my android",
        "sent from my samsung galaxy",
        "get outlook for ios",
        "get outlook for android",
    }
)


@dataclass(frozen=True)
class ReplyIsolationResult:
    ok: bool
    reply_text: Optional[str] = None
    reason: Optional[str] = None


def _earliest(current: int | None, candidate: int) -> int:
    return candidate if current is None else min(current, candidate)


def _strip_signature(text: str) -> str:
    """Removes a trailing signature/footer block when safely
    recognizable (the standard "-- " delimiter, or a known auto-appended
    mobile-footer line) -- never removes anything else, so Brad's own
    prose is never mistaken for a sign-off.
    """
    delimiter_match = _SIGNATURE_DELIMITER.search(text)
    if delimiter_match:
        text = text[: delimiter_match.start()]

    lines = text.splitlines()
    for i, line in enumerate(lines):
        normalized = line.strip().lower().rstrip(".!")
        if normalized in _MOBILE_FOOTER_PHRASES:
            return "\n".join(lines[:i])
    return text


def isolate_reply_text(body: str | None) -> ReplyIsolationResult:
    """Returns ONLY Brad's own newly-typed reply text. Everything from the
    EARLIEST recognized quoted/forwarded-digest boundary onward is
    discarded (Gmail forward marker, "On ... wrote:", Outlook original-
    message marker/header block, or a trailing "> " quoted block), and a
    trailing signature/footer is stripped when safely recognizable.

    Fails closed (ok=False) rather than guess whenever the boundary is
    genuinely ambiguous (interleaved "> " quoting) or nothing usable
    remains -- never partially applies a boundary it isn't confident
    about.
    """
    if not body or not body.strip():
        return ReplyIsolationResult(False, reason="message body is empty")

    boundary: int | None = None

    match = _GMAIL_FORWARD_MARKER.search(body)
    if match:
        boundary = _earliest(boundary, match.start())

    match = _GMAIL_ON_WROTE_PATTERN.search(body)
    if match:
        boundary = _earliest(boundary, match.start())

    match = _OUTLOOK_ORIGINAL_MARKER.search(body)
    if match:
        boundary = _earliest(boundary, match.start())

    match = _OUTLOOK_HEADER_BLOCK.search(body)
    if match:
        boundary = _earliest(boundary, match.start())

    # "> " quoting is only ever allowed as a clean TRAILING block from its
    # first occurrence to the end of the message -- interleaved quoting
    # (a non-quoted line reappearing after quoting has started) means the
    # boundary can't be trusted, so this fails closed rather than guess
    # which lines are Brad's.
    lines = body.splitlines()
    quote_start_line = next((i for i, line in enumerate(lines) if _QUOTE_LINE.match(line)), None)
    if quote_start_line is not None:
        tail = lines[quote_start_line:]
        if not all(_QUOTE_LINE.match(line) or not line.strip() for line in tail):
            return ReplyIsolationResult(
                False,
                reason='quoted ("> ") lines are interleaved with non-quoted lines; reply boundary is ambiguous',
            )
        quote_char_offset = sum(len(line) + 1 for line in lines[:quote_start_line])
        boundary = _earliest(boundary, quote_char_offset)

    reply_region = body[:boundary] if boundary is not None else body
    reply_region = _strip_signature(reply_region)
    reply_text = reply_region.strip()

    if not reply_text:
        return ReplyIsolationResult(
            False, reason="no reply text remained after removing quoted/forwarded content and signature"
        )

    return ReplyIsolationResult(True, reply_text=reply_text)


# --- verdict / header parsing -------------------------------------------------

_VERDICT_CANONICAL = {
    "strong like": "STRONG_LIKE",
    "like": "LIKE",
    "maybe": "MAYBE",
    "pass": "PASS",
    "strong pass": "STRONG_PASS",
}
# Longer phrases ("strong like") must be tried before their shorter
# substrings ("like") -- the alternation order below is deliberate.
_VERDICT_WORDS = r"strong\s+like|strong\s+pass|like|maybe|pass"

_BARE_VERDICT_LINE_PATTERN = re.compile(rf"^\s*({_VERDICT_WORDS})\s*[.:!]?\s*$", re.IGNORECASE)
_LABELED_VERDICT_LINE_PATTERN = re.compile(
    rf"^\s*(?P<label>.+?)\s*(?:--?|—|:)\s*(?P<verdict>{_VERDICT_WORDS})\s*[.:!]?\s*$", re.IGNORECASE
)

_LEADING_DOLLAR_PATTERN = re.compile(r"^\$")
_COMPANY_PUNCTUATION_PATTERN = re.compile(r"[^\w\s]")


def _canonical_verdict(text: str) -> str | None:
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    return _VERDICT_CANONICAL.get(normalized)


def normalize_ticker(raw: str | None) -> str | None:
    """Same rule as cli.py's digest-suppression ticker normalization
    (trim, uppercase, drop one optional leading "$"), implemented
    separately here to keep this module free of any cli.py import.
    """
    if not raw:
        return None
    normalized = _LEADING_DOLLAR_PATTERN.sub("", raw.strip()).upper()
    return normalized or None


def normalize_company_name(raw: str | None) -> str | None:
    """Same rule as cli.py's digest-suppression company normalization:
    lowercase, trim/collapse whitespace, strip basic punctuation only --
    never a fuzzy match.
    """
    if not raw:
        return None
    stripped_punctuation = _COMPANY_PUNCTUATION_PATTERN.sub("", raw)
    normalized = " ".join(stripped_punctuation.split()).lower()
    return normalized or None


@dataclass(frozen=True)
class DeliveryItem:
    """One idea from a specific digest delivery -- the "actual delivery
    items" allowed idea set a reply is matched against (task section 5).
    """

    source_id: int
    ticker: Optional[str]
    company: Optional[str]
    position: int


@dataclass(frozen=True)
class ParsedReplyBlock:
    item: DeliveryItem
    verdict: str
    comment: str


@dataclass(frozen=True)
class DigestReplyParseResult:
    ok: bool
    blocks: List[ParsedReplyBlock]
    reason: Optional[str] = None


def _resolve_delivery_item(label: str, delivery_items: List[DeliveryItem]) -> tuple[DeliveryItem | None, str | None]:
    """Exact normalized ticker match first; company-name fallback ONLY if
    no ticker match exists. Never fuzzy in either direction -- more than
    one match (which should not happen for a genuine digest, but is
    checked defensively) is treated the same as zero: ambiguous, refuse.
    """
    normalized_ticker = normalize_ticker(label)
    if normalized_ticker:
        ticker_matches = [
            item for item in delivery_items if item.ticker and normalize_ticker(item.ticker) == normalized_ticker
        ]
        if len(ticker_matches) == 1:
            return ticker_matches[0], None
        if len(ticker_matches) > 1:
            return None, f"more than one delivered idea matches ticker {label!r}"

    normalized_company = normalize_company_name(label)
    if normalized_company:
        company_matches = [
            item
            for item in delivery_items
            if item.company and normalize_company_name(item.company) == normalized_company
        ]
        if len(company_matches) == 1:
            return company_matches[0], None
        if len(company_matches) > 1:
            return None, f"more than one delivered idea matches company name {label!r}"

    return None, f"no delivered idea matches {label!r}"


def _parse_single_item_reply(
    reply_text: str, delivery_item: DeliveryItem
) -> tuple[ParsedReplyBlock | None, str | None]:
    lines = reply_text.splitlines()
    first_line_index = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first_line_index is None:
        return None, "reply text is empty after isolation"

    first_line = lines[first_line_index].strip()

    bare_match = _BARE_VERDICT_LINE_PATTERN.match(first_line)
    if bare_match:
        verdict = _canonical_verdict(bare_match.group(1))
        comment = "\n".join(lines[first_line_index + 1 :]).strip()
        return ParsedReplyBlock(item=delivery_item, verdict=verdict, comment=comment), None

    labeled_match = _LABELED_VERDICT_LINE_PATTERN.match(first_line)
    if labeled_match:
        label = labeled_match.group("label").strip()
        verdict = _canonical_verdict(labeled_match.group("verdict"))
        resolved_item, resolution_error = _resolve_delivery_item(label, [delivery_item])
        if resolved_item is None:
            return None, f"labeled header {label!r} did not match the single delivered idea ({resolution_error})"
        comment = "\n".join(lines[first_line_index + 1 :]).strip()
        return ParsedReplyBlock(item=resolved_item, verdict=verdict, comment=comment), None

    return None, "first non-empty line is not a supported verdict or 'TICKER — VERDICT' header"


def _parse_multi_item_reply(
    reply_text: str, delivery_items: List[DeliveryItem]
) -> tuple[List[ParsedReplyBlock], List[str]]:
    lines = reply_text.splitlines()
    header_indices: list[int] = []
    header_matches: list[re.Match] = []
    for i, line in enumerate(lines):
        match = _LABELED_VERDICT_LINE_PATTERN.match(line.strip())
        if match:
            header_indices.append(i)
            header_matches.append(match)

    if not header_indices:
        return [], ["no recognized 'TICKER — VERDICT' header found in a multi-idea digest reply"]

    blocks: list[ParsedReplyBlock] = []
    errors: list[str] = []
    for idx, (line_index, match) in enumerate(zip(header_indices, header_matches)):
        label = match.group("label").strip()
        verdict = _canonical_verdict(match.group("verdict"))
        resolved_item, resolution_error = _resolve_delivery_item(label, delivery_items)
        if resolved_item is None:
            errors.append(f"header {label!r} could not be unambiguously resolved ({resolution_error})")
            continue
        start = line_index + 1
        end = header_indices[idx + 1] if idx + 1 < len(header_indices) else len(lines)
        comment = "\n".join(lines[start:end]).strip()
        blocks.append(ParsedReplyBlock(item=resolved_item, verdict=verdict, comment=comment))

    if errors:
        # Fail the WHOLE reply, not just the offending block(s) -- a
        # partially-applied multi-idea reply risks silently dropping a
        # judgment Brad believed he gave, which is worse than a human
        # reviewing the whole message once.
        return [], errors

    return blocks, []


def parse_digest_reply(body: str | None, delivery_items: List[DeliveryItem]) -> DigestReplyParseResult:
    """Deterministically parses Brad's isolated reply text against the
    EXACT set of ideas that were actually delivered in the email he's
    replying to (never guessed from ticker text alone). Branches on
    len(delivery_items): a one-idea digest accepts a bare verdict line OR
    a labeled "TICKER — VERDICT" header; a multi-idea digest requires a
    labeled header for every block Brad responded to. Never creates a
    block for a delivered idea Brad didn't mention. Fails closed
    (ok=False) on any ambiguity -- ambiguous ticker/company resolution,
    an unrecognized verdict, or an unparseable reply structure -- rather
    than guess; callers must route a failure to NEEDS_REVIEW and must
    never create a feedback row for it.
    """
    isolation = isolate_reply_text(body)
    if not isolation.ok:
        return DigestReplyParseResult(False, [], reason=isolation.reason)

    if not delivery_items:
        return DigestReplyParseResult(False, [], reason="no delivery items to match against")

    if len(delivery_items) == 1:
        block, error = _parse_single_item_reply(isolation.reply_text, delivery_items[0])
        if block is None:
            return DigestReplyParseResult(False, [], reason=error)
        return DigestReplyParseResult(True, [block])

    blocks, errors = _parse_multi_item_reply(isolation.reply_text, delivery_items)
    if errors:
        return DigestReplyParseResult(False, [], reason="; ".join(errors))
    return DigestReplyParseResult(True, blocks)


# --- safety-net subject detection --------------------------------------------

_DIGEST_REPLY_SUBJECT_PATTERN = re.compile(
    r"^(?:re:\s*)+ideascout\s*[—-]\s*new ideas worth attention", re.IGNORECASE
)


def looks_like_digest_reply_subject(subject: str | None) -> bool:
    """Heuristic ONLY (task section 3's safety fallback): used when no
    delivery/thread mapping was found, to decide whether an unmatched
    message should fail closed into NEEDS_REVIEW instead of silently
    being processed as ordinary direct feedback. Never used to positively
    identify a genuine digest reply -- matching provider_thread_id
    against digest_deliveries is the only trusted signal for that.
    """
    if not subject:
        return False
    return bool(_DIGEST_REPLY_SUBJECT_PATTERN.match(subject.strip()))
