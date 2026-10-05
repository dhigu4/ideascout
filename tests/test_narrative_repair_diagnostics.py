"""Narrative-repair failure diagnostics.

A FAILED or STILL_INCOMPLETE repair must say WHY, in a form that is safe to
store and print: a stable category (PROVIDER_ERROR, RESPONSE_PARSE_ERROR,
VALIDATION_ERROR, EMPTY_RESPONSE, STILL_INCOMPLETE) plus a sanitized detail
(exception class / HTTP status / pydantic field names). Provider message
text, prompts, and keys must never reach the database error column, stdout,
or stderr. `show-narrative-repairs` reads those rows back without any LLM
call or database write.

No real LLM or network calls: the Anthropic client is a fake object, and every
database lives under pytest's tmp_path.
"""

from __future__ import annotations

import io
import contextlib
import types

import pytest

from ideascout import cli, db, shadow
from tests.test_screening_narrative import (
    FULL,
    build_taste_v1,
    fail_all_llm,
    insert_screened_source,
    make_config,
    prediction,
    write_rules,
)

SECRET = "sk-ant-api03-FAKESECRETVALUE-0000"
PROMPT_MARKER = "PROMPT-MARKER-DO-NOT-LEAK"


class FakeProviderError(Exception):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class FakeClient:
    """Stands in for the Anthropic client. `messages` is the object whose
    `.create` generate_structured calls.
    """

    def __init__(self, *, text: str | None = None, stop_reason: str = "end_turn", raises: Exception | None = None):
        self.calls = 0
        self._text = text
        self._stop_reason = stop_reason
        self._raises = raises
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        content = [] if self._text is None else [types.SimpleNamespace(type="text", text=self._text)]
        return types.SimpleNamespace(stop_reason=self._stop_reason, content=content)


def _setup_gap(tmp_path, monkeypatch):
    """A WATCH whose key_concerns and critical_questions are empty -- the
    narrative is incomplete, so rescreen-source must attempt one repair.
    """
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    source_id, screening_id = insert_screened_source(
        conn, latest, rules_sha, external_id="144273",
        pred=prediction("WATCH", reasons=["Hidden earnings power."], concerns=[], questions=[]),
        ticker="ITECH", company="I-Tech AB",
    )
    conn.close()
    return config, source_id, screening_id, latest


def _run_rescreen(config, monkeypatch, client, capsys):
    monkeypatch.setattr(shadow, "build_client", lambda api_key: client)

    def no_screen(*args, **kwargs):
        raise AssertionError("the full screen must not run for an existing screening")

    monkeypatch.setattr(shadow, "screen_idea", no_screen)
    code = cli.cmd_rescreen_source(config, "yellowbrick", "144273")
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def _repairs(config, screening_id):
    conn = db.connect(config.database_path)
    rows = [dict(r) for r in db.list_screening_narrative_repairs(conn, screening_id)]
    conn.close()
    return rows


def _snapshot_all_tables(config):
    conn = db.connect(config.database_path)
    tables = [
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    ]
    snapshot = {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in tables}
    conn.close()
    return snapshot


def _show(config, capsys):
    code = cli.cmd_show_narrative_repairs(config, "yellowbrick", "144273")
    captured = capsys.readouterr()
    return code, captured.out


# --- categories ---------------------------------------------------------------------


def test_provider_exception_is_recorded_with_class_and_status_only(tmp_path, monkeypatch, capsys):
    config, _, screening_id, _ = _setup_gap(tmp_path, monkeypatch)
    client = FakeClient(raises=FakeProviderError(f"401 invalid key {SECRET} echoing {PROMPT_MARKER}", 401))

    code, output = _run_rescreen(config, monkeypatch, client, capsys)

    assert code == 0
    assert client.calls == 1
    assert "Narrative repair: FAILED (PROVIDER_ERROR)" in output
    [row] = _repairs(config, screening_id)
    assert row["status"] == "FAILED"
    assert row["error"] == "PROVIDER_ERROR: FakeProviderError HTTP 401"


def test_parse_failure_is_recorded_and_displayed(tmp_path, monkeypatch, capsys):
    config, _, screening_id, _ = _setup_gap(tmp_path, monkeypatch)
    client = FakeClient(text="this is definitely not json {")

    code, output = _run_rescreen(config, monkeypatch, client, capsys)

    assert code == 0
    assert "Narrative repair: FAILED (RESPONSE_PARSE_ERROR)" in output
    [row] = _repairs(config, screening_id)
    assert row["status"] == "FAILED"
    assert row["error"] == "RESPONSE_PARSE_ERROR: malformed JSON"


