"""Unit tests for ideascout/evaluation.py -- deliberately just the mapping
primitives (Stage 4 explicitly defers a real scoring metric).
"""

from __future__ import annotations

from ideascout import evaluation


def test_like_verdicts_map_to_investigate_now():
    assert evaluation.map_verdict_to_expected_prediction("STRONG_LIKE") == "INVESTIGATE_NOW"
    assert evaluation.map_verdict_to_expected_prediction("LIKE") == "INVESTIGATE_NOW"


def test_maybe_maps_to_watch():
    assert evaluation.map_verdict_to_expected_prediction("MAYBE") == "WATCH"


def test_pass_verdicts_map_to_pass():
    assert evaluation.map_verdict_to_expected_prediction("PASS") == "PASS"
    assert evaluation.map_verdict_to_expected_prediction("STRONG_PASS") == "PASS"


def test_none_verdict_has_no_mapping():
    assert evaluation.map_verdict_to_expected_prediction(None) is None


def test_feedback_with_a_mapped_verdict_is_eligible():
    assert evaluation.is_eligible_for_categorical_evaluation("FEEDBACK", "LIKE") is True
    assert evaluation.is_eligible_for_categorical_evaluation("FEEDBACK", "STRONG_PASS") is True


def test_new_idea_is_never_forced_into_categorical_evaluation():
    assert evaluation.is_eligible_for_categorical_evaluation("NEW_IDEA", None) is False


def test_missed_idea_is_never_forced_into_categorical_evaluation():
    assert evaluation.is_eligible_for_categorical_evaluation("MISSED_IDEA", None) is False


def test_unclear_event_is_never_eligible():
    assert evaluation.is_eligible_for_categorical_evaluation("UNCLEAR", None) is False


def test_feedback_with_no_verdict_is_not_eligible():
    """Should never happen in practice (a PARSED FEEDBACK row always has a
    verdict -- see parser.determine_review_reason), but the function must
    not crash or silently invent a mapping if it ever does.
    """
    assert evaluation.is_eligible_for_categorical_evaluation("FEEDBACK", None) is False
