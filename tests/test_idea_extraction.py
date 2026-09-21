"""Unit tests for ideascout/idea_extraction.py (Level 1). Fake Anthropic
client only -- no real LLM, no DB, no production data.

Stage 5.3 context: a real production extraction call failed with
Anthropic's 400 "Schema is too complex" (see structured_llm.py's
_is_schema_complexity_error and this file's schema-shape test below). The
root cause was ExtractedIdea's fields all carrying Python-side defaults
(None for 4 Optional[str] fields, "" for the other 10) -- pydantic then
never emits a "required" array at all, and the 4 Optional fields become
anyOf-nullable unions. ExtractedIdea is now exactly 12 required plain str
fields (source_title/source_date were dropped -- see cli.py, which now
carries a source's existing title/date through extraction unchanged
instead of sourcing them from this model).
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


EXPECTED_FIELDS = [
    "company",
    "ticker",
    "business_summary",
    "core_thesis",
    "why_mispriced",
    "future_earnings_change",
    "upside_case",
    "downside_or_key_risks",
    "catalysts",
    "what_must_be_true",
    "evidence_of_market_misunderstanding",
    "known_unknowns",
]


def _valid_extraction_json(**overrides) -> str:
    data = {
        "company": "XYZ Corp",
        "ticker": "XYZ",
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


# --- schema shape: the actual regression guard for "Schema is too complex" --


def test_extracted_idea_schema_is_a_single_flat_object_of_required_strings():
    """Fails if someone later reintroduces the exact shape that caused
    the real production "Schema is too complex" failure: a default value
    (which drops a field out of "required"), an Optional[str]/`str | None`
    (an anyOf nullable union), a list, or a nested model.
    """
    from anthropic.lib._parse._transform import transform_schema

    schema = transform_schema(idea_extraction.ExtractedIdea)

    assert schema["type"] == "object"
    assert "$defs" not in schema
    assert "$ref" not in schema
    assert "anyOf" not in schema
    assert "oneOf" not in schema
    assert "allOf" not in schema

    properties = schema["properties"]
    assert set(properties.keys()) == set(EXPECTED_FIELDS)

    # Every field is required -- no field was dropped out of "required"
    # by carrying a Python-side default.
    assert set(schema["required"]) == set(EXPECTED_FIELDS)

    for field_name, field_schema in properties.items():
        assert field_schema["type"] == "string", f"{field_name} is not a plain string"
        assert "anyOf" not in field_schema, f"{field_name} has a nullable/union anyOf"
        assert "oneOf" not in field_schema, f"{field_name} has a oneOf"
        assert "allOf" not in field_schema, f"{field_name} has an allOf"
        assert "items" not in field_schema, f"{field_name} looks like an array"
        assert "properties" not in field_schema, f"{field_name} looks like a nested object"
        assert "$ref" not in field_schema, f"{field_name} references another schema"

    assert schema["additionalProperties"] is False


def test_extracted_idea_has_no_pydantic_defaults():
    """The direct cause of the missing "required" array: any field with a
    default silently drops out of it. Checking model_fields directly
    (rather than only the derived JSON schema) catches the root cause,
    not just its symptom.
    """
    for field_name, field_info in idea_extraction.ExtractedIdea.model_fields.items():
        assert field_info.is_required(), f"{field_name} has a default and is not required"


def test_extracted_idea_construction_requires_every_field():
    with pytest.raises(Exception):
        idea_extraction.ExtractedIdea(company="X")  # missing 11 required fields


# --- 12 fields still parse / blank values accepted, never invented ----------


def test_all_twelve_fields_parse():
    sent_values = json.loads(_valid_extraction_json())
    client = _ScriptedClient([_FakeResponse("end_turn", _valid_extraction_json())])

    result = idea_extraction.extract_idea(client, "fake-model", SOURCE_TEXT)

    assert set(EXPECTED_FIELDS) == set(sent_values.keys())
    for field_name in EXPECTED_FIELDS:
        assert getattr(result, field_name) == sent_values[field_name]


def test_extract_idea_accepts_blank_company_and_ticker_as_not_stated():
    """Blank string -- not null -- is how "not stated in the source" is
    represented now; the model must accept this without complaint, and
    must not invent a company/ticker just to avoid returning blank.
    """
    client = _ScriptedClient([_FakeResponse("end_turn", _valid_extraction_json(company="", ticker=""))])

    result = idea_extraction.extract_idea(client, "fake-model", SOURCE_TEXT)

    assert result.company == ""
    assert result.ticker == ""


def test_extract_idea_rejects_null_for_a_required_string_field_and_retries():
    """A response using JSON null for company/ticker (the OLD, no-longer-
    valid shape) is malformed against the new required-string schema --
    it must be treated like any other schema-validation failure (retried),
    never silently accepted.
    """
    responses = [
        _FakeResponse("end_turn", _valid_extraction_json(company=None, ticker=None)),
        _FakeResponse("end_turn", _valid_extraction_json()),
    ]
    client = _ScriptedClient(responses)

    result = idea_extraction.extract_idea(client, "fake-model", SOURCE_TEXT)

    assert result.company == "XYZ Corp"
    assert len(client.messages.calls) == 2


# --- blank -> None normalization happens at the persistence boundary --------


def test_extracted_idea_itself_never_normalizes_blank_to_none():
    """Normalization to None is cli.py's job at the DB-persistence
    boundary (see test_cli_shadow.py / test_cli_collect_source.py) -- the
    model/extraction function itself must keep returning the literal
    string the model produced, blank or not.
    """
    client = _ScriptedClient([_FakeResponse("end_turn", _valid_extraction_json(company=""))])
    result = idea_extraction.extract_idea(client, "fake-model", SOURCE_TEXT)
    assert result.company == ""  # not None
    assert result.company is not None


# --- no-leakage / factual-extraction-only ------------------------------------


def test_extract_idea_only_sends_source_text_never_anything_else():
    client = _ScriptedClient([_FakeResponse("end_turn", _valid_extraction_json())])

    idea_extraction.extract_idea(client, "fake-model", SOURCE_TEXT)

    sent = client.messages.calls[0]
    user_message = sent["messages"][0]["content"]
    assert SOURCE_TEXT in user_message
    # No leakage channel exists: extract_idea's signature only accepts
    # source_text -- there is no parameter through which a verdict,
    # comment, taste profile, or candidate-permanent-rules content could
    # even be passed.
    assert "STRONG_LIKE" not in user_message
    assert "verdict" not in user_message.lower()


def test_extract_idea_signature_has_no_taste_or_rules_or_verdict_parameters():
    """Screening remains a later, separate step (shadow.py) -- extraction
    has no way to receive taste/rules/a verdict even if a caller wanted
    to pass one.
    """
    import inspect

    params = set(inspect.signature(idea_extraction.extract_idea).parameters)
    assert params == {"client", "model_name", "source_text", "logger"}


def test_extracted_idea_has_no_screening_fields():
    """A compact idea record is factual extraction only -- it must never
    carry a verdict/prediction-shaped field.
    """
    forbidden = {"overall_prediction", "verdict", "prediction", "confidence", "mispricing", "recommendation"}
    assert forbidden.isdisjoint(idea_extraction.ExtractedIdea.model_fields.keys())


# --- retry / truncation behavior (unchanged by the schema fix) --------------


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


def test_extract_idea_malformed_json_is_retried_like_before():
    responses = [
        _FakeResponse("end_turn", '{"company": "cut off mid'),
        _FakeResponse("end_turn", _valid_extraction_json()),
    ]
    client = _ScriptedClient(responses)

    result = idea_extraction.extract_idea(client, "fake-model", SOURCE_TEXT)

    assert result.company == "XYZ Corp"
    assert len(client.messages.calls) == 2


def test_extract_idea_genuine_api_error_is_not_retried():
    client = _ScriptedClient([RuntimeError("simulated network failure")])

    with pytest.raises(structured_llm.StructuredGenerationError):
        idea_extraction.extract_idea(client, "fake-model", SOURCE_TEXT)

    assert len(client.messages.calls) == 1
