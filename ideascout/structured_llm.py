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

# Used ONLY when the immediately preceding attempt failed specifically due
# to output-limit truncation (stop_reason == "max_tokens"), never for a
# malformed/incomplete-JSON failure -- those are a different failure mode
# that a stricter length instruction wouldn't fix. Paired with a modestly
# larger token budget for that one retry attempt (see
# TRUNCATION_RETRY_TOKEN_MULTIPLIER) -- neither change alone is as
# reliable as both together, and escalating tokens on every retry
# regardless of cause would waste budget on failures a length reminder
# alone can already fix.
TRUNCATION_RETRY_REMINDER_SUFFIX = (
    "\n\nIMPORTANT: your previous response did not fit within the output token "
    "limit and was cut off mid-output -- it was truncated, not merely long. Be "
    "SIGNIFICANTLY more concise this time: keep every field to at most one or two "
    "short sentences. Still cover everything asked for, but prioritize returning a "
    "short, COMPLETE, valid response over a longer one that risks being cut off "
    "again."
)

# A retry immediately following a truncation gets a LARGER token budget
# than the original request, but only a modest, bounded multiple of it --
# never an unconditionally huge fixed number -- so a genuinely oversized
# request doesn't just get a bigger budget forever, and ordinary
# (non-truncated) requests never pay for headroom they don't need.
TRUNCATION_RETRY_TOKEN_MULTIPLIER = 1.5


PROVIDER_ERROR = "PROVIDER_ERROR"
RESPONSE_PARSE_ERROR = "RESPONSE_PARSE_ERROR"
VALIDATION_ERROR = "VALIDATION_ERROR"
EMPTY_RESPONSE = "EMPTY_RESPONSE"
UNKNOWN_ERROR = "UNKNOWN_ERROR"


class StructuredGenerationError(RuntimeError):
    """Raised when a structured-output LLM call fails outright, or returns
    unusable output after every retry attempt has been exhausted.

    `code` is a stable failure category (PROVIDER_ERROR, RESPONSE_PARSE_ERROR,
    VALIDATION_ERROR, EMPTY_RESPONSE, UNKNOWN_ERROR). `detail` is a short,
    SANITIZED explanation built only from safe facts -- an exception class
    name, an HTTP status, a truncation flag, or pydantic field names and error
    types. It never carries provider message text, prompts, keys, or payloads.
    The message string is unchanged from before, so existing callers and logs
    behave exactly as they did.
    """

    def __init__(self, message: str, *, code: str = UNKNOWN_ERROR, detail: str | None = None):
        super().__init__(message)
        self.code = code
        self.detail = detail


class _RetryableGenerationError(RuntimeError):
    """Internal only: one failed attempt (truncated response, or one that
    failed JSON/schema validation). Never raised out of this module --
    generate_structured catches this itself and either retries or raises
    StructuredGenerationError once attempts are exhausted.
    """

    def __init__(self, message: str, *, code: str, detail: str | None = None):
        super().__init__(message)
        self.code = code
        self.detail = detail


def _provider_detail(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    return f"{name} HTTP {status}" if isinstance(status, int) else name


def _classify_validation_error(exc: pydantic.ValidationError) -> tuple[str, str]:
    """Category plus a field-level summary built only from locations and error
    types -- never the input values pydantic would otherwise quote back.
    """
    errors = exc.errors()
    if any(error.get("type") == "json_invalid" for error in errors):
        return RESPONSE_PARSE_ERROR, "malformed JSON"
    parts = []
    for error in errors[:3]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "root"
        parts.append(f"{location}: {error.get('type')}")
    return VALIDATION_ERROR, "; ".join(parts)


def _is_schema_complexity_error(exc: Exception) -> bool:
    """True for Anthropic's 400 invalid_request_error rejecting the
    structured-output schema itself (observed message: "Schema is too
    complex") -- a configuration problem with the caller's output_model,
    never something a retry of the same request could fix. Generic: not
    tied to any particular caller/schema, just this one error shape.
    """
    try:
        import anthropic
    except ImportError:
        return False
    return isinstance(exc, anthropic.BadRequestError) and "schema is too complex" in str(exc).lower()


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
    Never returns a partial/incomplete structured object -- a call that
    exhausts every attempt raises StructuredGenerationError instead.

    A retry immediately following a TRUNCATION-caused failure (never a
    malformed-JSON one) uses a stricter compactness reminder and a
    modestly larger max_tokens for that one attempt only (see
    TRUNCATION_RETRY_TOKEN_MULTIPLIER) -- the base max_output_tokens is
    otherwise used for every attempt, so ordinary requests never pay for
    headroom they don't need.
    """
    output_config = output_config_for(output_model)
    last_error: Exception | None = None
    last_code = UNKNOWN_ERROR
    last_detail: str | None = None
    previous_attempt_truncated = False

    for attempt in range(1, max_attempts + 1):
        if attempt == 1:
            content = user_content
            attempt_max_tokens = max_output_tokens
        elif previous_attempt_truncated:
            content = user_content + TRUNCATION_RETRY_REMINDER_SUFFIX
            attempt_max_tokens = int(max_output_tokens * TRUNCATION_RETRY_TOKEN_MULTIPLIER)
        else:
            content = user_content + RETRY_REMINDER_SUFFIX
            attempt_max_tokens = max_output_tokens

        try:
            response = client.messages.create(
                model=model_name,
                max_tokens=attempt_max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": content}],
                output_config=output_config,
            )
        except Exception as exc:
            if _is_schema_complexity_error(exc):
                raise StructuredGenerationError(
                    f"The structured-output schema for {label} was rejected by the API as too "
                    f"complex. This is a schema/configuration problem with the output_model passed "
                    f"in, not a retryable content issue -- simplify it (a single flat object of "
                    f"required scalar fields, no Optional/unions, no nested models, no arrays) "
                    f"rather than retrying this request: {exc}",
                    code=PROVIDER_ERROR,
                    detail="schema rejected by provider",
                ) from exc
            raise StructuredGenerationError(
                f"LLM call failed while {label}: {exc}", code=PROVIDER_ERROR, detail=_provider_detail(exc)
            ) from exc

        try:
            if response.stop_reason == "max_tokens":
                previous_attempt_truncated = True
                raise _RetryableGenerationError(
                    f"{label}: response truncated at output limit (attempt {attempt}/{max_attempts}, "
                    f"max_tokens={attempt_max_tokens})",
                    code=RESPONSE_PARSE_ERROR,
                    detail="truncated at output limit",
                )
            previous_attempt_truncated = False

            text = next((block.text for block in response.content if getattr(block, "type", None) == "text"), "")
            if not text.strip():
                raise _RetryableGenerationError(
                    f"{label}: response contained no text (attempt {attempt}/{max_attempts})",
                    code=EMPTY_RESPONSE,
                    detail="response contained no text",
                )
            try:
                return output_model.model_validate_json(text)
            except pydantic.ValidationError as exc:
                code, detail = _classify_validation_error(exc)
                raise _RetryableGenerationError(
                    f"{label}: response was malformed/incomplete JSON (attempt {attempt}/{max_attempts}): {exc}",
                    code=code,
                    detail=detail,
                ) from exc
        except _RetryableGenerationError as exc:
            last_error = exc
            last_code = exc.code
            last_detail = exc.detail
            will_retry = attempt < max_attempts
            if logger is not None:
                logger.warning(f"{exc}{'; retrying' if will_retry else '; no attempts left'}")
            continue

    raise StructuredGenerationError(
        f"Failed {label} after {max_attempts} attempts: {last_error}", code=last_code, detail=last_detail
    )
