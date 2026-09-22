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

INLINE-PASTED-SOURCE FALLBACK (Stage 5.13 robustness fix): many real
Brad feedback emails have none of the above markers at all -- just a
verdict-like first line ("Like" / "Maybe" / "Strong Pass" / "Slight
Like" / ...), then Brad's own comment (one or more paragraphs), then the
pasted investment writeup itself with no boundary marker of any kind.
isolate_source() now tries this LOWEST-priority fallback only once every
marker/quote heuristic above has found nothing at all (never overriding
a higher-priority signal, and never applied when a quoted-reply
structure of any kind -- even an ambiguous/interleaved one -- is
present). It uses the ALREADY-PARSED feedback event's own user_comment
field ONLY as a whitespace-normalized delimiter to find where Brad's
comment ends -- never copying it into the returned source_text, and
never treating it as evidence.

PARAGRAPH-ALIGNMENT REFINEMENT (Stage 5.14): a real production
comparison across 10 historical inline-paste messages found the original
exact-whitespace-normalized-prefix match too brittle in two ways: (a)
the LLM parser sometimes echoes the verdict word itself at the start of
user_comment ("Like. Love the hidden asset angle...") even though the
verdict line was already removed from the raw body side, breaking an
exact prefix match; and (b) user_comment is sometimes a compressed
paraphrase of Brad's actual wording, not a verbatim copy, so it can
never match a raw substring at all. The fallback now (1) strips at most
one leading verdict-like phrase from user_comment before comparing, and
(2) instead of a single exact-prefix check, scores candidate boundaries
at REAL paragraph breaks (never an arbitrary character offset) using a
deterministic, token-level lexical/order-similarity metric
(_alignment_score, built on difflib.SequenceMatcher over word tokens --
never raw characters, since production diagnostics showed character-
level similarity was misleading for small paraphrases, and never an
LLM). A candidate boundary is accepted only when it clears ALL THREE of:
a minimum overall similarity, a minimum fraction of user_comment's own
tokens found in order in the candidate prefix ("comment coverage"), and
a bounded absolute count of prefix tokens NOT explained by user_comment
("unmatched prefix tokens") -- calibrated with real safety margin against
adversarial fixtures (see test_source_isolation.py and this module's
_MIN_ALIGNMENT_SIMILARITY/_MIN_COMMENT_COVERAGE/
_MAX_UNMATCHED_PREFIX_TOKENS). Candidates are tried at successively later
paragraph breaks (bounded by _MAX_LEADING_COMMENT_PARAGRAPHS, so this
never scans deep into a large document), and scanning STOPS at the first
paragraph that fails to clear the bar -- the furthest boundary that still
passed is used, so a short additional Brad paragraph the parser omitted
(observed in production) is conservatively folded into the excluded
region instead of leaking into source_text, but a boundary is never
allowed to "jump past" a failing candidate to reach some later,
coincidentally-matching one. source_text is always sliced from the
ORIGINAL raw body at the chosen paragraph boundary -- user_comment is
still only ever delimiter evidence, never copied into the result.

If no candidate boundary is confidently established, or a Gmail-style
quoted-reply header is present, or too little text remains after the
chosen boundary, this fails closed to UNSCORABLE_SOURCE exactly like
every other heuristic here -- it never guesses by paragraph count alone
or any other proxy. See isolate_source() and _inline_pasted_source_result()
below.
"""

from __future__ import annotations

import difflib
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

# A line that is JUST a verdict word (optionally qualified by "strong"/
# "slight") plus trailing punctuation -- never a full sentence that merely
# starts with one of these words (the trailing [.:!…\s]*$ anchor rules
# that out). Deliberately looser than parser.py's canonical VERDICTS enum
# (which has no "slight" variant) since this only needs to recognize the
# SHAPE of a verdict line, not validate it -- the real confirmation is the
# whitespace-normalized user_comment match that follows.
_VERDICT_LINE_PATTERN = re.compile(r"^(?:strong|slight)?\s*(?:like|maybe|pass)[.:!…\s]*$", re.IGNORECASE)

# Gmail's standard quoted-reply intro line (e.g. "On Mon, Jan 5, 2026 at
# 3:00 PM Brad <brad@example.com> wrote:"). Its presence anywhere in the
# body is treated as a strong signal that this is a reply-chain message,
# not a fresh inline paste -- the inline-paste fallback refuses to run at
# all rather than risk folding quoted history into "source."
_GMAIL_ON_WROTE_PATTERN = re.compile(r"^On .+ wrote:[ \t]*$", re.IGNORECASE | re.MULTILINE)

_WHITESPACE_RUN = re.compile(r"\s+")

# Same verdict vocabulary as _VERDICT_LINE_PATTERN, matched only at the
# very START of user_comment (Stage 5.14) -- the parser sometimes echoes
# the verdict word there even though it was already the raw body's own
# first line. Stripped at most once; normalization only, never applied to
# the raw body and never persisted anywhere.
_LEADING_VERDICT_PHRASE_PATTERN = re.compile(
    r"^(?:strong|slight)?\s*(?:like|maybe|pass)\b[.:;,!…]*\s*", re.IGNORECASE
)

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")

# Never scans arbitrarily deep into a long document looking for a
# coincidental match -- Brad's own leading commentary is realistically a
# handful of paragraphs at most, so candidate boundaries are only ever
# tried up to this many paragraphs in.
_MAX_LEADING_COMMENT_PARAGRAPHS = 4

# Calibrated against 5 realistic positive fixtures (verbatim, verdict-
# duplicated, and paraphrased user_comment, including a message with an
# extra short Brad paragraph the parser omitted) and adversarial negatives
# (generic-word overlap, a large unrelated intervening paragraph, and a
# wholly unrelated comment) -- see test_source_isolation.py's
# test_alignment_score_* tests for the exact numbers and margins. A
# candidate boundary must clear ALL THREE simultaneously; no single
# metric alone cleanly separates every case (in particular, a large
# unrelated paragraph can score deceptively well on similarity alone,
# which is exactly what _MIN_COMMENT_COVERAGE catches).
_MIN_ALIGNMENT_SIMILARITY = 0.60
_MIN_COMMENT_COVERAGE = 0.80
_MAX_UNMATCHED_PREFIX_TOKENS = 40

# A candidate source remainder must clear BOTH the existing character
# bound and a minimum token count -- catches a degenerate ~40-character
# remainder that isn't really prose (e.g. one long token or repeated
# punctuation).
_MIN_SOURCE_TOKENS = 8


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


def _tokenize(text: str) -> list[str]:
    """Lowercase word/number tokens only -- punctuation and whitespace
    differences ("self-help" vs "self help", "I like" vs "Likes", a
    trailing period) must never affect alignment confidence. Exposed
    (not double-underscore-mangled) so tests can measure it directly.
    """
    return _TOKEN_PATTERN.findall(text.lower())


def _strip_leading_verdict_phrase(comment: str) -> str:
    """Removes AT MOST ONE leading verdict-like phrase from an already-
    whitespace-stripped comment string (e.g. "Like. Love the hidden..."
    -> "Love the hidden..."). A no-op when no such phrase is present at
    the very start. Normalization only -- never applied to the raw body,
    never persisted.
    """
    return _LEADING_VERDICT_PHRASE_PATTERN.sub("", comment, count=1)


def _alignment_score(prefix_tokens: list[str], comment_tokens: list[str]) -> dict:
    """Deterministic, order-sensitive lexical similarity between one
    candidate prefix (Brad's own text up to a candidate paragraph
    boundary) and the parsed user_comment -- token-level, via
    difflib.SequenceMatcher (never raw characters: production diagnostics
    showed character-level similarity was misleading for small
    paraphrases; never an LLM). Exposed for direct testing/diagnostics.

    similarity: SequenceMatcher's own ratio() over the token sequences --
      2*matching / (len(prefix) + len(comment)), order-sensitive.
    comment_coverage: fraction of the COMMENT's own tokens found, in
      order, within the prefix -- guards specifically against a prefix
      that scores a deceptively reasonable overall `similarity` (e.g. a
      long unrelated paragraph) without actually containing most of what
      Brad's comment says.
    unmatched_prefix_tokens: count of prefix tokens NOT part of any
      match -- the "how much unexplained leading text is there" bound.
    """
    if not prefix_tokens or not comment_tokens:
        return {
            "similarity": 0.0,
            "comment_coverage": 0.0,
            "matching_tokens": 0,
            "unmatched_prefix_tokens": len(prefix_tokens),
        }
    matcher = difflib.SequenceMatcher(None, prefix_tokens, comment_tokens)
    matching_tokens = sum(block.size for block in matcher.get_matching_blocks())
    return {
        "similarity": matcher.ratio(),
        "comment_coverage": matching_tokens / len(comment_tokens),
        "matching_tokens": matching_tokens,
        "unmatched_prefix_tokens": len(prefix_tokens) - matching_tokens,
    }


def _alignment_is_confident(score: dict) -> bool:
    """All three of _MIN_ALIGNMENT_SIMILARITY/_MIN_COMMENT_COVERAGE/
    _MAX_UNMATCHED_PREFIX_TOKENS must hold at once -- see this module's
    docstring (Stage 5.14) for why no single metric alone is safe.
    """
    return (
        score["similarity"] >= _MIN_ALIGNMENT_SIMILARITY
        and score["comment_coverage"] >= _MIN_COMMENT_COVERAGE
        and score["unmatched_prefix_tokens"] <= _MAX_UNMATCHED_PREFIX_TOKENS
    )


def _split_into_paragraphs(text: str) -> list[tuple[int, int]]:
    """Splits `text` into paragraphs (maximal runs of non-blank lines),
    returning each paragraph's (start, end) character OFFSET in `text` --
    never a normalized/rewritten copy, so a chosen boundary can be sliced
    directly out of the ORIGINAL raw body. A paragraph's `end` excludes
    its own trailing blank-line separator; the NEXT paragraph's `start`
    is therefore always already past all of that blank space.
    """
    paragraphs: list[tuple[int, int]] = []
    para_start: int | None = None
    para_end = 0
    offset = 0
    for line in text.splitlines(keepends=True):
        if line.strip():
            if para_start is None:
                para_start = offset
            para_end = offset + len(line.rstrip("\r\n"))
        elif para_start is not None:
            paragraphs.append((para_start, para_end))
            para_start = None
        offset += len(line)
    if para_start is not None:
        paragraphs.append((para_start, para_end))
    return paragraphs


def _inline_pasted_source_result(body: str, user_comment: str | None):
    """Fallback-only heuristic (inline-pasted-source support, refined in
    Stage 5.14 -- see module docstring). Returns (result, decisive)
    exactly like _marker_result: decisive=False means the body's first
    non-empty line doesn't even look verdict-like, so the caller's
    generic "no boundary found" message applies instead of a more
    specific (but unearned) reason. Every other path is decisive: once a
    verdict-like line is recognized, this either confidently isolates a
    source or explains exactly why it refused to.

    user_comment is used ONLY to find where Brad's own comment ends --
    never copied into the returned source_text, never itself returned as
    or folded into evidence.
    """
    lines = body.splitlines()
    first_line_index = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first_line_index is None:
        return None, False

    if not _VERDICT_LINE_PATTERN.match(lines[first_line_index].strip()):
        return None, False

    if _GMAIL_ON_WROTE_PATTERN.search(body):
        return (
            IsolationResult(
                False,
                unscorable_reason="verdict-like first line found, but the message also contains a Gmail-style "
                "quoted-reply header ('On ... wrote:'); boundary is ambiguous",
            ),
            True,
        )

    raw_comment = (user_comment or "").strip()
    if not raw_comment:
        return (
            IsolationResult(
                False,
                unscorable_reason="verdict-like first line found but no parsed user_comment is available to "
                "anchor the boundary",
            ),
            True,
        )

    normalized_comment = _WHITESPACE_RUN.sub(
        " ", _strip_leading_verdict_phrase(raw_comment)
    ).strip()
    comment_tokens = _tokenize(normalized_comment)
    if not comment_tokens:
        return (
            IsolationResult(
                False,
                unscorable_reason="verdict-like first line found but parsed user_comment contained nothing "
                "beyond a verdict phrase to anchor the boundary",
            ),
            True,
        )

    remainder = "\n".join(lines[first_line_index + 1 :])
    paragraphs = _split_into_paragraphs(remainder)

    if len(paragraphs) < 2:
        return (
            IsolationResult(
                False,
                unscorable_reason="verdict-like first line found but no plausible paragraph boundary exists "
                "between Brad's comment and any remaining text",
            ),
            True,
        )

    best_boundary_offset: int | None = None
    max_candidates = min(len(paragraphs) - 1, _MAX_LEADING_COMMENT_PARAGRAPHS)
    for k in range(max_candidates):
        boundary_offset = paragraphs[k + 1][0]
        prefix_tokens = _tokenize(remainder[:boundary_offset])
        score = _alignment_score(prefix_tokens, comment_tokens)

        if score["unmatched_prefix_tokens"] > _MAX_UNMATCHED_PREFIX_TOKENS:
            # Genuinely unexplained/extraneous content has now appeared in
            # the prefix -- never jump PAST this to reach a later,
            # possibly coincidental match deeper in the document.
            break

        if _alignment_is_confident(score):
            # Prefer the FURTHEST defensible boundary (task requirement):
            # keep advancing while candidates keep passing, so a short
            # additional Brad paragraph the parser omitted is
            # conservatively folded into the excluded region rather than
            # leaking into source_text.
            best_boundary_offset = boundary_offset
        # else: nothing unexplained yet (unmatched is still bounded), but
        # coverage/similarity isn't sufficient at this k -- Brad's own
        # comment may simply span further paragraphs than considered so
        # far (a multi-paragraph comment matching multi-paragraph raw
        # text one-for-one). Keep scanning without recording this k as a
        # valid boundary; a later k may still legitimately pass once the
        # comment is fully consumed.

    if best_boundary_offset is None:
        return (
            IsolationResult(
                False,
                unscorable_reason="verdict-like first line found but Brad's parsed comment could not be "
                "confidently aligned with a real paragraph boundary that follows it",
            ),
            True,
        )

    source_text = remainder[best_boundary_offset:].lstrip()
    if len(source_text) < MIN_SOURCE_LENGTH or len(_tokenize(source_text)) < _MIN_SOURCE_TOKENS:
        return (
            IsolationResult(
                False,
                unscorable_reason="verdict-like first line and comment boundary aligned, but no substantial "
                "source text followed",
            ),
            True,
        )

    return IsolationResult(True, source_type="inline_pasted_source", source_text=source_text), True


def isolate_source(body: str | None, user_comment: str | None = None) -> IsolationResult:
    """Try each recognized boundary marker, most explicit first. The first
    marker TYPE that appears at all decides the outcome -- either a clean
    split (exactly one match, enough content after it) or an explicit
    UNSCORABLE_SOURCE (marker appears more than once, or too little
    content follows it). Lower-priority heuristics are only tried if a
    higher-priority marker type is entirely absent, never if it was
    present but ambiguous -- so an ambiguous strong signal is never
    silently overridden by a weaker one that happens to also match.

    user_comment (optional, Stage 5.13) is the ALREADY-PARSED feedback
    event's own comment text for this message -- used ONLY by the lowest-
    priority inline-pasted-source fallback below, ONLY as a delimiter, and
    NEVER included in or treated as source material. Every marker/quote
    check above it is completely unaffected by this parameter.
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

    # Lowest priority: no forwarded/quoted/explicit-SOURCE marker AND no
    # "> " quoted line anywhere at all -- only now is the inline-pasted-
    # source fallback tried (see _inline_pasted_source_result).
    result, decisive = _inline_pasted_source_result(body, user_comment)
    if decisive:
        return result

    return IsolationResult(
        False,
        unscorable_reason="no recognized forwarded-message, quoted-reply, or explicit SOURCE: boundary found",
    )
