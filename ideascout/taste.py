"""Thin adapter around the Anthropic API for synthesizing Far View Idea
Taste (Stage 3) from Brad's accumulated eligible feedback.

This is the only file (besides parser.py, for the separate per-message
feedback-extraction task) that imports the `anthropic` SDK for taste work.
Isolating it here means the taste-building model/provider can change
later without touching db.py or cli.py.

IMPORTANT: this module only ever reasons about the feedback records
handed to it by the caller -- which must always come from
db.get_feedback_eligible_for_learning() (directly, or filtered by
db.get_eligible_feedback_after(), which itself calls that same canonical
query). It never has access to, and must never be given, the underlying
investment writeups those feedback records refer to -- only Brad's own
verdicts and natural-language comments about them.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import List, Literal

import pydantic
from pydantic import BaseModel, Field

# Also referenced by cli.py; kept here so the numbers only exist in one place.
MIN_TRAINING_RECORDS = 15
HOLDOUT_SIZE = 20
MAX_CANDIDATE_RULES = 5

# Headroom for a response that must never be truncated mid-JSON/mid-section.
# max_tokens is a ceiling, not a target -- billing is for tokens actually
# produced, so raising this costs nothing on its own and does not make
# outputs more verbose; the system prompts' own compactness instructions
# are unchanged. A real production run (19 training records) hit the old
# 2048-token candidate-rules ceiling mid-string; these are set with real
# margin above what 5 short structured rules or 8 compact prose sections
# should ever need.
MAX_OUTPUT_TOKENS_TASTE_BODY = 6144
MAX_OUTPUT_TOKENS_CANDIDATE_RULES = 4096

# One initial attempt plus up to 2 retries, per spec.
MAX_GENERATION_ATTEMPTS = 3

_RETRY_REMINDER_SUFFIX = (
    "\n\nIMPORTANT: a previous attempt at this exact request did not fit in the "
    "available output space. Be more concise this time -- still cover everything "
    "asked for, just more tersely -- so the complete response fits well within "
    "the token limit."
)

IDEA_TASTE_SECTION_HEADERS = [
    "Strong Positive Signals",
    "Strong Negative Signals",
    "Context-Dependent Signals",
    "Mispricing Patterns",
    "Upside / Asymmetry Preferences",
    "Risk Tolerance",
    "Situation / Company Preferences",
    "Areas Still Uncertain",
]

IDEA_TASTE_SYSTEM_PROMPT = """\
You are building a compact, honest summary of one investor's ("Brad's") \
idea-selection taste, based ONLY on his own real feedback on real ideas \
that were shown to him. This will eventually be reused inside future \
screening prompts, so it must be accurate, not flattering or invented.

You will be given a numbered list of Brad's judgments. Each one may be:
- FEEDBACK, with a verdict of STRONG_LIKE, LIKE, MAYBE, PASS, or STRONG_PASS;
- NEW_IDEA, flagging something new he wants tracked (often no verdict);
- MISSED_IDEA, flagging something he wishes had been surfaced earlier \
  (often no verdict).

For every judgment you may see his verdict (if any), and his own words: a \
comment, and/or lists of positive reasons, concerns, and things he \
explicitly said were NOT a concern.

Your job is to infer Brad's general preferences from this evidence and \
write EXACTLY the following eight '##' sections, in this order, and no \
others:

## Strong Positive Signals
## Strong Negative Signals
## Context-Dependent Signals
## Mispricing Patterns
## Upside / Asymmetry Preferences
## Risk Tolerance
## Situation / Company Preferences
## Areas Still Uncertain

Hard rules:

1. Infer preferences ONLY from Brad's own judgments below. You have not \
   been given, and must not imagine, the underlying investment writeups \
   those judgments refer to -- reason about Brad's reaction, never about \
   whether the idea itself was objectively good.
2. Weight Brad's natural-language reasoning (his comments, his stated \
   reasons and concerns) more heavily than the bare categorical verdict. \
   A LIKE with a rich, specific explanation is stronger evidence than a \
   LIKE with none.