def test_truncated_response_is_a_parse_category(tmp_path, monkeypatch, capsys):
    config, _, screening_id, _ = _setup_gap(tmp_path, monkeypatch)
    client = FakeClient(text='{"key_reasons": ["cut', stop_reason="max_tokens")

    _run_rescreen(config, monkeypatch, client, capsys)

    [row] = _repairs(config, screening_id)
    assert row["error"] == "RESPONSE_PARSE_ERROR: truncated at output limit"


def test_schema_mismatch_is_a_validation_error_naming_fields_only(tmp_path, monkeypatch, capsys):
    config, _, screening_id, _ = _setup_gap(tmp_path, monkeypatch)
    client = FakeClient(text='{"key_concerns": 12345, "critical_questions": []}')

    code, output = _run_rescreen(config, monkeypatch, client, capsys)

    assert "Narrative repair: FAILED (VALIDATION_ERROR)" in output
    [row] = _repairs(config, screening_id)
    assert row["status"] == "FAILED"
    assert row["error"].startswith("VALIDATION_ERROR: key_concerns")
    assert "12345" not in row["error"]


def test_empty_response_is_its_own_category(tmp_path, monkeypatch, capsys):
    config, _, screening_id, _ = _setup_gap(tmp_path, monkeypatch)
    client = FakeClient(text=None)

    _run_rescreen(config, monkeypatch, client, capsys)

    [row] = _repairs(config, screening_id)
    assert row["status"] == "FAILED"
    assert row["error"] == "EMPTY_RESPONSE: response contained no text"


def test_still_incomplete_response_is_recorded_with_missing_fields(tmp_path, monkeypatch, capsys):
    config, _, screening_id, _ = _setup_gap(tmp_path, monkeypatch)
    client = FakeClient(text='{"key_concerns": ["Execution risk."], "critical_questions": []}')

    code, output = _run_rescreen(config, monkeypatch, client, capsys)

    assert code == 0
    assert "Narrative repair: FAILED" not in output
    [row] = _repairs(config, screening_id)
    assert row["status"] == "STILL_INCOMPLETE"
    assert row["error"] == "STILL_INCOMPLETE: fields still empty after repair: critical_questions"


def test_successful_repair_has_no_error(tmp_path, monkeypatch, capsys):
    config, _, screening_id, _ = _setup_gap(tmp_path, monkeypatch)
    client = FakeClient(text='{"key_concerns": ["Execution risk."], "critical_questions": ["When does it break even?"]}')

    code, _ = _run_rescreen(config, monkeypatch, client, capsys)

    assert code == 0
    [row] = _repairs(config, screening_id)
    assert row["status"] == "REPAIRED"
    assert row["error"] is None


# --- leakage ------------------------------------------------------------------------


def test_no_secret_or_prompt_text_reaches_db_or_console(tmp_path, monkeypatch, capsys):
    config, _, screening_id, _ = _setup_gap(tmp_path, monkeypatch)
    client = FakeClient(raises=FakeProviderError(f"bad key {SECRET} body {PROMPT_MARKER}", 500))

    _, output = _run_rescreen(config, monkeypatch, client, capsys)
    fail_all_llm(monkeypatch)
    _, shown = _show(config, capsys)

    stored = repr(_repairs(config, screening_id))
    for text in (output, shown, stored):
        assert SECRET not in text
        assert PROMPT_MARKER not in text


# --- diagnostic command -----------------------------------------------------------------


def test_show_reports_failed_attempt_with_category_and_missing_fields(tmp_path, monkeypatch, capsys):
    config, _, _, _ = _setup_gap(tmp_path, monkeypatch)
    _run_rescreen(config, monkeypatch, FakeClient(raises=FakeProviderError("boom", 503)), capsys)
    fail_all_llm(monkeypatch)

    code, out = _show(config, capsys)

    assert code == 0
    assert "attempt 1" in out
    assert "FAILED" in out
    assert "missing before attempt: key_concerns, critical_questions" in out
    assert "fields returned: none" in out
    assert "reason: PROVIDER_ERROR: FakeProviderError HTTP 503" in out
    assert "Effective narrative: INCOMPLETE" in out


