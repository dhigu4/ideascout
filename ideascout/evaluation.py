"""Evaluation mapping only -- deliberately NOT a scoring/accuracy engine.

Stage 4 explicitly defers building a sophisticated metric; this module is
just the one piece of logic an eventual evaluation needs and that is worth
getting right and testing now: how Brad's categorical feedback vocabulary
maps onto the shadow-prediction vocabulary, and which feedback is even
eligible for that categorical comparison at all.
"""

from __future__ import annotations

# STRONG_LIKE/LIKE both map to INVESTIGATE_NOW, and PASS/STRONG_PASS both
# map to PASS: Brad's vocabulary has an intensity distinction the
# shadow-prediction vocabulary does not attempt to capture (see Stage 4
# spec section 8) -- collapsing them here is deliberate, not a loss of
# information anywhere else (feedback.verdict itself is untouched).
VERDICT_TO_EXPECTED_PREDICTION = {
    "STRONG_LIKE": "INVESTIGATE_NOW",
    "LIKE": "INVESTIGATE_NOW",
    "MAYBE": "WATCH",
    "PASS": "PASS",
    "STRONG_PASS": "PASS",
}


def map_verdict_to_expected_prediction(verdict: str | None) -> str | None:
    """Returns the shadow-prediction category Brad's verdict should be
    compared against, or None if there is no defined mapping (e.g. verdict
    is None, or some future vocabulary addition this hasn't been taught
    about yet).
    """
    if verdict is None:
        return None
    return VERDICT_TO_EXPECTED_PREDICTION.get(verdict)


def is_eligible_for_categorical_evaluation(event_type: str, verdict: str | None) -> bool:
    """NEW_IDEA and MISSED_IDEA are event types, not verdicts -- they carry
    no STRONG_LIKE..STRONG_PASS judgment at all, so they must never be
    forced into this categorical comparison (they may still be analyzed
    qualitatively later; see the Stage 4 spec). Only a FEEDBACK event with
    a verdict in the mapping above is eligible.
    """
    return event_type == "FEEDBACK" and verdict in VERDICT_TO_EXPECTED_PREDICTION