3. Distinguish a pattern seen repeatedly across multiple judgments from a \
   single one-off comment. Never turn one observation into a general \
   rule -- if you only have one data point, say so explicitly ("a single \
   judgment suggests...") rather than stating it as a settled preference.
4. Preserve contradictions and nuance instead of averaging them away. If \
   Brad liked something in one case and passed on something similar in \
   another, describe both and try to articulate what differed between \
   them, rather than picking one side.
5. Distinguish hard dislikes (things Brad appears to reject regardless of \
   context) from conditional acceptances (things he dislikes by default \
   but has accepted, or said he would accept, under specific conditions).
6. Recognize interactions between variables rather than reducing them to \
   single-factor rules. For example, a cyclical business might normally \
   be unattractive to Brad but still draw interest at a severe trough \
   with exceptional company quality and enough upside -- if the evidence \
   supports something like that, describe the interaction, not the \
   flattened version ("Brad dislikes cyclicals"). Only describe an \
   interaction if the evidence actually supports it; do not invent one.
7. Where relevant and supported by evidence, test (do not assume) whether \
   Brad's comments distinguish between things like: future earnings \
   differentiation vs. multiple rerating; hidden earnings power vs. \
   merely cheap valuation; meaningful corporate change vs. cosmetic \
   restructuring; large upside vs. modest upside; genuine mispricing vs. \
   ordinary uncertainty. These are examples of distinctions worth \
   checking against the actual feedback -- do not state them as \
   conclusions unless the evidence actually shows Brad drawing that line.
8. Use cautious, calibrated wording whenever evidence is limited or mixed \
   -- phrases like "appears to prefer", "early evidence suggests", or \
   "mixed evidence" -- rather than confident, absolute language. Reserve \
   confident language for patterns seen repeatedly and consistently.
9. If a section has no real evidence yet, say so plainly (e.g. "No clear \
   evidence yet on this.") rather than filling it with speculation.
10. Keep the whole document compact -- this will be reused inside future \
    screening prompts, so cost and length matter. Prefer dense, specific \
    bullet points over long paragraphs.

Output ONLY the eight sections and their content in markdown. Do not \
include a title, a version line, or any preamble -- those are added \
separately.\
"""

CANDIDATE_RULES_SYSTEM_PROMPT = """\
You are proposing a small number of CANDIDATE durable investment-screening \
rules, based only on the same real feedback judgments from Brad described \
below. These are proposals for a human (Brad) to review -- they are never \
applied automatically, and you are not deciding anything.

Propose at most 5 candidate rules. Propose fewer, or none, if the evidence \
doesn't support 5 genuinely distinct, durable patterns -- do not pad the \
list to reach 5.

A candidate rule should describe something that looks likely to remain \
true about Brad's taste going forward, not a one-off reaction to a single \
idea. Prefer rules backed by multiple consistent judgments. A rule backed \
by only one or two judgments should be marked LOW confidence and its \
evidence should say so plainly.

For each candidate rule, provide:
- rule: the proposed durable rule itself, stated plainly and specifically \
  (not vague).
- evidence: which judgments and pattern actually support this, including \
  whether it is one-off or repeated evidence.
- reusability: why this looks like something that generalizes beyond the \
  specific ideas seen so far, rather than being circumstantial to them.
- confidence: HIGH (multiple consistent, unambiguous judgments), MEDIUM \
  (some support but with caveats or limited data), or LOW (thin, mixed, \
  or single-judgment evidence).

Do not invent reasoning Brad did not express. Do not draw on any \
knowledge of the underlying investment ideas themselves -- only on Brad's \
own reaction to them as given below.\
"""


class CandidateRule(BaseModel):
    rule: str
    evidence: str
    reusability: str
    confidence: Literal["HIGH", "MEDIUM", "LOW"]


