"""Deterministic, local (no LLM, no API call) separation of "source
material" -- a forwarded writeup, article, or quoted original email --
from Brad's own commentary in a raw AgentMail message body.

This is Level 0 of Stage 4's cost-control architecture: pure string/regex
logic, free to run on every message. It is deliberately conservative: if a
message's structure doesn't contain one of the recognized, unambiguous
boundary markers below -- exactly once -- this refuses to guess and
reports UNSCORABLE_SOURCE instead, with a specific reason. Guessing wrong
here would let Brad's own opinion leak into what is supposed to be a
blind, out-of-sample prediction -- worse than simply skipping the message.

KNOWN LIMITATION: once a boundary is found, everything from that point to
the end of the message body is treated as source material. A message
where Brad adds a postscript AFTER the forwarded/quoted content (rather
than only before it) would have that postscript incorrectly folded into
the "source." This is an accepted v1 simplification, not a leakage risk
in practice (postscripts observed so far are placement, not editorial
opinion), but worth revisiting if it turns out to matter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MIN_SOURCE_LENGTH = 40

_EXPLICIT_SOURCE_MARKER = re.compile(r"^[ \t]*SOURCE[ \t]*:[ \t]*$", re.IGNORECASE | re.MULTILINE)
_GMAIL_FORWARD_MARKER = re.compile(r"-{2,}[ \t]*Forwarded message[ \t]*-{2,}", re.IGNORECASE)
_OUTLOOK_ORIGINAL_MARKER = re.compile(r"-{2,}[ \t]*Original Message[ \t]*-{2,}", re.IGNORECASE)
_OUTLOOK_HEADER_BLOCK = re.compile(
    r"^From:.*\n(?:Sent|Date):.*\n(?:To:.*\n)?Subject:.*$", re.IGNORECASE | re.MULTILINE
)
_QUOTE_LINE = re.compile(r"^[ \t]*>")


@dataclass(frozen=True)
class IsolationResult:
    scorable: bool
    source_type: str | None = None
    source_text: str | None = None
    unscorable_reason: str | None = None


def _marker_result(body: str, pattern: re.Pattern, source_type: str, include_match: bool):
    """Returns (result, decisive). decisive=False means this marker type
    found nothing and the next heuristic should be tried; decisive=True
    means this marker type settled the question one way or the other
    (either a clean split, or an unresolvable ambiguity) and nothing else
    should be tried.
    """
    matches = list(pattern.finditer(body))
    if not matches:
        return None, False
    if len(matches) > 1:
        return (
            IsolationResult(False, unscorable_reason=f"multiple {source_type} markers found; boundary is ambiguous"),
            True,
        )
    match = matches[0]
    offset = match.start() if include_match else match.end()
    source_text = body[offset:].strip()
    if len(source_text) < MIN_SOURCE_LENGTH:
        return (
            IsolationResult(False, unscorable_reason=f"{source_type} marker found but no substantive content followed it"),
            True,
        )
    return IsolationResult(True, source_type=source_type, source_text=source_text), True


def isolate_source(body: str | None) -> IsolationResult:
    """Try each recognized boundary marker, most explicit first. The first
    marker TYPE that appears at all decides the outcome -- either a clean
    split (exactly one match, enough content after it) or an explicit
    UNSCORABLE_SOURCE (marker appears more than once, or too little
    content follows it). Lower-priority heuristics are only tried if a
    higher-priority marker type is entirely absent, never if it was
    present but ambiguous -- so an ambiguous strong signal is never
    silently overridden by a weaker one that happens to also match.
    """
    if not body or not body.strip():
        return IsolationResult(False, unscorable_reason="message body is empty")

    for pattern, source_type, include_match in (
        (_EXPLICIT_SOURCE_MARKER, "explicit_source_section", False),
        (_GMAIL_FORWARD_MARKER, "forwarded_email", True),
        (_OUTLOOK_ORIGINAL_MARKER, "forwarded_email", True),
        (_OUTLOOK_HEADER_BLOCK, "forwarded_email", True),
    ):
        result, decisive = _marker_result(body, pattern, source_type, include_match)
        if decisive:
            return result

    # Trailing quoted-reply block: Brad's own commentary (if any) comes
    # first, then quoting ("> ") runs uninterrupted to the end.
    lines = body.splitlines()
    quote_start = next((i for i, line in enumerate(lines) if _QUOTE_LINE.match(line)), None)
    if quote_start is not None:
        tail = lines[quote_start:]
        if all(_QUOTE_LINE.match(line) or not line.strip() for line in tail):
            source_text = "\n".join(_QUOTE_LINE.sub("", line, count=1).strip() for line in tail).strip()
            if len(source_text) >= MIN_SOURCE_LENGTH:
                return IsolationResult(True, source_type="quoted_original", source_text=source_text)
            return IsolationResult(
                False, unscorable_reason="quoted block found but no substantive content after removing quote markers"
            )
        return IsolationResult(
            False, unscorable_reason='quoted ("> ") lines are interleaved with non-quoted lines; boundary is ambiguous'
        )

    return IsolationResult(
        False,
        unscorable_reason="no recognized forwarded-message, quoted-reply, or explicit SOURCE: boundary found",
    )
