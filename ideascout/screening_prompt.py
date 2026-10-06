"""Versions of the screening prompt, recorded on every source_screenings row.

A screening's identity is (source, taste_version, screen_rules_sha256,
screening_prompt_version). The prompt version is what lets a prediction be
traced to the exact instructions that produced it, and what stops two prompts
with different authority rules from being mistaken for one another.

LEGACY (1): the peer-authority prompt. Taste and permanent rules were presented
as equals, with no statement of which governs. Every screening stored before
prompt provenance existed was produced by this prompt, so migration 18 labels
all existing rows with it. No prompt version was recorded before this change.

CURRENT (2): the authority-ordered prompt. Permanent rules are labelled
authoritative and outrank the inferred taste. See ideascout/shadow.py.
"""

LEGACY_SCREENING_PROMPT_VERSION = 1
CURRENT_SCREENING_PROMPT_VERSION = 2

SCREENING_PROMPT_DESCRIPTIONS = {
    LEGACY_SCREENING_PROMPT_VERSION: "legacy peer-authority prompt",
    CURRENT_SCREENING_PROMPT_VERSION: "authority-ordered prompt (permanent rules outrank taste)",
}
