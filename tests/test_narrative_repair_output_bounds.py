"""Narrative repair output is bounded: a repair-specific token ceiling, a
schema that asks for only the missing fields, and hard item limits enforced
on our side. A truncated or malformed response still fails closed after one
call, and a successful repair fills only the fields that were missing.

No real LLM or network calls: a recording fake client stands in for the
Anthropic client, and every database lives under pytest's tmp_path.
"""

from __future__ import annotations

import io
import contextlib
import json
import types

import pytest

from ideascout import cli, db, shadow, structured_llm
from tests.test_narrative_repair_diagnostics import FakeClient, _repairs, _run_rescreen, _setup_gap
from tests.test_screening_narrative import (
    build_taste_v1,
    insert_screened_source,
    make_config,
    prediction,
    write_rules,
)

ALL_FIELDS = {"key_reasons", "key_concerns", "critical_questions"}


class RecordingClient:
    def __init__(self, text: str, stop_reason: str = "end_turn"):
        self.kwargs: list[dict] = []
        self._text = text
        self._stop_reason = stop_reason
        self.messages = self

    def create(self, **kwargs):
        self.kwargs.append(kwargs)
        return types.SimpleNamespace(
            stop_reason=self._stop_reason,
            content=[types.SimpleNamespace(type="text", text=self._text)],
        )


class _IdeaRecord:
    def __getitem__(self, column):
        return "example"


def _call_repair(client, missing_fields):
    return shadow.repair_narrative(
        client,
        "fake-model",
        idea_taste_body="taste",
        screen_rules_body="rules",
        idea_record=_IdeaRecord(),
        prediction=types.SimpleNamespace(
            overall_prediction="WATCH", confidence="MEDIUM", mispricing="Plausible",
            variant_perception="Plausible", upside="Potentially sufficient",
            business_quality="Plausible", downside="Problematic",
        ),
        missing_fields=missing_fields,
    )


def _schema_of(kwargs):
    return kwargs["output_config"]["format"]["schema"]