def test_show_reports_repaired_attempt_as_complete(tmp_path, monkeypatch, capsys):
    config, _, _, _ = _setup_gap(tmp_path, monkeypatch)
    _run_rescreen(
        config, monkeypatch,
        FakeClient(text='{"key_concerns": ["Execution risk."], "critical_questions": ["Q?"]}'),
        capsys,
    )
    fail_all_llm(monkeypatch)

    _, out = _show(config, capsys)

    assert "REPAIRED" in out
    assert "fields returned: key_concerns, critical_questions" in out
    assert "reason: -" in out
    assert "Effective narrative: COMPLETE" in out


def test_show_reports_still_incomplete_attempt(tmp_path, monkeypatch, capsys):
    config, _, _, _ = _setup_gap(tmp_path, monkeypatch)
    _run_rescreen(config, monkeypatch, FakeClient(text='{"key_concerns": ["c"], "critical_questions": []}'), capsys)
    fail_all_llm(monkeypatch)

    _, out = _show(config, capsys)

    assert "STILL_INCOMPLETE" in out
    assert "reason: STILL_INCOMPLETE: fields still empty after repair: critical_questions" in out
    assert "Effective narrative: INCOMPLETE" in out


def test_show_is_read_only_and_makes_no_llm_call(tmp_path, monkeypatch, capsys):
    config, _, _, _ = _setup_gap(tmp_path, monkeypatch)
    _run_rescreen(config, monkeypatch, FakeClient(raises=FakeProviderError("boom", 500)), capsys)
    before = _snapshot_all_tables(config)
    fail_all_llm(monkeypatch)

    _show(config, capsys)
    _show(config, capsys)

    assert _snapshot_all_tables(config) == before


def test_show_unknown_external_id_changes_nothing(tmp_path, monkeypatch, capsys):
    config, _, _, _ = _setup_gap(tmp_path, monkeypatch)
    before = _snapshot_all_tables(config)
    fail_all_llm(monkeypatch)

    code = cli.cmd_show_narrative_repairs(config, "yellowbrick", "999999")
    capsys.readouterr()

    assert code == 1
    assert _snapshot_all_tables(config) == before


def test_show_never_prints_legacy_raw_error_text(tmp_path, monkeypatch, capsys):
    config, _, screening_id, _ = _setup_gap(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    db.insert_screening_narrative_repair(
        conn, screening_id=screening_id, attempted_at="2026-01-01T00:00:00+00:00", model_name="m",
        status="FAILED", missing_fields=["key_concerns"], key_reasons=[], key_concerns=[],
        critical_questions=[], error=f"LLM call failed while repairing: bad key {SECRET} {PROMPT_MARKER}",
    )
    conn.close()
    fail_all_llm(monkeypatch)

    _, out = _show(config, capsys)

    assert "legacy row written before sanitized diagnostics" in out
    assert SECRET not in out
    assert PROMPT_MARKER not in out


def test_show_command_is_wired_into_main(tmp_path, monkeypatch, capsys):
    config, _, _, _ = _setup_gap(tmp_path, monkeypatch)
    _run_rescreen(config, monkeypatch, FakeClient(raises=FakeProviderError("boom", 500)), capsys)
    fail_all_llm(monkeypatch)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(cli, "setup_logging", lambda path: None)

    code = cli.main(["show-narrative-repairs", "yellowbrick", "--external-id", "144273"])

    assert code == 0
    assert "reason: PROVIDER_ERROR: FakeProviderError HTTP 500" in capsys.readouterr().out


# --- digest stays fail-closed -----------------------------------------------------------


@pytest.mark.parametrize("client_kwargs", [
    dict(raises=FakeProviderError("boom", 500)),
    dict(text="not json"),
    dict(text='{"key_concerns": ["c"], "critical_questions": []}'),
])
def test_failed_or_still_incomplete_repair_keeps_source_out_of_digest(tmp_path, monkeypatch, capsys, client_kwargs):
    config, source_id, _, latest = _setup_gap(tmp_path, monkeypatch)
    _run_rescreen(config, monkeypatch, FakeClient(**client_kwargs), capsys)
    fail_all_llm(monkeypatch)

    conn = db.connect(config.database_path)
    selected, _, narrative_suppressed = cli.select_digest_candidates_detailed(conn, latest["version_number"])
    conn.close()

    assert narrative_suppressed == 1
    assert source_id not in {r["source_id"] for r in selected}
