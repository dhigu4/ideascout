"""Thin adapter around the Anthropic API for turning one raw email into
zero, one, or several structured feedback events.

This is the only file that imports the `anthropic` SDK or knows a model
name. Everything downstream (db.py, cli.py) only ever sees a list of
ParserResult objects or a ParserError, so the LLM provider/model can be
swapped later without touching the database or CLI.

IMPORTANT: The raw email in messages_raw is always authoritative. Nothing
in this file's output should ever be treated as more reliable than what
Brad actually wrote -- extracted tags are hints for later stages, not
conclusions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

PARSER_VERSION = "feedback-v1"

EVENT_TYPES = ("FEEDBACK", "NEW_IDEA", "MISSED_IDEA", "UNCLEAR")
VERDICTS = ("STRONG_LIKE", "LIKE", "MAYBE", "PASS", "STRONG_PASS")
NOVELTIES = ("NEW", "KNOWN", "UNKNOWN")

# Below this confidence -- or whenever an event's event_type is UNCLEAR, or
# its fields are internally inconsistent -- that event routes the whole
# message to NEEDS_REVIEW instead of PARSED so a human looks at it.
NEEDS_REVIEW_CONFIDENCE_THRESHOLD = 0.5

MAX_OUTPUT_TOKENS = 2048

SYSTEM_PROMPT = """\
You are extracting structured feedback from a single email. The email was \
sent by Brad, an investor, to his own idea-tracking inbox. You will be \
given the email's subject and full body exactly as received.

Your ONLY job is to capture what Brad himself explicitly wrote, in his own \
words. The body may also contain quoted prior emails, forwarded newsletter \
or article content, email signatures, and other boilerplate. None of that \
is Brad's opinion -- ignore it completely when judging sentiment, and \
never treat quoted or forwarded material as something Brad said.

Brad's own feedback vocabulary is exactly one of: STRONG LIKE, LIKE, MAYBE, \
PASS, STRONG PASS. He may instead be flagging a NEW IDEA (something new he \
wants tracked) or a MISSED IDEA (something he wishes had been surfaced \
earlier).

A single email may contain MORE THAN ONE piece of feedback -- for example, \
Brad replying to a digest of several ideas with a separate verdict on each \
one. Return one event per distinct piece of feedback, in the order they \
appear in his email. If Brad's email contains no feedback, new idea, or \
missed idea at all (e.g. it is purely informational, or just an \
acknowledgement), return an empty list of events -- do not invent one to \
fill the list.

Rules, applied to EACH event:
- event_type is "FEEDBACK" when Brad gives a verdict on an idea, "NEW_IDEA" \
  when he's flagging something new, "MISSED_IDEA" when he's flagging \
  something that should have been surfaced earlier, or "UNCLEAR" when you \
  cannot confidently tell which of these applies.
- If event_type is "FEEDBACK", verdict must be one of STRONG_LIKE, LIKE, \
  MAYBE, PASS, STRONG_PASS -- whichever matches what Brad actually wrote. \
  Do not soften or strengthen his wording (e.g. do not turn LIKE into \
  STRONG_LIKE just because his comment sounds enthusiastic).
- Only extract positive_reasons, concerns, and not_a_concern items that \
  Brad actually wrote himself. Never invent, infer, or assume a reason he \
  did not state.
- Never draw an investment conclusion beyond what Brad literally wrote.
- ticker and company should only be filled in if explicitly present in \
  Brad's own text (not merely present in quoted or forwarded material) -- \
  otherwise leave them null.
- novelty is optional. Only set it to NEW or KNOWN if Brad's own text says \
  so; otherwise use UNKNOWN.
- If you cannot confidently tell which part of the email is Brad's own \
  instruction versus quoted or forwarded material, or you cannot \
  confidently determine what Brad intended, emit exactly ONE event with \
  event_type "UNCLEAR", leave verdict null, and give a low confidence \
  score. Do not force an ambiguous message into one of the other \
  categories, and do not also emit a confident event alongside it.
- confidence is your own honest 0.0-1.0 estimate of how sure you are of \
  that specific event.
- user_comment should be a short, faithful summary of Brad's own comment \
  for that event, in his own words as much as possible. Do not summarize \
  quoted or forwarded content as if it were his comment.
