"""Shared plumbing for one Anthropic structured-output call with
truncation-aware retry.

This is the exact pattern taste.py's generate_candidate_rules developed
after a real production failure: client.messages.parse()'s convenience
wrapper validates JSON internally and raises immediately on truncated
output, with no access to the raw response's stop_reason. Both Stage 4
call sites (idea_extraction.py and shadow.py) need the identical fix --
plain client.messages.create() with a hand-built structured-output
config, so stop_reason can be checked BEFORE parsing is attempted, plus a
bounded number of retries with a "be concise" reminder -- so it lives
here once instead of being copied twice. taste.py is left exactly as it
is (already tested, already in production) rather than refactored onto
this shared helper.
"""

from __future__ import annotations

from typing import Type, TypeVar

import pydantic
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

RETRY_REMINDER_SUFFIX = (
    "\n\nIMPORTANT: a previous attempt at this exact request did not fit in the "
    "available output space, or was not valid. Be more concise this time -- still "
    "cover everything asked for, just more tersely -- so the complete response "
    "fits well within the token limit and is valid JSON."
)


class StructuredGenerationError(RuntimeError):
    """Raised when a structured-output LLM call fails outright, or returns
    unusable output after every retry attempt has been exhausted.
    """


class _RetryableGenerationError(RuntimeError):
    """Internal only: one failed attempt (truncated response, or one that
    failed JSON/schema validation). Never raised out of this module --
    generate_structured catches this itself and either retries or raises
    StructuredGenerationError once attempts are exhausted.
    """


def output_config_for(output_model: Type[BaseModel]) -> dict:
    """Build the same "strict" JSON-schema output_config that
    client.messages.parse(output_format=output_model) would build
    internally, by reusing the SDK's own (private but stable) schema
    transform -- see taste.py's _candidate_rules_output_config for the
    original discovery of this approach and why plain .create() is used
    instead of .parse().
    """
    from anthropic.lib._parse._transform import transform_schema

    return {"format": {"type": "json_schema", "schema": transform_schema(output_model)}}


def generate_structured(
    client,
    *,
    model_name: str,
    system_prompt: str,
    user_content: str,
    output_model: Type[T],
    max_output_tokens: int,
    max_attempts: int,
    label: str,
    logger=None,
) -> T:
    """Call `client.messages.create` up to `max_attempts` times, retrying
    only on truncation (stop_reason == "max_tokens") or malformed/
    incomplete JSON (pydantic.ValidationError) -- never on a genuine API
    failure (network, auth, etc.), which is raised immediately since
    retrying it would just repeat the same failure. Never repairs or
    guesses at broken JSON with string/regex patching: an attempt either
    validates cleanly against `output_model`, or is discarded and retried.
    """
    output_config = output_config_for(output_model)
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        content = user_content + (RETRY_REMINDER_SUFFIX if attempt > 1 else "")

        try:
            response = client.messages.create(
                model=model_name,
                max_tokens=max_output_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": content}],
                output_config=output_config,
            )
        except Exception as exc:
            raise StructuredGenerationError(f"LLM call failed while {label}: {exc}") from exc

        try:
            if response.stop_reason == "max_tokens":
                raise _RetryableGenerationError(
                    f"{label}: response truncated at output limit (attempt {attempt}/{max_attempts})"
                )

            text = next((block.text for block in response.content if getattr(block, "type", None) == "text"), "")
            try:
                return output_model.model_validate_json(text)
            except pydantic.ValidationError as exc:
                raise _RetryableGenerationError(
                    f"{label}: response was malformed/incomplete JSON (attempt {attempt}/{max_attempts}): {exc}"
                ) from exc
        except _RetryableGenerationError as exc:
            last_error = exc
            will_retry = attempt < max_attempts
            if logger is not None:
                logger.warning(f"{exc}{'; retrying' if will_retry else '; no attempts left'}")
            continue

    raise StructuredGenerationError(f"Failed {label} after {max_attempts} attempts: {last_error}")
