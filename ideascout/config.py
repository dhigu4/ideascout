"""Loads configuration from a .env file plus sane local defaults.

Nothing in this file talks to AgentMail or SQLite. It only answers the
question "what are the current settings?" so that every other module has
one place to get them from.

IMPORTANT -- persistent state lives OUTSIDE this repository. All real data
(the database, logs, and the real .env with real credentials) lives under
DEFAULT_STATE_DIR (C:\\Users\\<you>\\IdeaScoutLocal on Windows), never under
the repo. This is deliberate: a repo checkout, a test run, or a "clean up
my test files" pass must never be able to reach production state just
because it happened to share a path with the code. See CLAUDE.md for the
full rule.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Persistent production state -- database, .env, logs, backups -- lives
# here, deliberately outside PROJECT_ROOT. Nothing in this file ever
# derives a default path from PROJECT_ROOT anymore; that was the root
# cause of an earlier incident where ad-hoc development/testing against
# the repo-local default path destroyed real production data.
DEFAULT_STATE_DIR = Path.home() / "IdeaScoutLocal"
ENV_PATH = DEFAULT_STATE_DIR / ".env"
DEFAULT_DATABASE_PATH = DEFAULT_STATE_DIR / "ideas.db"
DEFAULT_LOG_PATH = DEFAULT_STATE_DIR / "app.log"
DEFAULT_BACKUP_DIR_NAME = "backups"

# Cheap, fast Claude model well suited to short structured-extraction tasks
# like this one. Override with PARSER_MODEL_NAME in .env if desired.
DEFAULT_PARSER_MODEL_NAME = "claude-haiku-4-5"

# Taste-building (Stage 3) is a different shape of task from per-message
# feedback extraction: a holistic, nuance-preserving synthesis over Brad's
# whole accumulated judgment history, run rarely (only on explicit
# `build-taste`, and gated by the holdout freeze). That calls for
# Anthropic's most capable model rather than the cheap per-message one.
# Override with TASTE_MODEL_NAME in .env if desired.
DEFAULT_TASTE_MODEL_NAME = "claude-opus-5"

# Stage 4 (blind shadow screening): Far View's own permanent, manually
# curated screening rules live in a SEPARATE repo (InvestmentBrain), never
# in IdeaScout and never in IdeaScoutLocal. This is Brad's real path on
# his own machine; override with SCREEN_RULES_PATH in .env if it ever
# lives somewhere else. shadow-score reads this file but IdeaScout never
# writes to it.
DEFAULT_SCREEN_RULES_PATH = Path.home() / "Repos" / "InvestmentBrain" / "IDEA_SCREEN_RULES.md"

# Website-source collection (first adapter: Yellowbrick). Both live under
# IdeaScoutLocal, same as everything else real -- never under the repo.
# browser-profiles holds Chrome's OWN persistent profile data (including
# whatever cookies/session state Chrome itself manages) -- IdeaScout code
# never reads, parses, or copies anything out of it. raw holds every
# captured source document, permanently, before any AI processing.
DEFAULT_BROWSER_PROFILES_DIR = DEFAULT_STATE_DIR / "browser-profiles"
DEFAULT_RAW_STORAGE_DIR = DEFAULT_STATE_DIR / "raw"

# Alerting (outbound digest email, see ideascout/notifier.py) defaults off
# regardless of what else is configured: nothing should ever be sent
# automatically without this being explicitly turned on. Only a REAL
# `send-digest` send (never --dry-run, never preview-digest) checks this.
# Override with ALERTS_ENABLED=true in .env once ready for real sends.
DEFAULT_ALERTS_ENABLED = False


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
    taste_model_name: str = DEFAULT_TASTE_MODEL_NAME
    screen_rules_path: Path = DEFAULT_SCREEN_RULES_PATH
    browser_profiles_dir: Path = DEFAULT_BROWSER_PROFILES_DIR
    raw_storage_dir: Path = DEFAULT_RAW_STORAGE_DIR
    alerts_enabled: bool = DEFAULT_ALERTS_ENABLED
    digest_recipient_email: str | None = None


def _parse_allowed_senders(raw: str | None) -> frozenset[str]:
    """BRAD_ALLOWED_SENDERS is a comma-separated list of email addresses.
    Comparisons are case-insensitive, so normalize to lowercase here once.
    """
    if not raw:
        return frozenset()
    return frozenset(address.strip().lower() for address in raw.split(",") if address.strip())


def find_legacy_repo_env() -> Path | None:
    """Return the path of an old .env sitting in the repo root, if one
    exists. Production .env now lives only at ENV_PATH (under
    DEFAULT_STATE_DIR) -- this is used purely to print a one-time-per-run
    warning telling a human to move it. It is never read for configuration
    and never moved/copied/deleted automatically.
    """
    legacy_path = PROJECT_ROOT / ".env"
    return legacy_path if legacy_path.exists() else None


def _parse_bool_env(raw: str | None, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def load_config(
    require_agentmail: bool = False,
    require_llm: bool = False,
    require_taste: bool = False,
    require_shadow: bool = False,
    require_source: bool = False,
) -> Config:
    """Read settings from the .env file (if present) and the environment.

    Real environment variables always win over values in .env, which is the
    standard behavior people expect from a .env file. .env is read only
    from ENV_PATH (under DEFAULT_STATE_DIR) -- an old repo-root .env is
    never read here, see find_legacy_repo_env().

    Set require_agentmail=True for commands that must talk to AgentMail
    (check-mail). Set require_llm=True for commands that must talk to the
    feedback-extraction LLM (parse-mail). Set require_taste=True for
    commands that must talk to the taste-building LLM (build-taste) --
    this shares ANTHROPIC_API_KEY with require_llm but does NOT need
    BRAD_ALLOWED_SENDERS, since taste-building never looks at raw email,
    only already-eligible feedback rows. Set require_shadow=True for
    shadow-score (Stage 4): shares ANTHROPIC_API_KEY the same way -- shadow
    screening never looks at raw email either, only already-isolated
    source text -- and does NOT check screen_rules_path exists here;
    cmd_shadow_score checks that itself so the failure message is specific
    to shadow-screening rather than a generic config error. Set
    require_source=True for website-source commands (source-login,
    collect-source): shares ANTHROPIC_API_KEY the same way (collection's
    extraction/screening steps need it; discovery/fetching alone do not,
    but source-login and collect-source both load config the same way for
    simplicity). Website credentials themselves are never part of Config
    at all -- authentication lives entirely in Chrome's own persistent
    profile (browser_profiles_dir), which this function only ever computes
    a PATH to, never reads the contents of. Commands that only touch the
    local database (status, show-feedback, taste-status, shadow-status,
    show-shadow-results, source-status, preview-digest) should leave all
    five False so they keep working even before credentials are configured.
    """
    load_dotenv(dotenv_path=ENV_PATH, override=False)

    api_key = os.getenv("AGENTMAIL_API_KEY") or None
    inbox_id = os.getenv("AGENTMAIL_INBOX_ID") or None
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY") or None
    parser_model_name = os.getenv("PARSER_MODEL_NAME") or DEFAULT_PARSER_MODEL_NAME
    taste_model_name = os.getenv("TASTE_MODEL_NAME") or DEFAULT_TASTE_MODEL_NAME
    brad_allowed_senders = _parse_allowed_senders(os.getenv("BRAD_ALLOWED_SENDERS"))

    database_path = Path(os.getenv("DATABASE_PATH") or DEFAULT_DATABASE_PATH)
    log_path = Path(os.getenv("LOG_PATH") or DEFAULT_LOG_PATH)
    screen_rules_path = Path(os.getenv("SCREEN_RULES_PATH") or DEFAULT_SCREEN_RULES_PATH)
    browser_profiles_dir = Path(os.getenv("BROWSER_PROFILES_DIR") or DEFAULT_BROWSER_PROFILES_DIR)
    raw_storage_dir = Path(os.getenv("RAW_STORAGE_DIR") or DEFAULT_RAW_STORAGE_DIR)
    alerts_enabled = _parse_bool_env(os.getenv("ALERTS_ENABLED"), DEFAULT_ALERTS_ENABLED)

    # send-digest's "to" address. Prefers an explicit DIGEST_RECIPIENT_EMAIL;
    # falls back to BRAD_ALLOWED_SENDERS only when that set has EXACTLY one
    # address, since that's the only case where reusing it is unambiguous --
    # BRAD_ALLOWED_SENDERS is documented as an inbound-only allowlist (who
    # may submit ideas), not a recipient list, so a multi-address set is
    # never guessed at.
    digest_recipient_email = os.getenv("DIGEST_RECIPIENT_EMAIL") or None
    if not digest_recipient_email and len(brad_allowed_senders) == 1:
        digest_recipient_email = next(iter(brad_allowed_senders))

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
    if require_taste:
        if not anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY")
    if require_shadow:
        if not anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY")
    if require_source:
        if not anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY")
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
        taste_model_name=taste_model_name,
        screen_rules_path=screen_rules_path,
        browser_profiles_dir=browser_profiles_dir,
        raw_storage_dir=raw_storage_dir,
        alerts_enabled=alerts_enabled,
        digest_recipient_email=digest_recipient_email,
    )
