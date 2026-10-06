"""Deterministic (NO LLM) parsing of Brad's "IdeaScout Blind Review" emails
(Stage 11): the CLEAN, blind judgments IdeaScout needs to evaluate Taste
v2 -- collected BEFORE Brad has seen any v2 prediction/reasons/concerns
for the exact idea he's judging.

Unlike digest_reply.py and screen_review.py, a BLIND_REVIEW judgment is a
genuine clean holdout judgment: feedback_origin='BLIND_REVIEW' is created
with holdout_eligible=1 (see cli.py's cmd_parse_mail and db.py's
Migration 14), so it counts toward the current v2 clean-holdout count
(db.get_eligible_feedback_after). This is the OPPOSITE structural
property from the other two deterministic email routes, which exist
precisely because their feedback is NOT a clean holdout judgment --
methodologically, this module is the reason the other two need that
distinction at all: without a way to also collect genuinely blind
judgments, there would be no way to ever legitimately unfreeze Taste v2.

This module NEVER calls an LLM anywhere, and the module that assigns
blind-review items (cli.py's cmd_blind_review) never shows Brad anything
derived from source_screenings before he submits a judgment -- see that
function's docstring for the display-side half of this guarantee. Every
block requires an explicit, unambiguous review reference ("BR-<assignment_
id>", see format_review_ref/parse_review_ref) resolving to EXACTLY one
blind_review_assignments row -- there is deliberately no ticker/company
fallback, exactly like screen_review.py: exact assignment identity matters
more than typing convenience. Any ambiguity, unknown reference, invalid
verdict, malformed header, or duplicate judgment of an already-judged
assignment fails the WHOLE message closed to NEEDS_REVIEW -- never a
partial ingest, never a guessed verdict.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import List, Optional

from . import db
from .digest_reply import VERDICT_WORDS, canonical_verdict, isolate_reply_text

PARSER_VERSION = "blind-review-v1"
MODEL_NAME = "deterministic"

# --- review reference: BR-<assignment_id> --------------------------------------
#
# blind_review_assignments.assignment_id is the exact identity of one
# blind-review exposure (see db.py's Migration 14 comment) -- this is a
# thin, human-typeable, publicly-safe display form of that key, never a
# second identity system. It contains no secrets and cannot silently
# start resolving to a different assignment later (SQLite AUTOINCREMENT
# never reuses/reassigns a primary key).

_REVIEW_REF_PATTERN = re.compile(r"^br-(\d+)$", re.IGNORECASE)


def format_review_ref(assignment_id: int) -> str:
    return f"BR-{assignment_id}"


def parse_review_ref(text: str) -> Optional[int]:
    match = _REVIEW_REF_PATTERN.match(text.strip())
    if not match:
        return None
    return int(match.group(1))


# --- verdict / header parsing -------------------------------------------------
#
# Same shape as screen_review.py's parser: a review reference is ALWAYS
# required (no bare-verdict shortcut, no ticker/company fallback), and a
# line that looks like an attempted verdict header but has no valid
# "BR-<digits>" reference is a malformed header, not silently absorbed as
# reasoning text -- it fails the whole message closed.

_BR_HEADER_ATTEMPT_PATTERN = re.compile(
    r"^\s*(?P<ref>br-\d+)\s*(?:--?|—|:)\s*(?P<verdict_text>.+?)\s*$", re.IGNORECASE
)
_GENERIC_LABELED_ATTEMPT_PATTERN = re.compile(
    rf"^\s*(?P<label>.+?)\s*(?:--?|—|:)\s*(?P<verdict>{VERDICT_WORDS})\s*[.:!]?\s*$", re.IGNORECASE
)
_BARE_VERDICT_ATTEMPT_PATTERN = re.compile(rf"^\s*({VERDICT_WORDS})\s*[.:!]?\s*$", re.IGNORECASE)
_LOOSE_BR_REF_PATTERN = re.compile(r"^\s*br-\d+\b", re.IGNORECASE)


@dataclass(frozen=True)
class BlindReviewBlock:
    assignment_row: sqlite3.Row
    verdict: str
    comment: str


@dataclass(frozen=True)
class BlindReviewParseResult:
    ok: bool
    blocks: List[BlindReviewBlock]
    reason: Optional[str] = None


def _find_valid_headers(lines: List[str]) -> tuple[list[tuple[int, str, str]], Optional[str]]:
    """Identical shape to screen_review.py's header scanner: returns
    (headers, None) on success, where headers is a list of (line_index,
    review_ref_text, canonical_verdict) tuples, or ([], reason) the
    instant any line looks like an attempted verdict header but is
    malformed (no valid ref, unrecognized verdict, ticker/company used
    instead of a ref).
    """
    headers: list[tuple[int, str, str]] = []
    for i, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped:
            continue

        br_attempt = _BR_HEADER_ATTEMPT_PATTERN.match(stripped)
        if br_attempt:
            verdict_text = br_attempt.group("verdict_text")
            verdict = canonical_verdict(verdict_text.rstrip(".:!"))
            if verdict is None:
                return [], (
                    f"line {i + 1}: {br_attempt.group('ref').upper()} has an unrecognized verdict "
                    f"{verdict_text!r} (valid: STRONG LIKE, LIKE, MAYBE, PASS, STRONG PASS)"
                )
            headers.append((i, br_attempt.group("ref"), verdict))
            continue

        if _LOOSE_BR_REF_PATTERN.match(stripped):
            return [], (
                f"line {i + 1} references a review reference but is not in the required "
                f"'BR-<id> — VERDICT' format: {stripped!r}"
            )

        if _GENERIC_LABELED_ATTEMPT_PATTERN.match(stripped) or _BARE_VERDICT_ATTEMPT_PATTERN.match(stripped):
            return [], (
                f"line {i + 1} looks like a review verdict but has no review reference (e.g. "
                f"'BR-123 — LIKE'); ticker/company-only feedback is not accepted for blind "
                f"reviews: {stripped!r}"
            )

    return headers, None


def _terminal_state_error(conn: sqlite3.Connection, assignment_row: sqlite3.Row) -> Optional[str]:
    """A blind-review assignment is TERMINAL once judged: judged_at is set
    only by db.insert_feedback_events in the same transaction that writes the
    accepted judgment. Terminality does not depend on the judgment's
    excluded_from_learning, holdout, or parse state -- exclusion changes how a
    judgment is used, never whether the assignment can be answered again.

    Legacy inconsistencies (judged_at and the judgment rows disagree) fail
    closed rather than creating an additional judgment.
    """
    assignment_id = assignment_row["assignment_id"]
    ref = format_review_ref(assignment_id)
    judged_at = assignment_row["judged_at"]
    judgment_rows = db.get_feedback_rows_for_blind_review_assignment(conn, assignment_id)

    if judged_at is None and judgment_rows:
        return (
            f"INCONSISTENT_BLIND_REVIEW_STATE: {ref} has a judgment row but is not marked judged; "
            "refusing to record another judgment"
        )
    if judged_at is not None and not judgment_rows:
        return (
            f"INCONSISTENT_BLIND_REVIEW_STATE: {ref} is marked judged at {judged_at} but has no judgment row; "
            "refusing to record another judgment"
        )
    if judged_at is not None:
        return (
            f"DUPLICATE_BLIND_REVIEW: {ref} was already judged at {judged_at}; "
            "a judged blind-review assignment is terminal and cannot be answered again"
        )
    return None


def parse_blind_review(conn: sqlite3.Connection, body: Optional[str]) -> BlindReviewParseResult:
    """Deterministically parses Brad's isolated blind-review text into one
    block per "BR-<id> — VERDICT" header, each resolved against the EXACT
    blind_review_assignments row it names (db.get_blind_review_assignment_
    by_id) -- never guessed from ticker/company text. Everything after a
    header, up to the next recognized header, is preserved VERBATIM as
    that block's comment.

    Fails the WHOLE message closed (ok=False) on: unparseable/ambiguous
    reply text, a malformed or ticker/company-only header, an unknown or
    duplicate review reference within the message, a review reference
    that doesn't resolve to any known assignment, or an assignment that
    has already been judged (judged_at set, whatever the prior judgment's
    exclusion state), or whose judged_at and judgment rows are inconsistent.
    Callers must route a failure to NEEDS_REVIEW and must never
    create a feedback row for any block of a failed message, and must
    NEVER reveal source_screenings content anywhere in that failure path.
    """
    isolation = isolate_reply_text(body)
    if not isolation.ok:
        return BlindReviewParseResult(False, [], reason=isolation.reason)

    lines = isolation.reply_text.splitlines()
    headers, error = _find_valid_headers(lines)
    if error is not None:
        return BlindReviewParseResult(False, [], reason=error)
    if not headers:
        return BlindReviewParseResult(
            False, [], reason="no recognized 'BR-<id> — VERDICT' review header found"
        )

    seen_assignment_ids: set[int] = set()
    blocks: list[BlindReviewBlock] = []
    errors: list[str] = []

    for idx, (line_index, ref_text, verdict) in enumerate(headers):
        assignment_id = parse_review_ref(ref_text)
        if assignment_id in seen_assignment_ids:
            errors.append(f"review reference {format_review_ref(assignment_id)} appears more than once")
            continue
        seen_assignment_ids.add(assignment_id)

        assignment_row = db.get_blind_review_assignment_by_id(conn, assignment_id)
        if assignment_row is None:
            errors.append(f"review reference {ref_text.upper()} does not resolve to any known assignment")
            continue

        terminal_error = _terminal_state_error(conn, assignment_row)
        if terminal_error is not None:
            errors.append(terminal_error)
            continue

        start = line_index + 1
        end = headers[idx + 1][0] if idx + 1 < len(headers) else len(lines)
        comment = "\n".join(lines[start:end]).strip()
        blocks.append(BlindReviewBlock(assignment_row=assignment_row, verdict=verdict, comment=comment))

    if errors:
        # Fail the WHOLE message, not just the offending block(s) -- see
        # digest_reply.py/screen_review.py's identical reasoning.
        return BlindReviewParseResult(False, [], reason="; ".join(errors))

    return BlindReviewParseResult(True, blocks)


# --- reveal-only categorical mapping (blind-review-results, AFTER judgment) ----
#
# A simple, documented, reveal-ONLY diagnostic -- never used for selection,
# eligibility, or anything before Brad submits a judgment. Buckets both
# Brad's human verdict and v2's categorical screen result into positive/
# neutral/negative so blind-review-results can show whether they broadly
# agreed, without pretending to a false precision the five-way/three-way
# vocabularies don't actually share.

_VERDICT_CATEGORY = {
    "STRONG_LIKE": "positive",
    "LIKE": "positive",
    "MAYBE": "neutral",
    "PASS": "negative",
    "STRONG_PASS": "negative",
}
_SCREEN_CATEGORY = {
    "INVESTIGATE_NOW": "positive",
    "WATCH": "neutral",
    "PASS": "negative",
}


def categorize_verdict(verdict: Optional[str]) -> Optional[str]:
    if verdict is None:
        return None
    return _VERDICT_CATEGORY.get(verdict)


def categorize_screen_prediction(overall_prediction: Optional[str]) -> Optional[str]:
    if overall_prediction is None:
        return None
    return _SCREEN_CATEGORY.get(overall_prediction)


def categorical_mapping_matches(verdict: Optional[str], overall_prediction: Optional[str]) -> Optional[bool]:
    """None means "not comparable" (e.g. no v2 screening exists yet for
    this source, or an unrecognized value) -- callers must not treat None
    as a mismatch.
    """
    verdict_category = categorize_verdict(verdict)
    prediction_category = categorize_screen_prediction(overall_prediction)
    if verdict_category is None or prediction_category is None:
        return None
    return verdict_category == prediction_category


# --- safety-net subject detection --------------------------------------------

_BLIND_REVIEW_SUBJECT_PATTERN = re.compile(
    r"^(?:(?:re|fwd|fw)\s*:\s*)*ideascout\s+blind\s+review\s*$", re.IGNORECASE
)


def looks_like_blind_review_subject(subject: Optional[str]) -> bool:
    """The ONLY signal used to route a message to this module -- same role
    as screen_review.looks_like_screen_review_subject. Accepts the exact
    subject "IdeaScout Blind Review", plus any number of leading
    Re:/Fwd:/Fw: prefixes a mail client may add.
    """
    if not subject:
        return False
    return bool(_BLIND_REVIEW_SUBJECT_PATTERN.match(subject.strip()))
