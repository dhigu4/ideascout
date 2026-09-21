"""Clean notifier interface for eventual digest alerting. NOT ACTIVATED.

Real outbound delivery -- almost certainly via AgentMail, matching the
rest of this app -- is deliberately NOT implemented here, and no
AgentMail "send" API is invented or guessed at. This module exists so a
future, explicitly-approved implementation has an obvious, narrow place to
live, without anything today being able to accidentally send Brad
anything: DisabledNotifier.send_digest always raises, regardless of
Config.alerts_enabled, and preview-digest (see cli.py) never calls this
module at all -- it only ever prints to the terminal.

Turning this on later requires: (1) inspecting the actual installed
AgentMail SDK's send capability, (2) implementing a real Notifier here,
and (3) Brad explicitly setting ALERTS_ENABLED=true. None of those three
things happen as a side effect of anything in this stage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DigestItem:
    """The same small, deliberately spare set of fields preview-digest
    prints to the terminal -- a future real notifier should not need (or
    be given) anything more than what a human preview already shows.
    """

    company: str | None
    ticker: str | None
    source_name: str
    overall_prediction: str
    why_mispriced: str | None
    upside_case: str | None
    main_concern: str | None
    canonical_url: str


class NotifierDisabledError(RuntimeError):
    """Raised by DisabledNotifier.send_digest -- not a bug, the safety
    rail. Outbound digest delivery has not been implemented or approved
    yet.
    """


class Notifier(Protocol):
    def send_digest(self, items: list[DigestItem]) -> None: ...


class DisabledNotifier:
    """The only Notifier implementation that exists right now. Always
    raises: there is deliberately no code path anywhere in IdeaScout today
    that can send Brad anything.
    """

    def send_digest(self, items: list[DigestItem]) -> None:
        raise NotifierDisabledError(
            "Outbound digest delivery is not implemented yet -- see "
            "ideascout/notifier.py. It must stay disabled until the "
            "installed AgentMail SDK's send capability has been inspected "
            "and production sending has been explicitly approved."
        )
