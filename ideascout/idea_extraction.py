"""Compact-idea extraction (Level 1 of Stage 4): a cheap LLM call over
ALREADY source-isolated text (see source_isolation.py) to produce a
permanent, inexpensive summary record for later screening.

CRITICAL / no-leakage: this module must never be given anything except
the isolated source_text produced by source_isolation.isolate_source(). It
never sees messages_raw.body_raw directly, never sees Brad's own
feedback/verdict/comment, and never sees candidate-permanent-rules.md.
company/ticker/source_title/source_date are extracted from what the
source text itself says -- never copied from Brad's feedback row, even
when that would be more reliable, because doing so would make the compact
idea record partly derived from Brad's own judgment, defeating the entire
purpose of a blind shadow-screening test.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from . import structured_llm

MAX_OUTPUT_TOKENS = 2048
MAX_ATTEMPTS = 3

SYSTEM_PROMPT = """\
You will be given ONLY the source material for one investment idea -- a \
forwarded writeup, article excerpt, or quoted original email. Any \
commentary, verdict, or opinion from the person who originally reviewed \
this idea has ALREADY been removed before it reached you. You have not \
seen, and must not guess at, what that person thinks of the idea.

Your job is to compactly and neutrally summarize what THIS SOURCE TEXT \
itself says about the business and the investment case -- not to render \
your own verdict, and not to speculate about facts the source doesn't \
state.

Rules:
- company/ticker/source_title/source_date: fill in ONLY if explicitly \
  present in the source text itself; otherwise leave null. Never guess.
- Every other field should be a short, neutral, faithful summary of what \
  the source text says, in your own words, without adding an opinion of \
  your own about whether the idea is good or bad.
- If the source text doesn't address a field at all, say so briefly \
  (e.g. "Not discussed in the source.") rather than leaving it blank or \
  inventing something.
- known_unknowns should list what a reader would need to find out that \
  this source text does not itself answer.
"""


class ExtractedIdea(BaseModel):
    company: Optional[str] = None
    ticker: Optional[str] = None
    source_title: Optional[str] = None
    source_date: Optional[str] = None
    business_summary: str = ""
    core_thesis: str = ""
    why_mispriced: str = ""
    future_earnings_change: str = ""
    upside_case: str = ""
    downside_or_key_risks: str = ""
    catalysts: str = ""
    what_must_be_true: str = ""
    evidence_of_market_misunderstanding: str = ""
    known_unknowns: str = ""


def build_client(api_key: str):
    import anthropic

    return anthropic.Anthropic(api_key=api_key)


def extract_idea(client, model_name: str, source_text: str, logger=None) -> ExtractedIdea:
    """source_text must already be Level-0-isolated, source-only material
    -- never the raw message body, and never anything derived from Brad's
    own feedback.
    """
    user_content = f"Source material:\n\n{source_text}"
    return structured_llm.generate_structured(
        client,
        model_name=model_name,
        system_prompt=SYSTEM_PROMPT,
        user_content=user_content,
        output_model=ExtractedIdea,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        max_attempts=MAX_ATTEMPTS,
        label="compact idea extraction",
        logger=logger,
    )