"""


class FeedbackEvent(BaseModel):
    event_type: Literal["FEEDBACK", "NEW_IDEA", "MISSED_IDEA", "UNCLEAR"]
    verdict: Optional[Literal["STRONG_LIKE", "LIKE", "MAYBE", "PASS", "STRONG_PASS"]] = None
    ticker: Optional[str] = None
    company: Optional[str] = None
    novelty: Optional[Literal["NEW", "KNOWN", "UNKNOWN"]] = None
    user_comment: str = ""
    positive_reasons: List[str] = Field(default_factory=list)
    concerns: List[str] = Field(default_factory=list)
    not_a_concern: List[str] = Field(default_factory=list)
    confidence: float


class FeedbackExtractionBatch(BaseModel):
    """Top-level structured-output schema: a single email can produce zero,
    one, or several feedback events.
    """

    events: List[FeedbackEvent] = Field(default_factory=list)


class ParserError(RuntimeError):
    """Raised when the LLM call fails for a non-transient reason, or its
    output cannot be trusted at all.

    Callers must treat this as a permanent ERROR outcome (not
    NEEDS_REVIEW, not retried automatically): it means we don't have a
    usable structured result, as opposed to the parser successfully
    returning a low-confidence or UNCLEAR event, and as opposed to a
    RetryableParserError (below) which is worth trying again later.
    """


class RetryableParserError(ParserError):
    """A transient failure -- timeout, connection error, HTTP 429, or HTTP
    5xx. Callers should mark the message RETRYABLE_ERROR so it is
    automatically retried on the next parse-mail run, instead of ERROR.
    """


@dataclass(frozen=True)
class ParserResult:
    event_type: str
    verdict: str | None
    ticker: str | None
    company: str | None
    novelty: str | None
    user_comment: str
    confidence: float
    parsed_json: str  # this single event's complete raw structured result
    needs_review: bool
    review_reason: str | None


def determine_review_reason(event_type: str, verdict: str | None, confidence: float) -> str | None:
    """Decide whether one event should be flagged for human review.

    Shared by the normal parse path and by crash-recovery (re-deriving a
    message's status from feedback rows that already exist), so the two
    paths can never disagree about what counts as reviewable.
    """
    if event_type == "UNCLEAR":
        return "parser returned UNCLEAR"
    if confidence < NEEDS_REVIEW_CONFIDENCE_THRESHOLD:
        return f"confidence {confidence:.2f} below threshold {NEEDS_REVIEW_CONFIDENCE_THRESHOLD}"
    if event_type == "FEEDBACK" and verdict is None:
        return "event_type is FEEDBACK but verdict is missing"
    return None


def build_client(api_key: str):
    import anthropic

    return anthropic.Anthropic(api_key=api_key)


def _is_transient_exception(exc: BaseException) -> bool:
    """True for failures worth automatically retrying later: timeouts,
    connection errors, rate limiting (429), and server errors (5xx).
    False for everything else -- bad credentials, bad requests, and any
    error this function doesn't recognize -- since retrying those without
    a human fixing something first would just waste API calls forever.
    """
    try:
        import anthropic
    except ImportError:
        return False

    if isinstance(exc, (anthropic.APITimeoutError, anthropic.APIConnectionError, anthropic.RateLimitError)):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code >= 500
    return False


def parse_message(client, model_name: str, subject: str | None, body: str | None) -> list[ParserResult]:
    """Call the LLM once and turn its structured output into a list of
    ParserResult, one per feedback event found (may be empty).

    Raises RetryableParserError for a transient failure (safe to retry
    later) or ParserError for anything else that means the call/output
    cannot be trusted. Never raises for a low-confidence or UNCLEAR event
    -- that is a successful parse whose content says "I'm not sure",
    reported back via needs_review/review_reason on that event instead.
    """
    user_content = f"Subject: {subject or '(no subject)'}\n\n{body or '(empty body)'}"

    try:
        response = client.messages.parse(
            model=model_name,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
            output_format=FeedbackExtractionBatch,
        )
    except Exception as exc:
        if _is_transient_exception(exc):
            raise RetryableParserError(f"Transient LLM failure: {exc}") from exc
        raise ParserError(f"LLM call failed: {exc}") from exc

    batch = getattr(response, "parsed_output", None)
    if batch is None:
        raise ParserError("LLM response did not include valid structured output")

    results = []
    for event in batch.events:
        review_reason = determine_review_reason(event.event_type, event.verdict, event.confidence)
        results.append(
            ParserResult(
                event_type=event.event_type,
                verdict=event.verdict,
                ticker=event.ticker,
                company=event.company,
                novelty=event.novelty,
                user_comment=event.user_comment,
                confidence=event.confidence,
                parsed_json=event.model_dump_json(),
                needs_review=review_reason is not None,
                review_reason=review_reason,
            )
        )
    return results
