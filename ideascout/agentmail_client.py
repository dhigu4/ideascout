"""Thin wrapper around the `agentmail` SDK.

This is the only file that imports the `agentmail` package. Keeping it
isolated here means that if AgentMail ever changes their SDK, the rest of
the app (db.py, cli.py) does not need to change.
"""

from __future__ import annotations

from agentmail import AgentMail

PAGE_SIZE = 100


def build_client(api_key: str) -> AgentMail:
    return AgentMail(api_key=api_key)


def list_all_message_items(
    client: AgentMail, inbox_id: str, *, include_unauthenticated: bool = False
) -> list:
    """Return metadata (no body) for every message currently in the inbox.

    Paginates through the full inbox every time rather than remembering a
    cursor between runs. This keeps duplicate-detection simple and correct
    (it relies solely on the message_id already being in the database) at
    the cost of re-listing metadata we've already seen -- a fine trade for
    a low-volume dedicated inbox.

    include_unauthenticated=False (AgentMail's own default) excludes mail
    that failed SPF/DKIM/DMARC from the results entirely. Stage 1 passes
    True so that unauthenticated mail is still captured and preserved like
    everything else; see fetch_authenticated_message_ids for how the two
    are told apart afterward.
    """
    items = []
    page_token = None
    while True:
        response = client.inboxes.messages.list(
            inbox_id=inbox_id,
            limit=PAGE_SIZE,
            page_token=page_token,
            include_unauthenticated=include_unauthenticated,
        )
        items.extend(response.messages)
        page_token = response.next_page_token
        if not page_token:
            break
    return items


def fetch_authenticated_message_ids(client: AgentMail, inbox_id: str) -> set[str]:
    """message_ids AgentMail currently considers authenticated (passed
    SPF/DKIM/DMARC) for this inbox.

    AgentMail does not expose authentication status as a field on the
    message object itself -- only as a listing-time include/exclude filter
    (`include_unauthenticated`, confirmed against AgentMail's own API
    reference). So this is computed the only way the API actually
    supports: list with that filter left at its default (which excludes
    unauthenticated mail) and collect which message_ids come back. Stage 1
    separately lists with include_unauthenticated=True to get everyone;
    comparing the two tells us, for every message, which side it's on.
    """
    items = list_all_message_items(client, inbox_id, include_unauthenticated=False)
    return {item.message_id for item in items}


def fetch_message(client: AgentMail, inbox_id: str, message_id: str):
    """Fetch the full message, including body text/html, for one message_id."""
    return client.inboxes.messages.get(inbox_id=inbox_id, message_id=message_id)


def extract_body(message) -> tuple[str, str]:
    """Return (body_raw, body_format) preferring plain text over HTML.

    body_format records which one we stored so it's never ambiguous later
    whether body_raw contains plain text or raw HTML markup.
    """
    if message.text:
        return message.text, "text"
    if message.html:
        return message.html, "html"
    return "", "none"


def format_recipients(message) -> str:
    return ", ".join(message.to) if message.to else ""
