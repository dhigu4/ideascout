"""Unit tests for ideascout/idea_extraction.py (Level 1). Fake Anthropic
client only -- no real LLM, no DB.
"""

from __future__ import annotations

import json

import pytest

from ideascout import idea_extraction, structured_llm


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


def _valid_extraction_json(**overrides) -> str:
    data = {
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
    data.update(overrides)
    return json.dumps(data)


SOURCE_TEXT = "XYZ Corp trades at 5x normalized earnings due to a temporary loss-making segment."


def test_extract_idea_only_sends_source_text_never_anything_else():
    client = _ScriptedClient([_FakeResponse("end_turn", _valid_extraction_json())])

    idea_extraction.extract_idea(client, "fake-model", SOURCE_TEXT)

    sent = client.messages.calls[0]
    user_message = sent["messages"][0]["content"]
    assert SOURCE_TEXT in user_message
    # No leakage channel exists: extract_idea's signature only accepts
    # source_text -- there is no parameter through which a verdict,
    # comment, or candidate-permanent-rules content could even be passed.
    assert "STRONG_LIKE" not in user_message
    assert "verdict" not in user_message.lower()


def test_extract_idea_returns_fields_from_source_only():
    client = _ScriptedClient([_FakeResponse("end_turn", _valid_extraction_json(ticker="ABC"))])

    result = idea_extraction.extract_idea(client, "fake-model", SOURCE_TEXT)

    assert result.ticker == "ABC"
    assert result.core_thesis == "Hidden earnings power."


def test_extract_idea_leaves_unstated_fields_null():
    client = _ScriptedClient([_FakeResponse("end_turn", _valid_extraction_json(company=None, ticker=None))])

    result = idea_extraction.extract_idea(client, "fake-model", SOURCE_TEXT)

    assert result.company is None
    assert result.ticker is None


def test_extract_idea_retries_on_truncation_then_succeeds():
    client = _ScriptedClient(
        [
            _FakeResponse("max_tokens", ""),
            _FakeResponse("end_turn", _valid_extraction_json()),
        ]
    )

    result = idea_extraction.extract_idea(client, "fake-model", SOURCE_TEXT)

    assert result.company == "XYZ Corp"
    assert len(client.messages.calls) == 2


def test_extract_idea_exhausts_retries_and_raises_clearly():
    client = _ScriptedClient([_FakeResponse("max_tokens", "")] * idea_extraction.MAX_ATTEMPTS)

    with pytest.raises(structured_llm.StructuredGenerationError):
        idea_extraction.extract_idea(client, "fake-model", SOURCE_TEXT)

    assert len(client.messages.calls) == idea_extraction.MAX_ATTEMPTS


def test_extract_idea_genuine_api_error_is_not_retried():
    client = _ScriptedClient([RuntimeError("simulated network failure")])

    with pytest.raises(structured_llm.StructuredGenerationError):
        idea_extraction.extract_idea(client, "fake-model", SOURCE_TEXT)

    assert len(client.messages.calls) == 1
