"""Unit tests for the taste-generation truncation-detection and retry
logic (ideascout/taste.py). These call taste.generate_idea_taste_body and
taste.generate_candidate_rules directly against a fake Anthropic client --
no cli.py, no database, no real LLM.

Context: a real production build-taste run failed with
"Invalid JSON: EOF while parsing a string" while generating
candidate-permanent-rules.md. Root cause: client.messages.parse()'s
output_format convenience validates the response text internally
(pydantic.TypeAdapter.validate_json) and raises immediately if the model's
JSON was truncated by max_tokens -- with no way for the caller to inspect
the raw response's stop_reason first. taste.py now uses plain
client.messages.create() with a hand-built structured-output schema
instead, specifically so stop_reason can be checked before any parsing is
attempted.
"""

from __future__ import annotations

import json
import logging

import pytest

from ideascout import taste


class FakeTextBlock:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class FakeResponse:
    def __init__(self, stop_reason: str = "end_turn", text: str = ""):
        self.stop_reason = stop_reason
        self.content = [FakeTextBlock(text)]


class ScriptedMessages:
    """A fake `client.messages` whose .create() returns responses from a
    fixed script, one per call, and records every call it received (so
    tests can assert on call count and on exactly what prompt was sent).
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("ScriptedMessages ran out of scripted responses")
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeClient:
    def __init__(self, responses):
        self.messages = ScriptedMessages(responses)


def valid_batch_json(n: int = 1) -> str:
    return json.dumps(
        {
            "rules": [
                {
                    "rule": f"Rule {i}",
                    "evidence": f"Evidence {i}",
                    "reusability": f"Reuse {i}",
                    "confidence": "LOW",
                }
                for i in range(n)
            ]
        }
    )


SAMPLE_RECORDS = [
    {
        "feedback_id": 1,
        "event_type": "FEEDBACK",
        "verdict": "LIKE",
        "ticker": "XYZ",
        "company": None,
        "user_comment": "Hidden asset is interesting.",
        "positive_reasons": [],
        "concerns": [],
        "not_a_concern": [],
    }
]


# --- candidate rules: truncation ---------------------------------------------


def test_candidate_rules_max_tokens_stop_reason_is_treated_as_retryable_truncation():
    client = FakeClient([FakeResponse("max_tokens", "")] * taste.MAX_GENERATION_ATTEMPTS)

    with pytest.raises(taste.TasteGenerationError) as exc_info:
        taste.generate_candidate_rules(client, "fake-model", SAMPLE_RECORDS)

    assert "truncat" in str(exc_info.value).lower()
    assert len(client.messages.calls) == taste.MAX_GENERATION_ATTEMPTS


def test_candidate_rules_truncated_mid_string_with_normal_stop_reason_is_retried():
    """Even without an explicit max_tokens stop_reason, genuinely
    incomplete/malformed JSON (e.g. cut off mid-string) must be caught by
    schema validation and retried -- not silently accepted as valid.
    """
    truncated = '{"rules": [{"rule": "Good idea but the reasoning cuts off mid'
    client = FakeClient([FakeResponse("end_turn", truncated)] * taste.MAX_GENERATION_ATTEMPTS)

    with pytest.raises(taste.TasteGenerationError):
        taste.generate_candidate_rules(client, "fake-model", SAMPLE_RECORDS)

    assert len(client.messages.calls) == taste.MAX_GENERATION_ATTEMPTS


def test_candidate_rules_logs_a_clear_truncation_reason_and_retries(caplog):
    logger = logging.getLogger("test-taste-retry-truncation")
    client = FakeClient([FakeResponse("max_tokens", ""), FakeResponse("end_turn", valid_batch_json(1))])

    with caplog.at_level(logging.WARNING, logger="test-taste-retry-truncation"):
        rules = taste.generate_candidate_rules(client, "fake-model", SAMPLE_RECORDS, logger=logger)

    assert len(rules) == 1
    messages = [record.message for record in caplog.records]
    assert any("truncated at output limit" in message for message in messages)
    assert any("retrying" in message for message in messages)


# --- candidate rules: retry succeeding / exhausting --------------------------


def test_candidate_rules_first_attempt_malformed_second_succeeds():
    responses = [
        FakeResponse("end_turn", '{"rules": [{"rule": "cut off'),  # malformed JSON
        FakeResponse("end_turn", valid_batch_json(2)),
    ]
    client = FakeClient(responses)

    rules = taste.generate_candidate_rules(client, "fake-model", SAMPLE_RECORDS)

    assert len(rules) == 2
    assert len(client.messages.calls) == 2


def test_candidate_rules_all_retry_attempts_fail_raises_clearly():
    client = FakeClient([FakeResponse("end_turn", "not json at all")] * taste.MAX_GENERATION_ATTEMPTS)

    with pytest.raises(taste.TasteGenerationError) as exc_info:
        taste.generate_candidate_rules(client, "fake-model", SAMPLE_RECORDS)

    assert len(client.messages.calls) == taste.MAX_GENERATION_ATTEMPTS
    assert str(taste.MAX_GENERATION_ATTEMPTS) in str(exc_info.value)


def test_candidate_rules_never_repairs_malformed_json_with_string_hacks():
    """A response that is *almost* valid JSON (e.g. missing a closing
    brace) must be rejected and retried, never silently patched up.
    """
    almost_valid = valid_batch_json(1)[:-1]  # drop the final closing brace
    responses = [FakeResponse("end_turn", almost_valid), FakeResponse("end_turn", valid_batch_json(1))]
    client = FakeClient(responses)

    rules = taste.generate_candidate_rules(client, "fake-model", SAMPLE_RECORDS)

    assert len(rules) == 1  # only the genuinely valid second response was used
    assert len(client.messages.calls) == 2


# --- retry prompts: same training set, concise reminder ---------------------


def test_retry_prompt_adds_concise_reminder_without_changing_training_corpus():
    responses = [FakeResponse("max_tokens", ""), FakeResponse("end_turn", valid_batch_json(1))]
    client = FakeClient(responses)

    taste.generate_candidate_rules(client, "fake-model", SAMPLE_RECORDS)

    first_content = client.messages.calls[0]["messages"][0]["content"]
    second_content = client.messages.calls[1]["messages"][0]["content"]

    assert "IMPORTANT" not in first_content
    assert "concise" in second_content.lower()
    # The underlying training corpus (same feedback, same records) is
    # identical in both -- only the reminder is appended.
    assert first_content == second_content[: len(first_content)]
    assert "XYZ" in first_content and "XYZ" in second_content


def test_candidate_rules_uses_same_max_five_cap_after_a_retry():
    responses = [
        FakeResponse("max_tokens", ""),
        FakeResponse("end_turn", valid_batch_json(7)),  # would-be 7, must still cap at 5
    ]
    client = FakeClient(responses)

    rules = taste.generate_candidate_rules(client, "fake-model", SAMPLE_RECORDS)
    assert len(rules) == taste.MAX_CANDIDATE_RULES == 5


# --- idea-taste body: truncation / retry (same mechanism, free text) -------


def test_idea_taste_body_truncated_is_retried_and_succeeds():
    responses = [
        FakeResponse("max_tokens", ""),
        FakeResponse("end_turn", "## Strong Positive Signals\nA real signal.\n"),
    ]
    client = FakeClient(responses)

    body = taste.generate_idea_taste_body(client, "fake-model", SAMPLE_RECORDS)

    assert "A real signal." in body
    assert len(client.messages.calls) == 2


def test_idea_taste_body_all_retries_truncated_fails_clearly():
    client = FakeClient([FakeResponse("max_tokens", "")] * taste.MAX_GENERATION_ATTEMPTS)

    with pytest.raises(taste.TasteGenerationError) as exc_info:
        taste.generate_idea_taste_body(client, "fake-model", SAMPLE_RECORDS)

    assert "truncat" in str(exc_info.value).lower()
    assert len(client.messages.calls) == taste.MAX_GENERATION_ATTEMPTS


# --- genuine API failures are not retried (no point retrying the same error)


def test_candidate_rules_genuine_api_error_is_not_retried():
    client = FakeClient([RuntimeError("simulated network failure")])

    with pytest.raises(taste.TasteGenerationError):
        taste.generate_candidate_rules(client, "fake-model", SAMPLE_RECORDS)

    assert len(client.messages.calls) == 1  # not retried -- a real API failure, not truncation/malformed JSON
