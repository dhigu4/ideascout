"""Outbound digest email delivery (Stage 6), via AgentMail.

Gated ENTIRELY by cli.py's cmd_send_digest -- nothing in this module
decides whether sending is currently allowed. A real send only ever
happens when Config.alerts_enabled is True AND --dry-run was not passed;
preview-digest never imports this module at all. This module's only job
is "given already-selected, already-stored data, render and send one
email" -- it never calls an LLM, never rewrites or invents any prose, and
never decides which ideas are eligible (see cli.py's
select_digest_candidates, shared with preview-digest).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from . import agentmail_client

DIGEST_SUBJECT_TEMPLATE = "IdeaScout — New Ideas Worth Attention — {date}"


@dataclass(frozen=True)
class DigestItem:
    """One digest-eligible idea -- holds ONLY fields already produced by
    collection/extraction/screening. key_reasons/key_concerns/
    critical_questions are expected to already be capped (max 2/2/1) by
    the caller before constructing this; this module trims defensively
    too, but never pads or invents content to fill a slot.
    """

    company: str | None
    ticker: str | None
    source_name: str
    canonical_url: str
    overall_prediction: str
    confidence: str
    mispricing: str
    variant_perception: str
    upside: str
    business_quality: str
    downside: str
    key_reasons: List[str] = field(default_factory=list)
    key_concerns: List[str] = field(default_factory=list)
    critical_questions: List[str] = field(default_factory=list)


def digest_subject(date_str: str) -> str:
    return DIGEST_SUBJECT_TEMPLATE.format(date=date_str)


def _format_item(item: DigestItem) -> str:
    label = item.company or item.ticker or "(unknown)"
    ticker_suffix = f" ({item.ticker})" if item.ticker and item.ticker != label else ""

    lines = [
        f"{label}{ticker_suffix}",
        f"Source: {item.source_name}",
        f"Overall result: {item.overall_prediction}",
        f"Confidence: {item.confidence}",
        "",
        "Why potentially interesting:",
    ]
    lines.extend(f"- {reason}" for reason in (item.key_reasons[:2] or ["(none noted)"]))
    lines.append("")
    lines.append("Main concerns:")
    lines.extend(f"- {concern}" for concern in (item.key_concerns[:2] or ["(none noted)"]))
    lines.append("")
    lines.append("Key question:")
    lines.append(f"- {item.critical_questions[0] if item.critical_questions else '(none noted)'}")
    lines.append("")
    lines.append("Screen dimensions:")
    lines.append(f"- Mispricing: {item.mispricing}")
    lines.append(f"- Variant perception: {item.variant_perception}")
    lines.append(f"- Upside: {item.upside}")
    lines.append(f"- Business quality: {item.business_quality}")
    lines.append(f"- Downside: {item.downside}")
    lines.append("")
    lines.append(f"Original source: {item.canonical_url}")
    return "\n".join(lines)


def render_digest_body(items: List[DigestItem]) -> str:
    """Plain-text digest email body -- pure string composition, no LLM
    call, no rewriting. Every value is taken verbatim from `items`, which
    itself only ever carries already-stored extraction/screening fields
    (see cli.py's _digest_item_from_row).
    """
    separator = "\n\n" + ("-" * 40) + "\n\n"
    return separator.join(_format_item(item) for item in items)


def digest_idempotency_key(date_str: str, source_ids: List[int]) -> str:
    """A deterministic key tying one send attempt to the EXACT set of
    source_ids it covers, for AgentMail's own idempotency_key (see
    agentmail_client.send_message): a retry using the identical key AND
    identical message content returns the original send instead of
    creating a duplicate. This matters specifically for the "email
    succeeded but digest_shown_sources marking then failed" case --
    candidates would be unchanged on an immediate retry, so the key would
    be identical too, and AgentMail's own idempotency prevents a second
    real send even before cli.py's own DB check would catch it. A later,
    genuinely different digest naturally gets a different key (different
    date and/or different source_id set), so this never blocks real work.
    """
    ids_part = "-".join(str(source_id) for source_id in sorted(source_ids))
    return f"ideascout-digest:{date_str}:{ids_part}"


def send_digest_email(
    client, *, inbox_id: str, recipient_email: str, subject: str, body: str, idempotency_key: str
):
    """Sends exactly one digest email. Any exception from the underlying
    AgentMail call propagates uncaught -- callers must treat ANY
    exception as "the send did not happen" and must NEVER mark sources
    shown in that case. Returns AgentMail's SendMessageResponse
    (message_id/thread_id) on success.
    """
    return agentmail_client.send_message(
        client,
        inbox_id=inbox_id,
        to=recipient_email,
        subject=subject,
        text=body,
        idempotency_key=idempotency_key,
    )
