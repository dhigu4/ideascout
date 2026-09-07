"""Tests for the check-mail command logic (ideascout/cli.py).

These tests never call the real AgentMail API. Instead they monkeypatch
ideascout.agentmail_client so cli.cmd_check_mail runs against fake, in-memory
message data. That's enough to prove the important behaviors: new messages
get stored, already-known ones are skipped, a per-message failure is
counted and logged instead of crashing the whole run, and running the
command twice never creates duplicate rows.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from ideascout import agentmail_client, cli, db
from ideascout.config import Config


@dataclass
class FakeMessage:
    message_id: str
    inbox_id: str = "inbox_abc"
    thread_id: str = "thread_abc"
    timestamp: datetime = field(
        default_factory=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    from_: str = "alice@example.com"
    to: list = field(default_factory=lambda: ["ideas@yourdomain.agentmail.to"])
    subject: str = "An idea"
    text: str | None = "The idea, verbatim."
    html: str | None = None
    size: int = 100


@dataclass
class FakeItem:
    message_id: str


def make_config(tmp_path) -> Config:
    return Config(
        agentmail_api_key="fake-key",
        agentmail_inbox_id="inbox_abc",
        database_path=tmp_path / "ideas.db",
        log_path=tmp_path / "app.log",
    )


def test_check_mail_stores_new_messages(tmp_path, monkeypatch):
    config = make_config(tmp_path)

    fake_items = [FakeItem("msg_1"), FakeItem("msg_2")]
    fake_messages = {
        "msg_1": FakeMessage("msg_1"),
        "msg_2": FakeMessage("msg_2"),
    }

    monkeypatch.setattr(agentmail_client, "build_client", lambda api_key: object())
    monkeypatch.setattr(
        agentmail_client, "list_all_message_items", lambda client, inbox_id: fake_items
    )
    monkeypatch.setattr(
        agentmail_client,
        "fetch_message",
        lambda client, inbox_id, message_id: fake_messages[message_id],
    )

    exit_code = cli.cmd_check_mail(config)

    assert exit_code == 0

    conn = db.connect(config.database_path)
    assert db.count_total_messages(conn) == 2
    assert db.get_meta(conn, "last_check_at") is not None
    assert db.get_meta(conn, "last_error") is None
    conn.close()


def test_check_mail_is_idempotent_across_runs(tmp_path, monkeypatch):
    config = make_config(tmp_path)

    fake_items = [FakeItem("msg_1")]
    fake_messages = {"msg_1": FakeMessage("msg_1")}

    monkeypatch.setattr(agentmail_client, "build_client", lambda api_key: object())
    monkeypatch.setattr(
        agentmail_client, "list_all_message_items", lambda client, inbox_id: fake_items
    )
    monkeypatch.setattr(
        agentmail_client,
        "fetch_message",
        lambda client, inbox_id, message_id: fake_messages[message_id],
    )

    first = cli.cmd_check_mail(config)
    second = cli.cmd_check_mail(config)

    assert first == 0
    assert second == 0

    conn = db.connect(config.database_path)
    assert db.count_total_messages(conn) == 1
    conn.close()


def test_check_mail_counts_per_message_failures_without_aborting(tmp_path, monkeypatch):
    config = make_config(tmp_path)

    fake_items = [FakeItem("msg_ok"), FakeItem("msg_broken")]

    def fake_fetch(client, inbox_id, message_id):
        if message_id == "msg_broken":
            raise RuntimeError("simulated AgentMail failure")
        return FakeMessage(message_id)

    monkeypatch.setattr(agentmail_client, "build_client", lambda api_key: object())
    monkeypatch.setattr(
        agentmail_client, "list_all_message_items", lambda client, inbox_id: fake_items
    )
    monkeypatch.setattr(agentmail_client, "fetch_message", fake_fetch)

    exit_code = cli.cmd_check_mail(config)

    # Errors are reported (non-zero exit) but the good message is still saved.
    assert exit_code == 1

    conn = db.connect(config.database_path)
    assert db.count_total_messages(conn) == 1
    assert db.get_meta(conn, "last_error") is not None
    # A run that talked to AgentMail and finished still counts as a completed check.
    assert db.get_meta(conn, "last_check_at") is not None
    conn.close()


def test_check_mail_reports_failure_when_listing_fails(tmp_path, monkeypatch):
    config = make_config(tmp_path)

    def fake_list(client, inbox_id):
        raise RuntimeError("cannot reach AgentMail")

    monkeypatch.setattr(agentmail_client, "build_client", lambda api_key: object())
    monkeypatch.setattr(agentmail_client, "list_all_message_items", fake_list)

    exit_code = cli.cmd_check_mail(config)

    assert exit_code == 1

    conn = db.connect(config.database_path)
    assert db.count_total_messages(conn) == 0
    assert db.get_meta(conn, "last_error") is not None
    # The check never completed, so there is no successful check timestamp.
    assert db.get_meta(conn, "last_check_at") is None
    conn.close()
