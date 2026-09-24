"""Tests for `python run.py send-digest` / `send-digest --dry-run`
(Stage 6: real AgentMail-backed digest delivery).

No real network/email calls anywhere -- AgentMail is mocked at the
ideascout.agentmail_client.build_client boundary, returning a fake client
whose .inboxes.messages.send(...) is scripted per test. Every database
lives under pytest's tmp_path.
"""

from __future__ import annotations

import dataclasses

from ideascout import cli, db, notifier
from ideascout.config import Config
from tests.test_cli_build_taste import make_config as _base_make_config
from tests.test_cli_build_taste import insert_n_eligible, patch_taste


def make_config(tmp_path, **overrides) -> Config:
    config = _base_make_config(tmp_path)
    fields = {
        "screen_rules_path": tmp_path / "IDEA_SCREEN_RULES.md",
        "agentmail_api_key": "fake-agentmail-key",
        "agentmail_inbox_id": "inbox_abc",
        "digest_recipient_email": "brad@example.com",
        "alerts_enabled": True,
    }
    fields.update(overrides)
    return dataclasses.replace(config, **fields)


def write_screen_rules(config, content: str = "1. Mispricing\n   Is there a specific reason the market may be wrong?\n"):
    config.screen_rules_path.write_text(content, encoding="utf-8", newline="")


def build_taste_v1(config, monkeypatch, body: str = "## Strong Positive Signals\nHidden earnings power.\n"):
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()
    patch_taste(monkeypatch, body=body)
    exit_code = cli.cmd_build_taste(config)
    assert exit_code == 0
    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    conn.close()
    return latest


_UNSET = object()


def insert_scored_source(
    conn,
    latest_taste,
    config,
    *,
    external_id: str,
    overall_prediction: str = "WATCH",
    content_hash: str | None = None,
    created_at: str = "2026-01-01T00:00:00+00:00",
    company=_UNSET,
    ticker=_UNSET,
) -> int:
    # A sentinel (not `x or default`) so a test can explicitly pass
    # ticker=None/company=None to mean "genuinely absent" (e.g. testing
    # Stage 7's company-name fallback), distinct from "not provided at
    # all" (apply the default below).
    if company is _UNSET:
        company = f"Company {external_id}"
    # "YB" prefix deliberately avoids colliding with insert_n_eligible's
    # own "T{i}" ticker scheme (used by build_taste_v1's 15 training
    # records) -- a real collision there would make a fixture accidentally
    # exercise Stage 7's already-judged suppression instead of the
    # unrelated behavior most of these tests are checking.
    if ticker is _UNSET:
        ticker = f"YB{external_id}"
    content_hash = content_hash or f"hash-{external_id}"
    source_id = db.insert_collected_source(
        conn, source_name="yellowbrick", external_id=external_id,
        canonical_url=f"https://www.joinyellowbrick.com/sp/{external_id}",
        discovered_at=created_at, discovery_title=company, source_date=None,
        source_title=company, author=None, ticker=ticker, company=company,
        source_type="stock_pitch", content_hash=content_hash, raw_html_path=f"/x/{external_id}.html",
        metadata_json="{}", created_at=created_at,
    )
    db.update_collected_source_extracted(
        conn, source_id=source_id, company=company, ticker=ticker, source_title=company,
        source_date=None, business_summary="x", core_thesis="x", why_mispriced="x",
        future_earnings_change="x", upside_case="x", downside_or_key_risks="x", catalysts="x",
        what_must_be_true="x", evidence_of_market_misunderstanding="x", known_unknowns="x",
        extraction_model_name="fake-model", extracted_at=created_at,
    )
    db.insert_source_screening(
        conn, source_id=source_id, created_at=created_at,
        taste_version=latest_taste["version_number"], taste_sha256=latest_taste["idea_taste_sha256"],
        screen_rules_path=str(config.screen_rules_path), screen_rules_sha256="rules_hash",
        content_hash=content_hash, model_name="fake-model", overall_prediction=overall_prediction,
        mispricing="Plausible", variant_perception="Plausible", upside="Potentially sufficient",
        business_quality="Plausible", downside="Acceptable",
        key_reasons_json='["Reason one.", "Reason two.", "Reason three."]',
        key_concerns_json='["Concern one.", "Concern two.", "Concern three."]',
        critical_questions_json='["Question one?", "Question two?"]',
        confidence="MEDIUM",
    )
    return source_id