class CandidateRuleBatch(BaseModel):
    rules: List[CandidateRule] = Field(default_factory=list)


class TasteGenerationError(RuntimeError):
    """Raised when the taste-building LLM call fails, or returns unusable
    output after every retry has been exhausted.
    """


class _RetryableGenerationError(RuntimeError):
    """Internal only: one failed attempt within the retry loop below (a
    truncated response, or one that failed JSON/schema validation). Never
    raised out of this module -- generate_idea_taste_body and
    generate_candidate_rules catch this themselves and either retry or
    re-raise as a TasteGenerationError once attempts are exhausted.
    """


class TasteIntegrityError(RuntimeError):
    """Raised when a taste version's authoritative artifact on disk is
    missing or no longer matches its stored/expected SHA-256 hash. This is
    the fail-loud signal taste-status uses to report artifact corruption
    instead of silently trusting a file that may have been edited,
    truncated, or only partially written.
    """


def build_client(api_key: str):
    import anthropic

    return anthropic.Anthropic(api_key=api_key)


def _candidate_rules_output_config() -> dict:
    """Build the same "strict" JSON-schema output_config that
    client.messages.parse(output_format=CandidateRuleBatch) would build
    internally, by reusing the SDK's own (private but stable) schema
    transform. This is needed because only .parse()/.stream() accept the
    convenient output_format=SomeType shortcut, but .parse() raises
    immediately on truncated/malformed JSON with no access to the raw
    response -- so there's no way to inspect stop_reason before it fails.
    Calling plain .create() with this output_config gives us the raw
    Message (stop_reason and all) ourselves, while still getting the same
    schema-constrained structured output .parse() would have requested.
    """
    from anthropic.lib._parse._transform import transform_schema

    return {"format": {"type": "json_schema", "schema": transform_schema(CandidateRuleBatch)}}


def training_records_from_rows(rows) -> list[dict]:
    """Convert feedback rows (sqlite3.Row, as returned by
    db.get_feedback_eligible_for_learning) into plain dicts carrying
    exactly what the taste prompts need: the columns already on the row,
    plus positive_reasons/concerns/not_a_concern pulled out of parsed_json
    (the only place those live -- they aren't their own columns).
    """
    records = []
    for row in rows:
        extra: dict = {}
        raw_json = row["parsed_json"]
        if raw_json:
            try:
                extra = json.loads(raw_json)
            except (ValueError, TypeError):
                extra = {}
        records.append(
            {
                "feedback_id": row["feedback_id"],
                "event_type": row["event_type"],
                "verdict": row["verdict"],
                "ticker": row["ticker"],
                "company": row["company"],
                "user_comment": row["user_comment"],
                "positive_reasons": extra.get("positive_reasons") or [],
                "concerns": extra.get("concerns") or [],
                "not_a_concern": extra.get("not_a_concern") or [],
            }
        )
    return records


def format_training_corpus(records: list[dict]) -> str:
    lines = []
    for index, record in enumerate(records, start=1):
        label = record.get("ticker") or record.get("company") or "(no ticker/company given)"
        verdict = record.get("verdict") or "(no verdict)"
        lines.append(f"### Judgment {index} -- {label}")
        lines.append(f"event_type: {record['event_type']}")
        lines.append(f"verdict: {verdict}")
        if record.get("user_comment"):
            lines.append(f"Brad's comment: {record['user_comment']}")
        if record.get("positive_reasons"):
            lines.append(f"Positive reasons Brad gave: {'; '.join(record['positive_reasons'])}")
        if record.get("concerns"):
            lines.append(f"Concerns Brad raised: {'; '.join(record['concerns'])}")
        if record.get("not_a_concern"):
            lines.append(
                f"Explicitly NOT a concern to Brad: {'; '.join(record['not_a_concern'])}"
            )
        lines.append("")
    return "\n".join(lines)


def _log(logger, message: str) -> None:
    if logger is not None:
        logger.warning(message)


