"""Unit tests for ideascout/shadow.py (Level 2). Fake Anthropic client
only -- no real LLM, no DB.
"""

from __future__ import annotations

import json

import pytest

from ideascout import shadow, structured_llm


class _FakeTextBlock:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class _FakeResponse:
    def __init__(self, stop_reason: str = "end_turn", text: str = ""):
        self.stop_reason = stop_reason
        self.content = [_FakeTextBlock(text)]


class _ScriptedMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("ran out of scripted responses")
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _ScriptedClient:
    def __init__(self, responses):
        self.messages = _ScriptedMessages(responses)


def _valid_prediction_json(**overrides) -> str:
    data = {
        "overall_prediction": "WATCH",
        "mispricing": "Plausible",
        "variant_perception": "Plausible",
        "upside": "Potentially sufficient",
        "business_quality": "Plausible",
        "downside": "Acceptable",
        "key_reasons": ["Hidden earnings power."],
        "key_concerns": ["Execution risk."],
        "critical_questions": ["When does the segment breakeven?"],
        "confidence": "MEDIUM",
    }
    data.update(overrides)
    return json.dumps(data)


# idea_records is a real table with no verdict/comment columns at all --
# this dict stands in for a sqlite3.Row with exactly those (and only
# those) compact fields, which is the actual structural guarantee against
# leakage: there is nothing to accidentally include.
IDEA_RECORD = {
    "company": "XYZ Corp",
    "ticker": "XYZ",
    "source_title": "XYZ Corp writeup",
    "source_date": "2026-01-05",
    "business_summary": "A widget maker.",
    "core_thesis": "Hidden earnings power.",
    "why_mispriced": "Loss-making segment obscures true earnings.",
    "future_earnings_change": "Segment breakeven expected 2027.",
    "upside_case": "3-4x.",
    "downside_or_key_risks": "Execution risk.",
    "catalysts": "Segment divestiture.",
    "what_must_be_true": "Segment losses must actually stop.",
    "evidence_of_market_misunderstanding": "Sell-side models consolidated losses as permanent.",
    "known_unknowns": "Exact divestiture timeline.",
}

IDEA_TASTE_BODY = "## Strong Positive Signals\nHidden earnings power is the most consistent theme.\n"
SCREEN_RULES_BODY = "1. Mispricing\n   Is there a specific reason the market may be wrong?\n"


def test_screen_idea_prompt_contains_only_taste_rules_and_compact_idea_fields():
    client = _ScriptedClient([_FakeResponse("end_turn", _valid_prediction_json())])

    shadow.screen_idea(
        client, "fake-model", idea_taste_body=IDEA_TASTE_BODY, screen_rules_body=SCREEN_RULES_BODY,
        idea_record=IDEA_RECORD,
    )

    sent = client.messages.calls[0]
    system_prompt = sent["system"]
    user_content = sent["messages"][0]["content"]

    assert IDEA_TASTE_BODY in system_prompt
    assert SCREEN_RULES_BODY in system_prompt
    assert "Hidden earnings power" in user_content  # from the compact idea record
    # No leakage channel exists: screen_idea's signature has no parameter
    # for a verdict, a comment, or candidate-permanent-rules content. The
    # actual idea payload sent to the model (user_content) is what must be
    # clean -- the system prompt's own fixed instructional template
    # legitimately uses words like "verdict" to describe the test itself.
    for leak in ("STRONG_LIKE", "LIKE", "MAYBE", "PASS", "verdict", "user_comment", "candidate"):
        assert leak.lower() not in user_content.lower()


def test_format_idea_record_for_screening_only_uses_compact_fields():
    formatted = shadow.format_idea_record_for_screening(IDEA_RECORD)
    assert "Hidden earnings power" in formatted
    assert "3-4x" in formatted
    # Every line comes from the fixed field list -- nothing else could
    # have been included even if IDEA_RECORD carried extra keys.
    assert formatted.count("\n") == len(shadow._IDEA_RECORD_FIELDS) - 1


def test_screen_idea_returns_categorical_fields_no_numeric_score():
    client = _ScriptedClient([_FakeResponse("end_turn", _valid_prediction_json(overall_prediction="INVESTIGATE_NOW"))])

    result = shadow.screen_idea(
        client, "fake-model", idea_taste_body=IDEA_TASTE_BODY, screen_rules_body=SCREEN_RULES_BODY,
        idea_record=IDEA_RECORD,
    )

    assert result.overall_prediction == "INVESTIGATE_NOW"
    assert result.confidence in ("HIGH", "MEDIUM", "LOW")
    assert isinstance(result.key_reasons, list)


def test_screen_idea_rejects_invalid_categorical_value():
    """The pydantic schema is the enforcement mechanism against fake
    precision / off-vocabulary values -- an invalid label is malformed
    output, retried like any other schema violation.
    """
    client = _ScriptedClient(
        [
            _FakeResponse("end_turn", _valid_prediction_json(overall_prediction="87")),
            _FakeResponse("end_turn", _valid_prediction_json()),
        ]
    )

    result = shadow.screen_idea(
        client, "fake-model", idea_taste_body=IDEA_TASTE_BODY, screen_rules_body=SCREEN_RULES_BODY,
        idea_record=IDEA_RECORD,
    )
    assert result.overall_prediction == "WATCH"
    assert len(client.messages.calls) == 2


def test_screen_idea_retries_on_truncation_then_succeeds():
    client = _ScriptedClient(
        [
            _FakeResponse("max_tokens", ""),
            _FakeResponse("end_turn", _valid_prediction_json()),
        ]
    )

    result = shadow.screen_idea(
        client, "fake-model", idea_taste_body=IDEA_TASTE_BODY, screen_rules_body=SCREEN_RULES_BODY,
        idea_record=IDEA_RECORD,
    )
    assert result.overall_prediction == "WATCH"
    assert len(client.messages.calls) == 2


def test_screen_idea_exhausts_retries_and_raises_clearly():
    client = _ScriptedClient([_FakeResponse("max_tokens", "")] * shadow.MAX_ATTEMPTS)

    with pytest.raises(structured_llm.StructuredGenerationError):
        shadow.screen_idea(
            client, "fake-model", idea_taste_body=IDEA_TASTE_BODY, screen_rules_body=SCREEN_RULES_BODY,
            idea_record=IDEA_RECORD,
        )
    assert len(client.messages.calls) == shadow.MAX_ATTEMPTS