class _FakeSendResponse:
    def __init__(self, message_id="msg_fake"):
        self.message_id = message_id
        self.thread_id = "thread_fake"


class _FakeMessages:
    def __init__(self, response=None, exc=None):
        self._response = response if response is not None else _FakeSendResponse()
        self._exc = exc
        self.calls: list[dict] = []

    def send(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return self._response


class _FakeInboxes:
    def __init__(self, messages):
        self.messages = messages


class _FakeAgentMailClient:
    def __init__(self, messages):
        self.inboxes = _FakeInboxes(messages)


def patch_agentmail(monkeypatch, *, exc: Exception | None = None):
    """Mocks AgentMail entirely -- build_client returns a fake client
    whose .send() is scripted to succeed (default) or raise `exc`.
    Returns the fake FakeMessages object so tests can inspect calls.
    """
    from ideascout import agentmail_client

    messages = _FakeMessages(exc=exc)
    fake_client = _FakeAgentMailClient(messages)
    monkeypatch.setattr(agentmail_client, "build_client", lambda api_key: fake_client)
    return messages


# --- no candidates / dry-run: no email, no mutation --------------------------


def test_send_digest_no_eligible_ideas_sends_nothing_and_changes_nothing(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)
    messages = patch_agentmail(monkeypatch)

    exit_code = cli.cmd_send_digest(config, dry_run=False)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Candidates selected: 0" in output
    assert "No new ideas to send." in output
    assert messages.calls == []

    conn = db.connect(config.database_path)
    assert conn.execute("SELECT COUNT(*) FROM digest_shown_sources").fetchone()[0] == 0
    conn.close()


def test_send_digest_dry_run_sends_nothing_and_changes_nothing(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path, alerts_enabled=False)  # dry-run must work even if alerts are off
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_scored_source(conn, latest_taste, config, external_id="1", overall_prediction="WATCH")
    conn.close()
    messages = patch_agentmail(monkeypatch)

    exit_code = cli.cmd_send_digest(config, dry_run=True)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Candidates selected: 1" in output
    assert "DRY RUN" in output
    assert "no email sent and no database changes" in output.lower()
    assert "Email sent: NO" in output
    assert messages.calls == []

    conn = db.connect(config.database_path)
    assert conn.execute("SELECT COUNT(*) FROM digest_shown_sources").fetchone()[0] == 0
    conn.close()


def test_send_digest_dry_run_renders_email_content(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_scored_source(
        conn, latest_taste, config, external_id="1", overall_prediction="WATCH", company="XYZ Corp", ticker="XYZ"
    )
    conn.close()
    patch_agentmail(monkeypatch)

    cli.cmd_send_digest(config, dry_run=True)
    output = capsys.readouterr().out

    assert "XYZ Corp" in output
    assert "Reason one." in output
    assert "Concern one." in output
    assert "Question one?" in output
    assert "Mispricing" in output
    assert "joinyellowbrick.com/sp/1" in output


# --- successful sends ----------------------------------------------------------


def test_send_digest_one_watch_sends_one_email(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_scored_source(conn, latest_taste, config, external_id="1", overall_prediction="WATCH")
    conn.close()
    messages = patch_agentmail(monkeypatch)

    exit_code = cli.cmd_send_digest(config, dry_run=False)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Candidates selected: 1" in output
    assert "Email sent: YES" in output
    assert "Sources marked shown: 1" in output
    assert len(messages.calls) == 1
    assert messages.calls[0]["to"] == "brad@example.com"
    assert messages.calls[0]["inbox_id"] == "inbox_abc"


def test_send_digest_passes_sanitized_idempotency_key_to_agentmail(tmp_path, monkeypatch):
    """Stage 8 production fix: the real send path must pass AgentMail
    exactly the sanitized (SHA-256-derived) key -- no literal ":" or any
    other character outside AgentMail's allowed set -- and it must match
    what notifier.digest_idempotency_key computes for the same batch.
    """
    import re

    from ideascout import notifier

    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    source_id = insert_scored_source(conn, latest_taste, config, external_id="1", overall_prediction="WATCH")
    conn.close()
    messages = patch_agentmail(monkeypatch)

    cli.cmd_send_digest(config, dry_run=False)

    assert len(messages.calls) == 1
    key = messages.calls[0]["idempotency_key"]
    assert re.fullmatch(r"[A-Za-z0-9._~-]+", key)
    assert ":" not in key

    from ideascout.cli import utcnow_iso

    expected_date = utcnow_iso()[:10]
    assert key == notifier.digest_idempotency_key(expected_date, [source_id])


def test_send_digest_investigate_now_sorts_before_watch(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_scored_source(conn, latest_taste, config, external_id="1", overall_prediction="WATCH", company="WatchCo")
    insert_scored_source(
        conn, latest_taste, config, external_id="2", overall_prediction="INVESTIGATE_NOW", company="InvestNowCo",
        created_at="2026-01-02T00:00:00+00:00",
    )
    conn.close()
    messages = patch_agentmail(monkeypatch)

    cli.cmd_send_digest(config, dry_run=False)

    body = messages.calls[0]["text"]
    assert body.index("InvestNowCo") < body.index("WatchCo")


def test_send_digest_excludes_pass(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_scored_source(conn, latest_taste, config, external_id="1", overall_prediction="PASS", company="PassCo")
    insert_scored_source(conn, latest_taste, config, external_id="2", overall_prediction="WATCH", company="WatchCo")
    conn.close()
    messages = patch_agentmail(monkeypatch)

    exit_code = cli.cmd_send_digest(config, dry_run=False)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Candidates selected: 1" in output
    body = messages.calls[0]["text"]
    assert "PassCo" not in body
    assert "WatchCo" in body


def test_send_digest_caps_at_five(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    for i in range(8):
        insert_scored_source(
            conn, latest_taste, config, external_id=str(i), overall_prediction="INVESTIGATE_NOW",
            company=f"Company{i}", created_at=f"2026-01-01T00:0{i}:00+00:00",
        )
    conn.close()
    messages = patch_agentmail(monkeypatch)

    cli.cmd_send_digest(config, dry_run=False)

    body = messages.calls[0]["text"]
    included = sum(1 for i in range(8) if f"Company{i}" in body)
    assert included == 5


def test_send_digest_successful_send_marks_only_included_source_ids(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    included_ids = [
        insert_scored_source(
            conn, latest_taste, config, external_id=str(i), overall_prediction="INVESTIGATE_NOW",
            created_at=f"2026-01-01T00:0{i}:00+00:00",
        )
        for i in range(5)
    ]
    excluded_id = insert_scored_source(
        conn, latest_taste, config, external_id="99", overall_prediction="WATCH",
        created_at="2026-01-01T00:09:00+00:00",
    )
    conn.close()
    patch_agentmail(monkeypatch)

    cli.cmd_send_digest(config, dry_run=False)

    conn = db.connect(config.database_path)
    shown_ids = {row["source_id"] for row in conn.execute("SELECT source_id FROM digest_shown_sources")}
    conn.close()

    assert shown_ids == set(included_ids)
    assert excluded_id not in shown_ids


def test_immediate_rerun_after_success_has_no_candidates(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_scored_source(conn, latest_taste, config, external_id="1", overall_prediction="WATCH")
    conn.close()
    patch_agentmail(monkeypatch)

    cli.cmd_send_digest(config, dry_run=False)
    capsys.readouterr()

    exit_code = cli.cmd_send_digest(config, dry_run=False)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "No new ideas to send." in output


def test_eight_eligible_five_first_run_three_second_run(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    for i in range(8):
        insert_scored_source(
            conn, latest_taste, config, external_id=str(i), overall_prediction="INVESTIGATE_NOW",
            company=f"Company{i}", created_at=f"2026-01-01T00:0{i}:00+00:00",
        )
    conn.close()
    messages = patch_agentmail(monkeypatch)

    cli.cmd_send_digest(config, dry_run=False)
    output_1 = capsys.readouterr().out
    assert "Candidates selected: 5" in output_1
    assert "Sources marked shown: 5" in output_1

    cli.cmd_send_digest(config, dry_run=False)
    output_2 = capsys.readouterr().out
    assert "Candidates selected: 3" in output_2
    assert "Sources marked shown: 3" in output_2

    assert len(messages.calls) == 2
    conn = db.connect(config.database_path)
    total_shown = conn.execute("SELECT COUNT(*) FROM digest_shown_sources").fetchone()[0]
    conn.close()
    assert total_shown == 8


# --- send failure --------------------------------------------------------------


def test_send_digest_failed_send_marks_nothing(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_scored_source(conn, latest_taste, config, external_id="1", overall_prediction="WATCH")
    conn.close()
    patch_agentmail(monkeypatch, exc=RuntimeError("simulated AgentMail rejection"))

    exit_code = cli.cmd_send_digest(config, dry_run=False)
    assert exit_code != 0
    output = capsys.readouterr().out
    assert "Email sent: NO" in output
    assert "Sources marked shown: 0" in output
    assert "FAILED" in output

    conn = db.connect(config.database_path)
    assert conn.execute("SELECT COUNT(*) FROM digest_shown_sources").fetchone()[0] == 0
    conn.close()


def test_send_digest_alerts_disabled_refuses_real_send(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path, alerts_enabled=False)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_scored_source(conn, latest_taste, config, external_id="1", overall_prediction="WATCH")
    conn.close()
    messages = patch_agentmail(monkeypatch)

    exit_code = cli.cmd_send_digest(config, dry_run=False)
    assert exit_code != 0
    output = capsys.readouterr().out
    assert "ALERTS_ENABLED" in output
    assert messages.calls == []

    conn = db.connect(config.database_path)
    assert conn.execute("SELECT COUNT(*) FROM digest_shown_sources").fetchone()[0] == 0
    conn.close()


def test_send_digest_missing_recipient_fails_clearly(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path, digest_recipient_email=None)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_scored_source(conn, latest_taste, config, external_id="1", overall_prediction="WATCH")
    conn.close()
    messages = patch_agentmail(monkeypatch)

    exit_code = cli.cmd_send_digest(config, dry_run=False)
    assert exit_code != 0
    output = capsys.readouterr().out
    assert "DIGEST_RECIPIENT_EMAIL" in output
    assert messages.calls == []


# --- email succeeds but DB marking fails ---------------------------------------


def test_send_digest_db_marking_failure_after_success_warns_loudly(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_scored_source(conn, latest_taste, config, external_id="1", overall_prediction="WATCH")
    conn.close()
    patch_agentmail(monkeypatch)

    def failing_mark(conn_arg, source_ids, shown_at):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(db, "mark_sources_shown_in_digest", failing_mark)

    exit_code = cli.cmd_send_digest(config, dry_run=False)
    assert exit_code != 0
    output = capsys.readouterr().out
    assert "Email sent: YES" in output
    assert "CRITICAL" in output
    assert "duplicate" in output.lower()
    assert "Sources marked shown: 0" in output


# --- version / dedupe semantics -------------------------------------------------


def test_historical_version_does_not_surface_instead_of_latest(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    # Old version: INVESTIGATE_NOW, never shown.
    insert_scored_source(
        conn, latest_taste, config, external_id="1", overall_prediction="INVESTIGATE_NOW",
        content_hash="old-content", created_at="2026-01-01T00:00:00+00:00",
    )
    # New version of the SAME external_id: now screens as PASS.
    insert_scored_source(
        conn, latest_taste, config, external_id="1", overall_prediction="PASS",
        content_hash="new-content", created_at="2026-01-02T00:00:00+00:00",
    )
    conn.close()
    messages = patch_agentmail(monkeypatch)

    exit_code = cli.cmd_send_digest(config, dry_run=False)
    assert exit_code == 0
    output_capture = messages.calls
    assert output_capture == []  # old INVESTIGATE_NOW version never resurfaces


def test_materially_changed_latest_version_may_surface(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    old_id = insert_scored_source(
        conn, latest_taste, config, external_id="1", overall_prediction="WATCH",
        content_hash="old-content", created_at="2026-01-01T00:00:00+00:00",
    )
    # Mark the OLD version already shown.
    db.mark_source_shown_in_digest(conn, old_id, "2026-01-01T00:01:00+00:00")
    new_id = insert_scored_source(
        conn, latest_taste, config, external_id="1", overall_prediction="INVESTIGATE_NOW",
        content_hash="new-content", created_at="2026-01-02T00:00:00+00:00",
    )
    conn.close()
    patch_agentmail(monkeypatch)

    exit_code = cli.cmd_send_digest(config, dry_run=False)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    shown_ids = {row["source_id"] for row in conn.execute("SELECT source_id FROM digest_shown_sources")}
    conn.close()
    assert new_id in shown_ids
    assert old_id in shown_ids  # was already marked before this run, untouched


# --- shared selection / read-only guarantees ------------------------------------


def test_preview_digest_and_send_digest_use_identical_candidate_selection(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_scored_source(conn, latest_taste, config, external_id="1", overall_prediction="INVESTIGATE_NOW")
    insert_scored_source(conn, latest_taste, config, external_id="2", overall_prediction="WATCH")
    insert_scored_source(conn, latest_taste, config, external_id="3", overall_prediction="PASS")
    selected, suppressed_count = cli.select_digest_candidates(conn, latest_taste["version_number"])
    conn.close()

    assert suppressed_count == 0
    assert [row["external_id"] for row in selected] == ["1", "2"]


def test_preview_digest_remains_read_only_regarding_agentmail(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_scored_source(conn, latest_taste, config, external_id="1", overall_prediction="WATCH")
    conn.close()
    messages = patch_agentmail(monkeypatch)

    cli.cmd_preview_digest(config)

    assert messages.calls == []


def test_no_llm_invoked_by_digest_generation(tmp_path, monkeypatch):
    from ideascout import idea_extraction, shadow

    def fail_if_called(*args, **kwargs):
        raise AssertionError("digest generation must never call an LLM")

    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_scored_source(conn, latest_taste, config, external_id="1", overall_prediction="WATCH")
    conn.close()
    patch_agentmail(monkeypatch)

    monkeypatch.setattr(idea_extraction, "build_client", fail_if_called)
    monkeypatch.setattr(idea_extraction, "extract_idea", fail_if_called)
    monkeypatch.setattr(shadow, "build_client", fail_if_called)
    monkeypatch.setattr(shadow, "screen_idea", fail_if_called)

    cli.cmd_preview_digest(config)
    exit_code = cli.cmd_send_digest(config, dry_run=True)
    assert exit_code == 0


# --- Taste/rules preconditions --------------------------------------------------


def test_send_digest_no_taste_yet(tmp_path, capsys):
    config = make_config(tmp_path)
    exit_code = cli.cmd_send_digest(config, dry_run=True)
    assert exit_code == 0
    assert "No taste model" in capsys.readouterr().out
