"""Unit tests for ideascout/notifier.py (Stage 6: real AgentMail-backed
digest delivery). Pure rendering/composition tests plus a mocked-client
send test -- no real network call, no real AgentMail credentials.
"""

from __future__ import annotations

import pytest

from ideascout import notifier


def make_item(**overrides) -> notifier.DigestItem:
    fields = dict(
        company="XYZ Corp",
        ticker="XYZ",
        source_name="yellowbrick",
        canonical_url="https://www.joinyellowbrick.com/sp/123",
        overall_prediction="INVESTIGATE_NOW",
        confidence="HIGH",
        mispricing="Strong",
        variant_perception="Plausible",
        upside="Potentially sufficient",
        business_quality="Plausible",
        downside="Acceptable",
        key_reasons=["Hidden earnings power.", "Cheap on normalized basis."],
        key_concerns=["Execution risk.", "Customer concentration."],
        critical_questions=["When does the segment breakeven?"],
    )
    fields.update(overrides)
    return notifier.DigestItem(**fields)


# --- rendering: pure string composition, no LLM ------------------------------


def test_digest_subject_uses_exact_template():
    assert notifier.digest_subject("2026-09-23") == "IdeaScout — New Ideas Worth Attention — 2026-09-23"


def test_render_digest_body_includes_all_required_fields():
    body = notifier.render_digest_body([make_item()])

    assert "XYZ Corp" in body
    assert "(XYZ)" in body
    assert "Source: yellowbrick" in body
    assert "Overall result: INVESTIGATE_NOW" in body
    assert "Confidence: HIGH" in body
    assert "Why potentially interesting:" in body
    assert "Hidden earnings power." in body
    assert "Cheap on normalized basis." in body
    assert "Main concerns:" in body
    assert "Execution risk." in body
    assert "Customer concentration." in body
    assert "Key question:" in body
    assert "When does the segment breakeven?" in body
    assert "Screen dimensions:" in body
    assert "Mispricing: Strong" in body
    assert "Variant perception: Plausible" in body
    assert "Upside: Potentially sufficient" in body
    assert "Business quality: Plausible" in body
    assert "Downside: Acceptable" in body
    assert "Original source: https://www.joinyellowbrick.com/sp/123" in body


def test_render_digest_body_caps_reasons_concerns_and_questions():
    item = make_item(
        key_reasons=["r1", "r2", "r3", "r4"],
        key_concerns=["c1", "c2", "c3"],
        critical_questions=["q1", "q2"],
    )
    body = notifier.render_digest_body([item])

    assert "r1" in body and "r2" in body
    assert "r3" not in body and "r4" not in body
    assert "c1" in body and "c2" in body
    assert "c3" not in body
    assert "q1" in body
    assert "q2" not in body


def test_render_digest_body_handles_empty_lists_without_inventing_content():
    item = make_item(key_reasons=[], key_concerns=[], critical_questions=[])
    body = notifier.render_digest_body([item])
    assert "(none noted)" in body


def test_render_digest_body_joins_multiple_items_with_a_separator():
    body = notifier.render_digest_body([make_item(company="XYZ Corp"), make_item(company="ABC Inc", ticker="ABC")])
    assert "XYZ Corp" in body
    assert "ABC Inc" in body
    assert body.index("XYZ Corp") < body.index("ABC Inc")


def test_render_digest_body_falls_back_when_company_missing():
    item = make_item(company=None, ticker="XYZ")
    body = notifier.render_digest_body([item])
    assert "XYZ" in body


# --- idempotency key -----------------------------------------------------------


def test_digest_idempotency_key_is_deterministic_and_order_independent():
    key_a = notifier.digest_idempotency_key("2026-09-23", [5, 2, 8])
    key_b = notifier.digest_idempotency_key("2026-09-23", [8, 5, 2])
    assert key_a == key_b


def test_digest_idempotency_key_differs_by_date_or_source_ids():
    base = notifier.digest_idempotency_key("2026-09-23", [1, 2])
    assert notifier.digest_idempotency_key("2026-09-24", [1, 2]) != base
    assert notifier.digest_idempotency_key("2026-09-23", [1, 3]) != base


# --- send_digest_email: delegates to agentmail_client, no real network ------


class _FakeSendResponse:
    def __init__(self, message_id="msg_123", thread_id="thread_456"):
        self.message_id = message_id
        self.thread_id = thread_id


class _FakeMessages:
    def __init__(self, response=None, exc=None):
        self._response = response
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


class _FakeClient:
    def __init__(self, messages):
        self.inboxes = _FakeInboxes(messages)


def test_send_digest_email_delegates_with_exact_fields():
    messages = _FakeMessages(response=_FakeSendResponse())
    client = _FakeClient(messages)

    result = notifier.send_digest_email(
        client,
        inbox_id="inbox_abc",
        recipient_email="brad@example.com",
        subject="IdeaScout — New Ideas Worth Attention — 2026-09-23",
        body="body text",
        idempotency_key="ideascout-digest:2026-09-23:1-2",
    )

    assert result.message_id == "msg_123"
    assert len(messages.calls) == 1
    call = messages.calls[0]
    assert call["inbox_id"] == "inbox_abc"
    assert call["to"] == "brad@example.com"
    assert call["subject"] == "IdeaScout — New Ideas Worth Attention — 2026-09-23"
    assert call["text"] == "body text"
    assert call["idempotency_key"] == "ideascout-digest:2026-09-23:1-2"


def test_send_digest_email_propagates_exceptions_uncaught():
    messages = _FakeMessages(exc=RuntimeError("simulated AgentMail failure"))
    client = _FakeClient(messages)

    with pytest.raises(RuntimeError, match="simulated AgentMail failure"):
        notifier.send_digest_email(
            client,
            inbox_id="inbox_abc",
            recipient_email="brad@example.com",
            subject="subject",
            body="body",
            idempotency_key="key",
        )
