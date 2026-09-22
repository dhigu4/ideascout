"""Compact-idea extraction (Level 1 of Stage 4): a cheap LLM call over
ALREADY source-isolated text (see source_isolation.py) to produce a
permanent, inexpensive summary record for later screening.

CRITICAL / no-leakage: this module must never be given anything except
the isolated source_text produced by source_isolation.isolate_source(). It
never sees messages_raw.body_raw directly, never sees Brad's own
feedback/verdict/comment, never sees candidate-permanent-rules.md, and
never sees idea-taste.md -- this is factual/compact extraction only,
never screening. company/ticker are extracted from what the source text
itself says -- never copied from Brad's feedback row, even when that
would be more reliable, because doing so would make the compact idea
record partly derived from Brad's own judgment, defeating the entire
purpose of a blind shadow-screening test.

SCHEMA SHAPE (Stage 5.3 fix): a real production extraction call failed
with Anthropic's 400 "Schema is too complex". The cause was every field on
ExtractedIdea having a Python-side default (None for 4 fields, "" for the
other 10) -- pydantic's model_json_schema() then never emits a "required"
array at all (a field with a default is "optional" by JSON Schema
convention), and the 4 Optional[str] fields additionally become
`anyOf: [string, null]` unions. That combination -- zero required
properties, nullable unions, and every default re-encoded as a synthetic
description -- is what the API rejected. ExtractedIdea is now a single
flat object of exactly 12 required plain `str` fields, no defaults, no
Optional/unions, no nested models, no arrays -- source_title/source_date
were dropped entirely (see cli.py's extraction call sites, which now
carry a source's existing title/date through unchanged rather than
sourcing them from this model). The model is asked to return an empty
string, never invented content, when the source doesn't support a field.
"""

from __future__ import annotations

from pydantic import BaseModel

from . import structured_llm

MAX_OUTPUT_TOKENS = 2048
MAX_ATTEMPTS = 3

SYSTEM_PROMPT = """\
You will be given ONLY the source material for one investment idea -- a \
forwarded writeup, article excerpt, or quoted original email. Any \
commentary, verdict, or opinion from the person who originally reviewed \
this idea has ALREADY been removed before it reached you. You have not \
seen, and must not guess at or infer, what that person thinks of the \
idea. You have NOT been given, and must not use, that person's \
historical feedback/judgment history or any taste/preference profile \
derived from it. You must not evaluate, score, or screen this idea in \
any way -- that happens later, as a separate step, using different inputs.

Your job is to compactly and neutrally summarize what THIS SOURCE TEXT \
itself says about the business and the investment case -- not to render \
your own verdict, and not to speculate about facts the source doesn't \
state.

Every field in your response is a required string. Rules:
- Use ONLY the captured source material. Never invent facts, and never \
  fill a field with a guess just to have something to put there.
- company/ticker: fill in ONLY if explicitly present in the source text \
  itself; otherwise return an empty string. Never guess.
- Every other field should be a short, neutral, faithful summary of what \
  the source text says, in your own words, without adding an opinion of \
  your own about whether the idea is good or bad.
- BE GENUINELY COMPACT. Each field is a compression of the source, not a \
  retelling of it: at most 1-3 short sentences per field, even when the \
  source material is long (a source can run to tens of thousands of \
  characters; your summary of it must not). Use plain, direct sentences, \
  not lists of clauses strung together. A short, complete answer for \
  every field is always better than a long, detailed one -- do not pad a \
  field with extra detail, caveats, or restated context just because the \
  source discusses it at length.
- If the source text doesn't address a field at all, return an empty \
  string for it rather than inventing something or padding it with \
  filler text.
- known_unknowns should clearly separate what the source itself claims \
  from what is genuinely unknown -- list what a reader would need to \
  find out that this source text does not itself answer, briefly.
"""


# Deliberately flat: exactly 12 required string fields, no defaults, no
# Optional/unions, no nested models, no arrays (see module docstring for
# why). This shape is load-bearing, not just style -- a future change
# that reintroduces a default, an Optional[str]/`str | None`, a list, or
# a nested model here risks the exact "Schema is too complex" failure
# this fixed (see test_idea_extraction.py's schema-shape test). No class
# docstring on purpose: pydantic would embed it as a schema "description"
# sent to the API on every call, which is unnecessary bloat here.
class ExtractedIdea(BaseModel):
    company: str
    ticker: str
    business_summary: str
    core_thesis: str
    why_mispriced: str
    future_earnings_change: str
    upside_case: str
    downside_or_key_risks: str
    catalysts: str
    what_must_be_true: str
    evidence_of_market_misunderstanding: str
    known_unknowns: str


def build_client(api_key: str):
    import anthropic

    return anthropic.Anthropic(api_key=api_key)


def extract_idea(client, model_name: str, source_text: str, logger=None) -> ExtractedIdea:
    """source_text must already be Level-0-isolated, source-only material
    -- never the raw message body, and never anything derived from Brad's
    own feedback. Blank ("") company/ticker mean "not stated in the
    source" -- callers that persist to a nullable database column should
    normalize "" to None themselves (see cli.py); this function/model
    never invents a value just to avoid returning blank.
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
