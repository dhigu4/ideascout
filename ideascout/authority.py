"""Authority hierarchy for screening and taste building, plus a deterministic
check that a generated Taste artifact has not hardened a numeric preference
into a permanent threshold.

Order of authority, highest first:

  1. IDEA_SCREEN_RULES.md -- permanent, Brad-approved rules. Authoritative.
  2. The current inferred Taste artifact -- a secondary, regenerable summary.
  3. The idea/source material -- the subject of each decision, not a rule source.

Taste may refine preferences inside the permanent rules. It may not contradict
them, harden a numeric threshold, weaken a permanent exclusion, invent a
permanent criterion, or turn an observed tendency into a categorical rule.

The prompt blocks below are plain text with no format placeholders, so they can
be concatenated into any prompt. Keep them free of "{" and "}".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

AUTHORITY_ORDER = (
    "1. IDEA_SCREEN_RULES.md (permanent, Brad-approved rules) -- authoritative",
    "2. the current inferred Taste artifact -- secondary, inferred, regenerable",
    "3. the idea/source material -- the subject of the decision, not a rule source",
)

SCREENING_AUTHORITY_BLOCK = """\
AUTHORITY (applies to every screening decision):
1. The permanent screening rules above are authoritative. They are Brad-approved and change only when Brad changes them.
2. The taste profile is a secondary, inferred summary of Brad's past reactions. It may refine preferences inside the permanent rules. It never overrides them.
3. If the taste profile conflicts with the permanent rules, the permanent rules win.
4. The canonical upside standard is in the permanent rules: a plausible path to greater than 100%+ upside over a multi-year period. Apply that standard as written.
5. A multiple such as 3x may appear in the taste profile only as an inferred preference or an attractive characteristic. It is NOT a hurdle, floor, or minimum. Do not describe 3x as Brad's hurdle, effective hurdle, required hurdle, or minimum unless the permanent rules state it.
6. An idea must not be PASSed merely because it falls short of a 3x preference, if it otherwise satisfies the permanent rules.
7. Do not cite an inferred preference as though Brad formally adopted it. When relying on the taste profile, say that it suggests something.
8. Permanent exclusions in the rules apply regardless of anything the taste profile says.
"""

TASTE_AUTHORITY_BLOCK = """\
AUTHORITY AND PERMANENT RULES (read this before writing anything):
The user message contains Brad's PERMANENT SCREENING RULES, which are Brad-approved and authoritative. Your summary is an inferred, secondary description of his taste. Authority order: permanent rules first, your inferred summary second, the idea material last.

Your summary may refine preferences inside the permanent rules. It must NOT:
- contradict a permanent rule;
- turn a numeric threshold from the rules into a harder one (the rules set the upside standard as a plausible path to greater than 100%+ upside over a multi-year period; do not write that Brad requires a 3x, a minimum multiple, or any other floor the rules do not state);
- weaken a permanent exclusion;
- invent a permanent screening criterion;
- turn an observed tendency into a categorical rule.

Separate the two kinds of statement. Brad-approved rules are restated only as the rules themselves. Inferred tendencies are written as tendencies, with calibrated wording. If feedback suggests that more upside is better, phrase that as a preference (for example: "appears to prefer larger upside; several judgments found 3x-style multiples attractive"). Never phrase it as a minimum, a hurdle, or a requirement.

