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
    config = Config(
        agentmail_api_key="fake-key",
        agentmail_inbox_id="inbox_abc",
        database_path=tmp_path / "ideas.db",
        log_path=tmp_path / "app.log",
    )
    # Normal commands now refuse to silently create a missing production
    # database (see db.open_production_database) -- tests may still create
    # temporary databases normally, so do that explicitly here.
    db.connect(config.database_path).close()
    return config


def patch_agentmail(monkeypatch, all_items, authenticated_ids=None, fetch_message=None):
    """Simulate AgentMail's real listing behavior: include_unauthenticated=True
    returns every item, the default (False) returns only the authenticated
    subset. Defaults every item to authenticated unless told otherwise, so
    existing tests that don't care about authentication don't need to.
    """
    if authenticated_ids is None:
        authenticated_ids = {item.message_id for item in all_items}

    def fake_list(client, inbox_id, include_unauthenticated=False):
        if include_unauthenticated:
            return all_items
        return [item for item in all_items if item.message_id in authenticated_ids]

    monkeypatch.setattr(agentmail_client, "build_client", lambda api_key: object())
    monkeypatch.setattr(agentmail_client, "list_all_message_items", fake_list)
    monkeypatch.setattr(
        agentmail_client, "fetch_authenticated_message_ids", lambda client, inbox_id: authenticated_ids
    )
    if fetch_message is not None:
        monkeypatch.setattr(agentmail_client, "fetch_message", fetch_message)


def test_check_mail_stores_new_messages(tmp_path, monkeypatch):
    config = make_config(tmp_path)

    fake_items = [FakeItem("msg_1"), FakeItem("msg_2")]
    fake_messages = {
        "msg_1": FakeMessage("msg_1"),
        "msg_2": FakeMessage("msg_2"),
    }

    patch_agentmail(
        monkeypatch, fake_items, fetch_message=lambda client, inbox_id, message_id: fake_messages[message_id]
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

    patch_agentmail(
        monkeypatch, fake_items, fetch_message=lambda client, inbox_id, message_id: fake_messages[message_id]
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

    patch_agentmail(monkeypatch, fake_items, fetch_message=fake_fetch)

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

    def fake_list(client, inbox_id, include_unauthenticated=False):
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


def test_check_mail_records_authentication_status(tmp_path, monkeypatch):
    config = make_config(tmp_path)

    fake_items = [FakeItem("msg_auth"), FakeItem("msg_unauth")]
    fake_messages = {
        "msg_auth": FakeMessage("msg_auth"),
        "msg_unauth": FakeMessage("msg_unauth"),
    }

    # Both are captured (Stage 1 preserves everything), but only msg_auth
    # is reported as authenticated by the (simulated) authenticated-only listing.
    patch_agentmail(
        monkeypatch,
        fake_items,
        authenticated_ids={"msg_auth"},
        fetch_message=lambda client, inbox_id, message_id: fake_messages[message_id],
    )

    exit_code = cli.cmd_check_mail(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    assert db.count_total_messages(conn) == 2  # neither message was dropped
    auth_row = conn.execute(
        "SELECT sender_authenticated FROM messages_raw WHERE message_id = 'msg_auth'"
    ).fetchone()
    unauth_row = conn.execute(
        "SELECT sender_authenticated FROM messages_raw WHERE message_id = 'msg_unauth'"
    ).fetchone()
    assert auth_row["sender_authenticated"] == "AUTHENTICATED"
    assert unauth_row["sender_authenticated"] == "UNAUTHENTICATED"
    conn.close()
