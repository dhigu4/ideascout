"""Unit tests for ideascout/structured_llm.py's schema-complexity-error
handling (Stage 5.3). Generic tests only -- structured_llm.py must never
be special-cased for any one caller (Yellowbrick, email, or otherwise).
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import BaseModel

from ideascout import structured_llm


def _fake_bad_request_error(message: str):
    import anthropic

    response = httpx.Response(
        400,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        json={"error": {"type": "invalid_request_error", "message": message}},
    )
    return anthropic.BadRequestError(message, response=response, body=None)


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


class _SimpleModel(BaseModel):
    a: str
    b: str


def test_is_schema_complexity_error_matches_the_observed_message():
    exc = _fake_bad_request_error("Schema is too complex.")
    assert structured_llm._is_schema_complexity_error(exc) is True


def test_is_schema_complexity_error_case_insensitive():
    exc = _fake_bad_request_error("SCHEMA IS TOO COMPLEX")
    assert structured_llm._is_schema_complexity_error(exc) is True


def test_is_schema_complexity_error_false_for_unrelated_bad_request():
    exc = _fake_bad_request_error("max_tokens must be greater than 0")
    assert structured_llm._is_schema_complexity_error(exc) is False


def test_is_schema_complexity_error_false_for_non_api_exceptions():
    assert structured_llm._is_schema_complexity_error(RuntimeError("Schema is too complex")) is False
    assert structured_llm._is_schema_complexity_error(ValueError("boom")) is False


def test_schema_complexity_error_is_not_retried():
    """The real fix is simplifying the schema, not retrying -- a rejected
    schema is rejected every time, so retrying would just waste calls.
    """
    client = _ScriptedClient([_fake_bad_request_error("Schema is too complex.")])

    with pytest.raises(structured_llm.StructuredGenerationError) as exc_info:
        structured_llm.generate_structured(
            client,
            model_name="fake-model",
            system_prompt="system",
            user_content="content",
            output_model=_SimpleModel,
            max_output_tokens=100,
            max_attempts=3,
            label="a test call",
        )

    assert len(client.messages.calls) == 1  # never retried
    assert "too complex" in str(exc_info.value).lower()
    assert "schema" in str(exc_info.value).lower()


def test_schema_complexity_error_message_explains_it_is_a_config_problem():
    client = _ScriptedClient([_fake_bad_request_error("Schema is too complex.")])

    with pytest.raises(structured_llm.StructuredGenerationError) as exc_info:
        structured_llm.generate_structured(
            client,
            model_name="fake-model",
            system_prompt="system",
            user_content="content",
            output_model=_SimpleModel,
            max_output_tokens=100,
            max_attempts=3,
            label="a test call",
        )

    message = str(exc_info.value).lower()
    assert "not a retryable" in message or "not retryable" in message or "configuration" in message


def test_other_bad_request_errors_still_raise_clearly_without_retry():
    """Only the specific schema-complexity shape gets special treatment --
    every other genuine API error still fails immediately, exactly as
    before (this was already true; this test just pins it down).
    """
    client = _ScriptedClient([_fake_bad_request_error("model not found")])

    with pytest.raises(structured_llm.StructuredGenerationError):
        structured_llm.generate_structured(
            client,
            model_name="fake-model",
            system_prompt="system",
            user_content="content",
            output_model=_SimpleModel,
            max_output_tokens=100,
            max_attempts=3,
            label="a test call",
        )

    assert len(client.messages.calls) == 1


# --- truncation-aware retry escalation (Stage 5.13) --------------------------


def test_normal_compact_response_succeeds_on_first_attempt_with_base_token_budget():
    client = _ScriptedClient([_FakeResponse("end_turn", '{"a": "1", "b": "2"}')])

    result = structured_llm.generate_structured(
        client, model_name="fake-model", system_prompt="system", user_content="content",
        output_model=_SimpleModel, max_output_tokens=100, max_attempts=3, label="a test call",
    )

    assert result.a == "1"
    assert len(client.messages.calls) == 1
    assert client.messages.calls[0]["max_tokens"] == 100


def test_truncation_then_retry_uses_larger_token_budget_and_stricter_reminder():
    client = _ScriptedClient(
        [
            _FakeResponse("max_tokens", ""),
            _FakeResponse("end_turn", '{"a": "1", "b": "2"}'),
        ]
    )

    result = structured_llm.generate_structured(
        client, model_name="fake-model", system_prompt="system", user_content="content",
        output_model=_SimpleModel, max_output_tokens=100, max_attempts=3, label="a test call",
    )

    assert result.a == "1"
    assert len(client.messages.calls) == 2
    assert client.messages.calls[0]["max_tokens"] == 100
    # Modestly larger, never the same, never absurdly huge.
    retry_tokens = client.messages.calls[1]["max_tokens"]
    assert retry_tokens > 100
    assert retry_tokens == int(100 * structured_llm.TRUNCATION_RETRY_TOKEN_MULTIPLIER)

    retry_content = client.messages.calls[1]["messages"][0]["content"]
    assert "truncat" in retry_content.lower()
    assert "SIGNIFICANTLY more concise" in retry_content


def test_repeated_truncation_fails_safely_with_no_partial_result():
    client = _ScriptedClient([_FakeResponse("max_tokens", "")] * 3)

    with pytest.raises(structured_llm.StructuredGenerationError) as exc_info:
        structured_llm.generate_structured(
            client, model_name="fake-model", system_prompt="system", user_content="content",
            output_model=_SimpleModel, max_output_tokens=100, max_attempts=3, label="a test call",
        )

    assert len(client.messages.calls) == 3
    assert "truncat" in str(exc_info.value).lower()
    # Never a growing-forever budget -- capped by the fixed multiplier
    # applied to the ORIGINAL base each time, not compounded attempt over
    # attempt.
    assert client.messages.calls[1]["max_tokens"] == client.messages.calls[2]["max_tokens"]


def test_malformed_json_retry_does_not_escalate_tokens_or_use_truncation_reminder():
    """A validation failure is a DIFFERENT failure mode from truncation --
    it must keep using the base token budget and the existing generic
    reminder, never the truncation-specific one.
    """
    client = _ScriptedClient(
        [
            _FakeResponse("end_turn", '{"a": "1"'),  # malformed/incomplete JSON, NOT truncated
            _FakeResponse("end_turn", '{"a": "1", "b": "2"}'),
        ]
    )

    result = structured_llm.generate_structured(
        client, model_name="fake-model", system_prompt="system", user_content="content",
        output_model=_SimpleModel, max_output_tokens=100, max_attempts=3, label="a test call",
    )

    assert result.a == "1"
    assert client.messages.calls[1]["max_tokens"] == 100  # unchanged
    retry_content = client.messages.calls[1]["messages"][0]["content"]
    assert "SIGNIFICANTLY more concise" not in retry_content
    assert "did not fit in the available output space" in retry_content


def test_truncation_recovering_into_malformed_json_does_not_keep_escalating():
    """After a truncation-caused retry SUCCEEDS in getting past max_tokens
    but still returns malformed JSON, the NEXT retry must fall back to the
    base token budget and the generic reminder -- escalation is tied to
    the immediately preceding failure's cause, not sticky for the rest of
    the call.
    """
    client = _ScriptedClient(
        [
            _FakeResponse("max_tokens", ""),
            _FakeResponse("end_turn", '{"a": "1"'),  # not truncated this time, just malformed
            _FakeResponse("end_turn", '{"a": "1", "b": "2"}'),
        ]
    )

    result = structured_llm.generate_structured(
        client, model_name="fake-model", system_prompt="system", user_content="content",
        output_model=_SimpleModel, max_output_tokens=100, max_attempts=3, label="a test call",
    )

    assert result.a == "1"
    assert len(client.messages.calls) == 3
    assert client.messages.calls[1]["max_tokens"] == int(100 * structured_llm.TRUNCATION_RETRY_TOKEN_MULTIPLIER)
    assert client.messages.calls[2]["max_tokens"] == 100  # back to base -- attempt 2 wasn't truncated


def test_generic_success_path_unaffected_by_the_schema_complexity_check():
    client = _ScriptedClient([_FakeResponse("end_turn", '{"a": "1", "b": "2"}')])

    result = structured_llm.generate_structured(
        client,
        model_name="fake-model",
        system_prompt="system",
        user_content="content",
        output_model=_SimpleModel,
        max_output_tokens=100,
        max_attempts=3,
        label="a test call",
    )

    assert result.a == "1"
    assert result.b == "2"
