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

# Cheap, fast Claude model well suited to short structured-extraction tasks
# like this one. Override with PARSER_MODEL_NAME in .env if desired.
DEFAULT_PARSER_MODEL_NAME = "claude-haiku-4-5"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    agentmail_api_key: str | None
    agentmail_inbox_id: str | None
    database_path: Path
    log_path: Path
    anthropic_api_key: str | None = None
    parser_model_name: str = DEFAULT_PARSER_MODEL_NAME
    brad_allowed_senders: frozenset[str] = frozenset()


def _parse_allowed_senders(raw: str | None) -> frozenset[str]:
    """BRAD_ALLOWED_SENDERS is a comma-separated list of email addresses.
    Comparisons are case-insensitive, so normalize to lowercase here once.
    """
    if not raw:
        return frozenset()
    return frozenset(address.strip().lower() for address in raw.split(",") if address.strip())


def load_config(require_agentmail: bool = False, require_llm: bool = False) -> Config:
    """Read settings from the .env file (if present) and the environment.

    Real environment variables always win over values in .env, which is the
    standard behavior people expect from a .env file.

    Set require_agentmail=True for commands that must talk to AgentMail
    (check-mail). Set require_llm=True for commands that must talk to the
    LLM provider (parse-mail). Commands that only touch the local database
    (status, show-feedback) should leave both False so they keep working
    even before credentials are configured.
    """
    load_dotenv(dotenv_path=ENV_PATH, override=False)

    api_key = os.getenv("AGENTMAIL_API_KEY") or None
    inbox_id = os.getenv("AGENTMAIL_INBOX_ID") or None
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY") or None
    parser_model_name = os.getenv("PARSER_MODEL_NAME") or DEFAULT_PARSER_MODEL_NAME
    brad_allowed_senders = _parse_allowed_senders(os.getenv("BRAD_ALLOWED_SENDERS"))

    database_path = Path(os.getenv("DATABASE_PATH") or DEFAULT_DATABASE_PATH)
    log_path = Path(os.getenv("LOG_PATH") or DEFAULT_LOG_PATH)

    missing = []
    if require_agentmail:
        if not api_key:
            missing.append("AGENTMAIL_API_KEY")
        if not inbox_id:
            missing.append("AGENTMAIL_INBOX_ID")
    if require_llm:
        if not anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY")
        if not brad_allowed_senders:
            missing.append("BRAD_ALLOWED_SENDERS")
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
        anthropic_api_key=anthropic_api_key,
        parser_model_name=parser_model_name,
        brad_allowed_senders=brad_allowed_senders,
    )
