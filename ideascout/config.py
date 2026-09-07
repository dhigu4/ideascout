"""Loads configuration from a .env file plus sane local defaults.

Nothing in this file talks to AgentMail or SQLite. It only answers the
question "what are the current settings?" so that every other module has
one place to get them from.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "ideas.db"
DEFAULT_LOG_PATH = PROJECT_ROOT / "data" / "app.log"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    agentmail_api_key: str | None
    agentmail_inbox_id: str | None
    database_path: Path
    log_path: Path


def load_config(require_agentmail: bool = False) -> Config:
    """Read settings from the .env file (if present) and the environment.

    Real environment variables always win over values in .env, which is the
    standard behavior people expect from a .env file.

    Set require_agentmail=True for commands that must talk to AgentMail
    (check-mail). Commands that only touch the local database (status)
    should leave it False so they keep working even before credentials are
    configured.
    """
    load_dotenv(dotenv_path=ENV_PATH, override=False)

    api_key = os.getenv("AGENTMAIL_API_KEY") or None
    inbox_id = os.getenv("AGENTMAIL_INBOX_ID") or None

    database_path = Path(os.getenv("DATABASE_PATH") or DEFAULT_DATABASE_PATH)
    log_path = Path(os.getenv("LOG_PATH") or DEFAULT_LOG_PATH)

    if require_agentmail:
        missing = []
        if not api_key:
            missing.append("AGENTMAIL_API_KEY")
        if not inbox_id:
            missing.append("AGENTMAIL_INBOX_ID")
        if missing:
            raise ConfigError(
                "Missing required setting(s): "
                + ", ".join(missing)
                + f". Add them to {ENV_PATH} (see .env.example)."
            )

    return Config(
        agentmail_api_key=api_key,
        agentmail_inbox_id=inbox_id,
        database_path=database_path,
        log_path=log_path,
    )
