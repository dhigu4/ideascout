"""Deterministic narrative-completeness contract for source screenings.

A WATCH or INVESTIGATE_NOW screening is only useful to Brad if it explains
itself: at least one substantive reason (key_reasons), one substantive
concern (key_concerns), and one substantive question (critical_questions).
The screening model's schema permits all three to be empty, and a real
production screening (ITECH.ST) came back as WATCH with every narrative
list empty -- so this module is the single shared rule that decides whether
such a screening may be repaired, suppressed from the digest, or shown as
complete.

Pure functions only: no database, no LLM, no I/O. PASS may stay sparse, and
INSUFFICIENT_INFORMATION is never affected by this contract.
"""

from __future__ import annotations

from typing import Iterable, Optional

NARRATIVE_REQUIRED_PREDICTIONS = frozenset({"WATCH", "INVESTIGATE_NOW"})
NARRATIVE_FIELDS = ("key_reasons", "key_concerns", "critical_questions")

# Placeholder text that carries no substance. Compared after lowercasing and
# stripping trailing punctuation, so "N/A." and "none" both count as missing.
_PLACEHOLDER_TEXTS = frozenset(
    {
        "",
        "n/a",
        "na",
        "none",
        "none given",
        "none noted",
        "not stated",
        "unknown",
        "tbd",
        "-",
        "--",
        "...",
        "(none)",
        "(none noted)",
    }
)


def substantive_items(items: Optional[Iterable]) -> list[str]:
    """The items that actually carry content: non-string, blank,
    whitespace-only, placeholder-only, and punctuation-only entries are all
    dropped. Returns stripped text, never invented or rewritten text.
    """
    if not items:
        return []
    result: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text:
            continue
        if text.lower().rstrip(".:;!,") in _PLACEHOLDER_TEXTS:
            continue
        if not any(character.isalnum() for character in text):
            continue
        result.append(text)
    return result


def narrative_gaps(
    overall_prediction: str,
    key_reasons: Optional[Iterable],
    key_concerns: Optional[Iterable],
    critical_questions: Optional[Iterable],
) -> list[str]:
    """Names of the narrative fields still missing for this prediction.
    Always empty for PASS / INSUFFICIENT_INFORMATION. Each required field is
    checked independently: all three must have at least one substantive item.
    """
    if overall_prediction not in NARRATIVE_REQUIRED_PREDICTIONS:
        return []
    gaps: list[str] = []
    if not substantive_items(key_reasons):
        gaps.append("key_reasons")
    if not substantive_items(key_concerns):
        gaps.append("key_concerns")
    if not substantive_items(critical_questions):
        gaps.append("critical_questions")
    return gaps


def merge_narrative(
    original: dict[str, list[str]],
    repair: Optional[dict[str, list[str]]],
) -> dict[str, list[str]]:
    """Effective narrative after an optional repair. Per field: a substantive
    original value is ALWAYS kept untouched (a repair can never overwrite it);
    only a field that was missing originally may be filled from the repair.
    The result is always the cleaned, substantive view of each field.
    """
    merged: dict[str, list[str]] = {}
    for field in NARRATIVE_FIELDS:
        kept = substantive_items(original.get(field))
        if kept:
            merged[field] = kept
            continue
        merged[field] = substantive_items((repair or {}).get(field))
    return merged