def generate_idea_taste_body(client, model_name: str, records: list[dict], logger=None) -> str:
    """Returns just the eight-section markdown body -- the caller
    (cli.py) prepends the deterministic title/version/date/count header so
    that metadata is always exactly correct, never left to the model.

    Retries up to MAX_GENERATION_ATTEMPTS total attempts if the response is
    truncated (stop_reason == "max_tokens") or comes back empty. A genuine
    API-call failure (network, auth, etc.) is NOT retried here -- it is
    raised immediately, since retrying it would just repeat the same
    failure.
    """
    corpus = format_training_corpus(records)
    base_user_content = (
        f"Here are {len(records)} of Brad's real, eligible judgments, oldest first:\n\n{corpus}"
    )

    last_error: Exception | None = None
    for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
        user_content = base_user_content + (_RETRY_REMINDER_SUFFIX if attempt > 1 else "")

        try:
            response = client.messages.create(
                model=model_name,
                max_tokens=MAX_OUTPUT_TOKENS_TASTE_BODY,
                system=IDEA_TASTE_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
        except Exception as exc:
            raise TasteGenerationError(f"LLM call failed while generating idea-taste.md: {exc}") from exc

        try:
            if response.stop_reason == "max_tokens":
                raise _RetryableGenerationError(
                    f"idea-taste response truncated at output limit (attempt {attempt}/{MAX_GENERATION_ATTEMPTS})"
                )

            text = next((block.text for block in response.content if block.type == "text"), "").strip()
            if not text:
                raise _RetryableGenerationError(
                    f"idea-taste response was empty (attempt {attempt}/{MAX_GENERATION_ATTEMPTS})"
                )
        except _RetryableGenerationError as exc:
            last_error = exc
            will_retry = attempt < MAX_GENERATION_ATTEMPTS
            _log(logger, f"{exc}{'; retrying' if will_retry else '; no attempts left'}")
            continue

        return text

    raise TasteGenerationError(
        f"Failed to generate idea-taste.md after {MAX_GENERATION_ATTEMPTS} attempts: {last_error}"
    )


def generate_candidate_rules(
    client, model_name: str, records: list[dict], logger=None
) -> list[CandidateRule]:
    """Returns at most MAX_CANDIDATE_RULES CandidateRule objects.

    Retries up to MAX_GENERATION_ATTEMPTS total attempts for the specific
    failure modes that mean the response was unusable but the API call
    itself worked: truncation (stop_reason == "max_tokens"), and malformed
    or incomplete JSON / a schema mismatch (pydantic.ValidationError from
    validating the raw text -- this covers both "not valid JSON at all"
    and "valid JSON that doesn't match the expected shape"). Never repairs
    or guesses at broken JSON with string/regex patching -- an attempt
    either validates cleanly or is discarded and retried.

    Uses plain .create() with a hand-built structured-output config
    (_candidate_rules_output_config) rather than .parse(location), purely
    so stop_reason can be inspected BEFORE attempting to parse -- see that
    function's docstring.
    """
    corpus = format_training_corpus(records)
    base_user_content = (
        f"Here are {len(records)} of Brad's real, eligible judgments, oldest first:\n\n{corpus}"
    )
    output_config = _candidate_rules_output_config()

    last_error: Exception | None = None
    for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
        user_content = base_user_content + (_RETRY_REMINDER_SUFFIX if attempt > 1 else "")

        try:
            response = client.messages.create(
                model=model_name,
                max_tokens=MAX_OUTPUT_TOKENS_CANDIDATE_RULES,
                system=CANDIDATE_RULES_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
                output_config=output_config,
            )
        except Exception as exc:
            raise TasteGenerationError(
                f"LLM call failed while generating candidate-permanent-rules.md: {exc}"
            ) from exc

        try:
            if response.stop_reason == "max_tokens":
                raise _RetryableGenerationError(
                    "candidate rules response truncated at output limit "
                    f"(attempt {attempt}/{MAX_GENERATION_ATTEMPTS})"
                )

            text = next((block.text for block in response.content if block.type == "text"), "")
            try:
                batch = CandidateRuleBatch.model_validate_json(text)
            except pydantic.ValidationError as exc:
                raise _RetryableGenerationError(
                    "candidate rules response was malformed/incomplete JSON "
                    f"(attempt {attempt}/{MAX_GENERATION_ATTEMPTS}): {exc}"
                ) from exc
        except _RetryableGenerationError as exc:
            last_error = exc
            will_retry = attempt < MAX_GENERATION_ATTEMPTS
            _log(logger, f"{exc}{'; retrying' if will_retry else '; no attempts left'}")
            continue

        return batch.rules[:MAX_CANDIDATE_RULES]

    raise TasteGenerationError(
        f"Failed to generate candidate-permanent-rules.md after {MAX_GENERATION_ATTEMPTS} attempts: {last_error}"
    )


def render_idea_taste_document(
    *, version_label: str, generated_date: str, training_count: int, body: str
) -> str:
    header = (
        "# Far View Idea Taste\n\n"
        f"Version: {version_label}\n"
        f"Generated: {generated_date}\n"
        f"Training judgments: {training_count}\n\n"
    )
    return header + body.strip() + "\n"


def render_candidate_rules_document(
    *, version_label: str, generated_date: str, training_count: int, rules: list[CandidateRule]
) -> str:
    lines = [
        "# Candidate Permanent Rules",
        "",
        f"Generated: {generated_date}",
        f"Based on taste version: {version_label} ({training_count} training judgments)",
        "",
        "These are proposals only. They have NOT been added to "
        "IDEA_SCREEN_RULES.md and never will be automatically -- review "
        "and add them there yourself, manually, only if you agree.",
        "",
    ]

    if not rules:
        lines.append("No candidate rules were proposed from this training set.")
        return "\n".join(lines) + "\n"

    for index, rule in enumerate(rules, start=1):
        lines.append(f"## Candidate {index}")
        lines.append("")
        lines.append(f"**Candidate rule:** {rule.rule}")
        lines.append(f"**Evidence:** {rule.evidence}")
        lines.append(f"**Why it may be reusable:** {rule.reusability}")
        lines.append(f"**Confidence:** {rule.confidence}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# --- Artifact hashing / integrity --------------------------------------------
#
# A taste version's authoritative artifact is whatever file its
# taste_versions.idea_taste_path row points at -- for taste v1 (built
# before per-version immutable directories existed) that is the canonical
# idea-taste.md itself; for every version built after this fix it is the
# immutable file under taste-versions/vN/. Either way, these helpers only
# ever look at "the path the DB row says is authoritative" -- they never
# guess or fall back to the canonical convenience copy.


def compute_content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def compute_file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_file_sha256(path: Path, expected_sha256: str, *, label: str) -> None:
    """Raise TasteIntegrityError unless `path` exists and its content hashes
    to exactly `expected_sha256`. Used both right after writing a new
    version's artifact (before it is allowed to become active) and by
    taste-status to check an already-active version's artifact has not
    since been altered or lost.
    """
    if not path.exists():
        raise TasteIntegrityError(f"{label} artifact is missing: {path}")
    actual = compute_file_sha256(path)
    if actual != expected_sha256:
        raise TasteIntegrityError(
            f"{label} artifact hash mismatch: expected {expected_sha256}, got {actual} ({path})"
        )


def verify_taste_version_integrity(taste_version_row) -> None:
    """Verify a taste_versions row's authoritative idea-taste artifact
    still matches its stored SHA-256. This is what taste-status calls to
    report "Artifact integrity: OK" -- or raise TasteIntegrityError, which
    the caller treats as a loud failure rather than a silent pass.
    """
    verify_file_sha256(
        Path(taste_version_row["idea_taste_path"]),
        taste_version_row["idea_taste_sha256"],
        label=f"idea-taste.md ({taste_version_row['version_label']})",
    )
