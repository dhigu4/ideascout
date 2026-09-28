"""Deterministic (NO LLM) parsing of Brad's "IdeaScout Screen Review"
emails (Stage 10): feedback given AFTER Brad has already seen one or more
of IdeaScout's own past screening results.

CRITICAL METHODOLOGICAL RULE, identical in spirit to digest_reply.py's:
a screen review is NOT a clean holdout judgment -- Brad has already seen
IdeaScout's screen result/rationale for the exact screening he's
reviewing. Feedback derived from a screen review is always created with
feedback_origin='SCREEN_REVIEW' and holdout_eligible=0 (see cli.py's
cmd_parse_mail and db.py's Migration 13) so it can still train a future
Taste build, but can never be miscounted as a genuine blind holdout
judgment or shadow-scored as one (db.get_eligible_feedback_after filters
on holdout_eligible).

This module NEVER calls an LLM anywhere. Every block requires an explicit,
unambiguous review reference ("SR-<screening_id>", see format_review_ref/
parse_review_ref) resolving to EXACTLY one source_screenings row -- there
is deliberately no ticker/company fallback here (unlike digest_reply.py):
exact screening provenance matters more than typing convenience, since a
ticker/company can have been screened more than once across taste
versions or content updates. Any ambiguity, unknown reference, invalid
verdict, malformed header, or duplicate review of the same screening
fails the WHOLE message closed to NEEDS_REVIEW -- never a partial ingest,
never a guessed verdict.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import List, Optional

from . import db
from .digest_reply import VERDICT_WORDS, canonical_verdict, isolate_reply_text

PARSER_VERSION = "screen-review-v1"
MODEL_NAME = "deterministic"

# --- review reference: SR-<screening_id> --------------------------------------
#
# source_screenings.screening_id is already the existing, durable, natural
# unique key for "one exact screening result" (see db.py's Migration 13
# comment) -- this is a thin, human-typeable, publicly-safe display form
# of that same key, never a second identity system. It contains no
# secrets and cannot silently start resolving to a different screening
# later, because screening_id is never reused or reassigned (SQLite
# AUTOINCREMENT) and source_screenings rows are never updated in place
# (see insert_source_screening's docstring).

_REVIEW_REF_PATTERN = re.compile(r"^sr-(\d+)$", re.IGNORECASE)


def format_review_ref(screening_id: int) -> str:
    return f"SR-{screening_id}"


def parse_review_ref(text: str) -> Optional[int]:
    match = _REVIEW_REF_PATTERN.match(text.strip())
    if not match:
        return None
    return int(match.group(1))


# --- verdict / header parsing -------------------------------------------------
#
# Unlike digest_reply.py's format, a review reference is ALWAYS required --
# there is no single-item bare-verdict shortcut and no ticker/company
# fallback (task requirement: "Do NOT accept ticker-only or company-only
# SCREEN_REVIEW feedback"). A line that looks like an attempted verdict
# header but has no valid "SR-<digits>" reference is treated as a
# malformed header, not silently absorbed as reasoning text, and fails the
# whole message closed.

_SR_HEADER_ATTEMPT_PATTERN = re.compile(
    r"^\s*(?P<ref>sr-\d+)\s*(?:--?|—|:)\s*(?P<verdict_text>.+?)\s*$", re.IGNORECASE
)
_GENERIC_LABELED_ATTEMPT_PATTERN = re.compile(
    rf"^\s*(?P<label>.+?)\s*(?:--?|—|:)\s*(?P<verdict>{VERDICT_WORDS})\s*[.:!]?\s*$", re.IGNORECASE
)
_BARE_VERDICT_ATTEMPT_PATTERN = re.compile(rf"^\s*({VERDICT_WORDS})\s*[.:!]?\s*$", re.IGNORECASE)
_LOOSE_SR_REF_PATTERN = re.compile(r"^\s*sr-\d+\b", re.IGNORECASE)


@dataclass(frozen=True)
class ScreenReviewBlock:
    screening_row: sqlite3.Row
    verdict: str
    comment: str


@dataclass(frozen=True)
class ScreenReviewParseResult:
    ok: bool
    blocks: List[ScreenReviewBlock]
    reason: Optional[str] = None


def _find_valid_headers(lines: List[str]) -> tuple[list[tuple[int, str, str]], Optional[str]]:
    """Scans every non-empty line for a valid "SR-<id> — VERDICT" header.

    Returns (headers, None) on success, where headers is a list of
    (line_index, review_ref_text, canonical_verdict) tuples -- or
    ([], reason) the instant any line looks like an attempted verdict
    header but is malformed in some way (no valid ref, unrecognized
    verdict, ticker/company used instead of a ref). This whole-message
    fail-closed behavior is deliberate: a malformed block anywhere means
    the whole email is rejected, never partially ingested.
    """
    headers: list[tuple[int, str, str]] = []
    for i, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped:
            continue

        sr_attempt = _SR_HEADER_ATTEMPT_PATTERN.match(stripped)
        if sr_attempt:
            verdict_text = sr_attempt.group("verdict_text")
            verdict = canonical_verdict(verdict_text.rstrip(".:!"))
            if verdict is None:
                return [], (
                    f"line {i + 1}: {sr_attempt.group('ref').upper()} has an unrecognized verdict "
                    f"{verdict_text!r} (valid: STRONG LIKE, LIKE, MAYBE, PASS, STRONG PASS)"
                )
            headers.append((i, sr_attempt.group("ref"), verdict))
            continue

        if _LOOSE_SR_REF_PATTERN.match(stripped):
            return [], (
                f"line {i + 1} references a review reference but is not in the required "
                f"'SR-<id> — VERDICT' format: {stripped!r}"
            )

        if _GENERIC_LABELED_ATTEMPT_PATTERN.match(stripped) or _BARE_VERDICT_ATTEMPT_PATTERN.match(stripped):
            return [], (
                f"line {i + 1} looks like a review verdict but has no review reference (e.g. "
                f"'SR-12345 — LIKE'); ticker/company-only feedback is not accepted for screen "
                f"reviews: {stripped!r}"
            )

    return headers, None


def parse_screen_review(conn: sqlite3.Connection, body: Optional[str]) -> ScreenReviewParseResult:
    """Deterministically parses Brad's isolated review text into one block
    per "SR-<id> — VERDICT" header, each resolved against the EXACT
    source_screenings row it names (db.get_source_screening_by_id) -- never
    guessed from ticker/company text. Everything after a header, up to the
    next recognized header, is preserved VERBATIM as that block's comment.

    Fails the WHOLE message closed (ok=False) on: unparseable/ambiguous
    reply text, a malformed or ticker/company-only header, an unknown or
    duplicate review reference within the message, a review reference that
    doesn't resolve to any known screening, or a screening that already
    has a learning-eligible SCREEN_REVIEW judgment from a prior message.
    Callers must route a failure to NEEDS_REVIEW and must never create a
    feedback row for any block of a failed message.
    """
    isolation = isolate_reply_text(body)
    if not isolation.ok:
        return ScreenReviewParseResult(False, [], reason=isolation.reason)

    lines = isolation.reply_text.splitlines()
    headers, error = _find_valid_headers(lines)
    if error is not None:
        return ScreenReviewParseResult(False, [], reason=error)
    if not headers:
        return ScreenReviewParseResult(
            False, [], reason="no recognized 'SR-<id> — VERDICT' review header found"
        )

    seen_screening_ids: set[int] = set()
    blocks: list[ScreenReviewBlock] = []
    errors: list[str] = []

    for idx, (line_index, ref_text, verdict) in enumerate(headers):
        screening_id = parse_review_ref(ref_text)
        if screening_id in seen_screening_ids:
            errors.append(f"review reference {format_review_ref(screening_id)} appears more than once")
            continue
        seen_screening_ids.add(screening_id)

        screening_row = db.get_source_screening_by_id(conn, screening_id)
        if screening_row is None:
            errors.append(f"review reference {ref_text.upper()} does not resolve to any known screening")
            continue

        existing = db.get_learning_eligible_screen_review_feedback_for_screening(conn, screening_id)
        if existing is not None:
            errors.append(
                f"DUPLICATE_SCREEN_REVIEW: {format_review_ref(screening_id)} already has a "
                "learning-eligible screen-review judgment"
            )
            continue

        start = line_index + 1
        end = headers[idx + 1][0] if idx + 1 < len(headers) else len(lines)
        comment = "\n".join(lines[start:end]).strip()
        blocks.append(ScreenReviewBlock(screening_row=screening_row, verdict=verdict, comment=comment))

    if errors:
        # Fail the WHOLE message, not just the offending block(s) -- see
        # digest_reply.py's identical reasoning: a partially-applied
        # review risks silently dropping a judgment Brad believed he gave.
        return ScreenReviewParseResult(False, [], reason="; ".join(errors))

    return ScreenReviewParseResult(True, blocks)


# --- safety-net subject detection --------------------------------------------

_SCREEN_REVIEW_SUBJECT_PATTERN = re.compile(
    r"^(?:(?:re|fwd|fw)\s*:\s*)*ideascout\s+screen\s+review\s*$", re.IGNORECASE
)


def looks_like_screen_review_subject(subject: Optional[str]) -> bool:
    """The ONLY signal used to route a message to this module -- a screen
    review is not tied to any single digest delivery/thread, so (unlike
    digest_reply.py's thread-id-based primary signal) subject text is the
    primary and only routing signal here. Accepts the exact subject
    "IdeaScout Screen Review", plus any number of leading Re:/Fwd:/Fw:
    reply/forward prefixes a mail client may add.
    """
    if not subject:
        return False
    return bool(_SCREEN_REVIEW_SUBJECT_PATTERN.match(subject.strip()))