def _setup_no_narrative(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    source_id, screening_id = insert_screened_source(
        conn, latest, rules_sha, external_id="144273",
        pred=prediction("WATCH", reasons=[], concerns=[], questions=[]),
        ticker="ITECH", company="I-Tech AB",
    )
    conn.close()
    return config, source_id, screening_id, latest


def _effective(config, source_id):
    conn = db.connect(config.database_path)
    effective = cli._effective_narrative(conn, db.get_latest_source_screening_for_source(conn, source_id))
    conn.close()
    return effective


def _patch_screen_and_build(monkeypatch, client):
    monkeypatch.setattr(shadow, "build_client", lambda api_key: client)

    def no_screen(*args, **kwargs):
        raise AssertionError("the full screen must not run")

    monkeypatch.setattr(shadow, "screen_idea", no_screen)


# --- ceiling and call shape ----------------------------------------------------------


def test_repair_uses_repair_specific_output_ceiling(monkeypatch):
    client = RecordingClient('{"key_concerns": ["c"]}')
    _call_repair(client, ["key_concerns"])
    assert client.kwargs[0]["max_tokens"] == shadow.REPAIR_MAX_OUTPUT_TOKENS
    assert shadow.REPAIR_MAX_OUTPUT_TOKENS == 1024


def test_global_structured_llm_limits_are_unchanged():
    assert shadow.MAX_OUTPUT_TOKENS == 2048
    assert shadow.MAX_ATTEMPTS == 3


def test_worst_compliant_payload_fits_the_ceiling_with_margin():
    worst_item = "x" * shadow.REPAIR_MAX_ITEM_CHARS
    payload = json.dumps({
        field: [worst_item] * shadow.REPAIR_MAX_ITEMS_PER_FIELD for field in sorted(ALL_FIELDS)
    })
    conservative_tokens = len(payload) / 3  # roughly 3 characters per token, deliberately pessimistic
    assert conservative_tokens < shadow.REPAIR_MAX_OUTPUT_TOKENS / 1.5


def test_repair_makes_exactly_one_call_even_when_truncated(monkeypatch):
    client = RecordingClient('{"key_concerns": ["cut', stop_reason="max_tokens")
    with pytest.raises(structured_llm.StructuredGenerationError) as info:
        _call_repair(client, ["key_concerns"])
    assert len(client.kwargs) == 1
    assert info.value.code == structured_llm.RESPONSE_PARSE_ERROR
    assert info.value.detail == "truncated at output limit"


# --- schema and prompt constrain the output --------------------------------------------


def test_schema_contains_only_the_requested_fields(monkeypatch):
    client = RecordingClient('{"critical_questions": ["Q?"]}')
    _call_repair(client, ["critical_questions"])
    assert set(_schema_of(client.kwargs[0])["properties"]) == {"critical_questions"}


def test_schema_describes_item_limits_and_prompt_forbids_restating(monkeypatch):
    client = RecordingClient('{"key_concerns": ["c"], "critical_questions": ["q"]}')
    _call_repair(client, ["key_concerns", "critical_questions"])
    schema = _schema_of(client.kwargs[0])
    assert "maxItems: 2" in schema["properties"]["key_concerns"]["description"]
    system = client.kwargs[0]["system"]
    user = client.kwargs[0]["messages"][0]["content"]
    assert "Return 1 or 2 items" in user
    assert "Do not restate" in user
    assert "Missing fields to fill: key_concerns, critical_questions" in user
    assert "Do NOT revisit" in user
    assert "\nrules\n" in system  # the rules body is still the system context


def test_prompt_tells_model_to_write_only_the_items(monkeypatch):
    client = RecordingClient('{"key_reasons": ["r"]}')
    _call_repair(client, ["key_reasons"])
    user = client.kwargs[0]["messages"][0]["content"]
    assert "Write only the items" in user


# --- item limits enforced on our side ----------------------------------------------------


def test_more_than_two_items_fails_closed_as_validation_error(monkeypatch):
    client = RecordingClient('{"key_reasons": ["a", "b", "c"]}')
    with pytest.raises(structured_llm.StructuredGenerationError) as info:
        _call_repair(client, ["key_reasons"])
    assert info.value.code == structured_llm.VALIDATION_ERROR
    assert info.value.detail.startswith("key_reasons")


def test_over_long_item_fails_closed_as_validation_error(monkeypatch):
    client = RecordingClient(json.dumps({"key_concerns": ["x" * (shadow.REPAIR_MAX_ITEM_CHARS + 1)]}))
    with pytest.raises(structured_llm.StructuredGenerationError) as info:
        _call_repair(client, ["key_concerns"])
    assert info.value.code == structured_llm.VALIDATION_ERROR


def test_maximum_compliant_normal_length_response_parses(monkeypatch):
    item = ("A specific, concise sentence about the thesis. " * 8)[: shadow.REPAIR_MAX_ITEM_CHARS]
    assert len(item) == shadow.REPAIR_MAX_ITEM_CHARS
    client = RecordingClient(json.dumps({
        "key_reasons": [item, item],
        "key_concerns": [item, item],
        "critical_questions": [item, item],
    }))
    result = _call_repair(client, sorted(ALL_FIELDS))
    assert len(result.key_reasons) == 2 and len(result.critical_questions) == 2
    assert len(client.kwargs) == 1


def test_repair_result_is_a_full_narrative_repair_with_unrequested_fields_empty(monkeypatch):
    client = RecordingClient('{"key_reasons": ["injected"], "key_concerns": ["c"]}')
    result = _call_repair(client, ["key_concerns"])
    assert result.key_reasons == []
    assert result.key_concerns == ["c"]
    assert result.critical_questions == []


# --- end-to-end through rescreen-source ---------------------------------------------------


def test_missing_all_three_fields_succeeds_in_one_call(tmp_path, monkeypatch, capsys):
    config, source_id, screening_id, _ = _setup_no_narrative(tmp_path, monkeypatch)
    client = FakeClient(text='{"key_reasons": ["Mispricing."], "key_concerns": ["Risk."], "critical_questions": ["Q?"]}')

    code, _ = _run_rescreen(config, monkeypatch, client, capsys)

    assert code == 0
    assert client.calls == 1
    [row] = _repairs(config, screening_id)
    assert row["status"] == "REPAIRED"
    effective = _effective(config, source_id)
    assert effective["key_reasons"] == ["Mispricing."]
    assert effective["key_concerns"] == ["Risk."]
    assert effective["critical_questions"] == ["Q?"]


def test_missing_only_one_field_fills_only_that_field(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    source_id, screening_id = insert_screened_source(
        conn, latest, rules_sha, external_id="144273",
        pred=prediction("WATCH", reasons=["Original reason."], concerns=["Original concern."], questions=[]),
    )
    conn.close()
    client = FakeClient(text='{"key_reasons": ["Injected reason."], "critical_questions": ["What breaks even?"]}')

    _run_rescreen(config, monkeypatch, client, capsys)

    effective = _effective(config, source_id)
    assert effective["key_reasons"] == ["Original reason."]
    assert effective["key_concerns"] == ["Original concern."]
    assert effective["critical_questions"] == ["What breaks even?"]


def test_existing_substantive_fields_are_never_overwritten(tmp_path, monkeypatch, capsys):
    config, source_id, _, _ = _setup_no_narrative_with_reasons(tmp_path, monkeypatch)
    client = FakeClient(text='{"key_reasons": ["Different."], "key_concerns": ["Filled."], "critical_questions": ["Q?"]}')

    _run_rescreen(config, monkeypatch, client, capsys)

    assert _effective(config, source_id)["key_reasons"] == ["Original reason."]


def _setup_no_narrative_with_reasons(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    source_id, screening_id = insert_screened_source(
        conn, latest, rules_sha, external_id="144273",
        pred=prediction("WATCH", reasons=["Original reason."], concerns=[], questions=[]),
    )
    conn.close()
    return config, source_id, screening_id, latest


def test_truncated_single_call_fails_closed_and_stays_digest_ineligible(tmp_path, monkeypatch, capsys):
    config, source_id, screening_id, latest = _setup_no_narrative(tmp_path, monkeypatch)
    client = FakeClient(text='{"key_reasons": ["cut', stop_reason="max_tokens")

    code, output = _run_rescreen(config, monkeypatch, client, capsys)

    assert code == 0
    assert client.calls == 1
    assert "Narrative repair: FAILED (RESPONSE_PARSE_ERROR)" in output
    [row] = _repairs(config, screening_id)
    assert row["status"] == "FAILED"
    assert row["error"] == "RESPONSE_PARSE_ERROR: truncated at output limit"

    conn = db.connect(config.database_path)
    selected, _, suppressed = cli.select_digest_candidates_detailed(conn, latest["version_number"])
    conn.close()
    assert suppressed == 1
    assert source_id not in {r["source_id"] for r in selected}


def test_malformed_response_fails_closed_after_one_call(tmp_path, monkeypatch, capsys):
    config, source_id, screening_id, _ = _setup_no_narrative(tmp_path, monkeypatch)
    client = FakeClient(text="{not json")

    _run_rescreen(config, monkeypatch, client, capsys)

    assert client.calls == 1
    assert _effective(config, source_id)["key_reasons"] == []
    [row] = _repairs(config, screening_id)
    assert row["status"] == "FAILED"


def test_successful_repair_makes_effective_narrative_complete(tmp_path, monkeypatch, capsys):
    config, source_id, _, latest = _setup_no_narrative(tmp_path, monkeypatch)
    client = FakeClient(text='{"key_reasons": ["r"], "key_concerns": ["c"], "critical_questions": ["q"]}')

    _run_rescreen(config, monkeypatch, client, capsys)

    conn = db.connect(config.database_path)
    selected, _, suppressed = cli.select_digest_candidates_detailed(conn, latest["version_number"])
    conn.close()
    assert suppressed == 0
    assert source_id in {r["source_id"] for r in selected}