Do not invent permanent rules from small-sample correlations. Preserve context and exceptions. Taste is an inferred summary, not a rulebook.
"""

CANDIDATE_RULES_AUTHORITY_BLOCK = """\
AUTHORITY AND PERMANENT RULES:
The user message contains Brad's PERMANENT SCREENING RULES, which are Brad-approved and authoritative. Do not propose a candidate rule that duplicates, hardens, weakens, or contradicts a permanent rule. A proposal that would turn a preference into a numeric minimum or hurdle must be reworded as a preference, or omitted. Candidate rules are proposals for Brad only; they never override the permanent rules.
"""

_NEGATION = re.compile(r"\b(?:not|no|never|isn'?t|aren'?t|without|nor|neither|rather\s+than|unless)\b", re.IGNORECASE)

# Each pattern captures the specific number it is keying off, in group 1 (except
# "an effective hurdle", which has none -- that exact framing is rejected
# regardless of the number, since the permanent rules never describe the upside
# standard as a "hurdle" at all). `unit` says which canonical set (see
# _extract_canonical_thresholds) that captured number is checked against; None
# means the match is never exempt. The gap between a keyword and its number
# deliberately excludes digits ([^.\n;0-9]), so a keyword can only reach the
# number it actually governs -- not skip over an intervening, unrelated number
# (e.g. a faithfully-restated ">100%") to reach a second, later one (e.g. an
# incidental "3x" mention elsewhere in the same sentence).
_HARD_THRESHOLD_PATTERNS = (
    (
        "a multiple stated as a required minimum or floor",
        re.compile(
            r"\b(?:requires?|required|must|minimum|min\.?|floor|at\s+least|no\s+less\s+than|mandatory|"
            r"non-?negotiable|hard|strict|absolute)\b[^.\n;0-9]{0,40}?\b(\d+(?:\.\d+)?)\s*x\b",
            re.IGNORECASE,
        ),
        "x",
    ),
    (
        "a multiple stated as a hurdle, floor, or threshold",
        re.compile(
            r"\b(\d+(?:\.\d+)?)\s*x\s+(?:hurdle|floor|minimum|threshold|requirement|cut-?off|bar)\b",
            re.IGNORECASE,
        ),
        "x",
    ),
    (
        "an effective hurdle",
        re.compile(r"\beffective(?:ly)?\s+(?:\d+(?:\.\d+)?\s*x\s+)?hurdle\b", re.IGNORECASE),
        None,
    ),
    (
        "a percentage stated as a required minimum, floor, or threshold",
        re.compile(
            r"\b(?:requires?|required|must|minimum|min\.?|floor|hurdle|threshold|at\s+least|no\s+less\s+than|"
            r"hard|strict|absolute|mandatory)\b[^.\n;0-9]{0,40}?\b(\d+(?:\.\d+)?)\s*%",
            re.IGNORECASE,
        ),
        "%",
    ),
    (
        "a percentage stated as a hurdle, floor, or threshold",
        re.compile(
            r"\b(\d+(?:\.\d+)?)\s*%[^.\n;0-9]{0,40}?\b(?:hurdle|floor|minimum|threshold|requirement|cut-?off)\b",
            re.IGNORECASE,
        ),
        "%",
    ),
)


@dataclass(frozen=True)
class AuthorityConflict:
    line_number: int
    excerpt: str
    reason: str


def _extract_canonical_thresholds(permanent_rules_text: str) -> dict[str, set[float]]:
    """Numeric thresholds that already appear in the permanent screening rules
    (IDEA_SCREEN_RULES.md), as plain text extraction -- no interpretation of
    meaning, no LLM. A generated Taste statement that restates one of these
    exact numbers, in the same unit, is a faithful restatement of an EXISTING
    canonical rule, not an invented or hardened one. Covers percentages
    (including an IRR percentage, if the rules ever state one -- IRR numbers
    are just percentages) and x-multiples, should the rules ever state one.
    """
    percentages = {float(m) for m in re.findall(r"(\d+(?:\.\d+)?)\s*%", permanent_rules_text)}
    multiples = {float(m) for m in re.findall(r"(\d+(?:\.\d+)?)\s*x\b", permanent_rules_text, re.IGNORECASE)}
    return {"%": percentages, "x": multiples}


def find_hard_threshold_conflicts(text: str, permanent_rules_text: str = "") -> list[AuthorityConflict]:
    """Deterministic, line-level check for generated Taste text that turns a
    numeric preference (such as a 3x multiple) into a hard threshold.

    Two kinds of match are never flagged:
    - one preceded by a negation within the same clause (e.g. "does not
      require 3x") -- a benign statement, not a hard threshold;
    - one whose captured number is a FAITHFUL RESTATEMENT of a threshold that
      already appears in `permanent_rules_text`, in the same unit (e.g. "the
      permanent rule requires >100% upside" when the rules say ">100%+") --
      allowed because it doesn't introduce, raise, or harden anything. A line
      restating a canonical threshold still fails if it ALSO introduces a
      different, non-canonical hard threshold (e.g. "...>100%, but require
      3x" -- the 3x half is still flagged, since 3x is not in the rules).

    Pass `permanent_rules_text=""` (the default) to disable the canonical
    exception entirely and flag every numeric hard-threshold pattern, which is
    useful for testing the patterns in isolation. One finding per line per
    reason. No LLM is involved, and nothing is ever rewritten.
    """
    canonical = _extract_canonical_thresholds(permanent_rules_text)
    findings: list[AuthorityConflict] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for reason, pattern, unit in _HARD_THRESHOLD_PATTERNS:
            for match in pattern.finditer(line):
                prefix = line[max(0, match.start() - 50): match.start()]
                if _NEGATION.search(prefix):
                    continue
                captured = match.group(1) if match.lastindex else None
                if unit is not None and captured is not None and float(captured) in canonical.get(unit, set()):
                    continue
                findings.append(AuthorityConflict(line_number, line.strip()[:160], reason))
                break
    return findings
