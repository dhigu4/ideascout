"""Blind shadow screening (Level 2 of Stage 4): predicts how Brad would
likely react to a new idea, using ONLY:

  A. the frozen, authoritative Taste v1 artifact (idea-taste.md),
  B. Far View's canonical, permanent IDEA_SCREEN_RULES.md, and
  C. one compact, source-only idea record (see idea_extraction.py).

It is never given Brad's actual verdict/comments on that idea, never
candidate-permanent-rules.md (proposals only, never adopted automatically
-- see taste.py), and never the raw source text (only its already-compact
extraction, to keep this call cheap). This is what makes the resulting
prediction usable as a genuine out-of-sample test: nothing about Brad's
real reaction, or about unreviewed candidate rules, can leak into it.

SHADOW MODE ONLY: nothing in this module ever shows a prediction to Brad
-- that is cli.py's job (show-shadow-results), and it only ever reveals a
prediction once Brad's own eligible feedback for that idea already exists.
"""

from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, Field

from . import structured_llm

MAX_OUTPUT_TOKENS = 2048
MAX_ATTEMPTS = 3

SYSTEM_PROMPT_TEMPLATE = """\
You are testing whether a documented investment taste profile and a fixed \
set of screening rules can predict a specific investor's ("Brad's") \
reaction to a new idea, before he has reviewed it himself. You will be \
given his taste profile, his firm's permanent screening rules, and a \
compact, neutral summary of one new idea. You have NOT been given, and \
must not guess at, Brad's actual verdict on this specific idea -- this is \
a blind prediction.

=== BRAD'S TASTE PROFILE (idea-taste.md) ===
{idea_taste_body}

=== FAR VIEW'S PERMANENT SCREENING RULES (IDEA_SCREEN_RULES.md) ===
{screen_rules_body}

Apply the taste profile and the screening rules above to the idea you are \
given below. Rate each dimension using ONLY the exact categorical labels \
specified in the schema -- never invent a numeric score, and never claim \
more precision than the material supports. If the source material doesn't \
give you enough to judge a dimension, say Unknown (or, for the overall \
prediction, INSUFFICIENT_INFORMATION) rather than guessing.
"""

_IDEA_RECORD_FIELDS = (
    ("Company", "company"),
    ("Ticker", "ticker"),
    ("Source title", "source_title"),
    ("Source date", "source_date"),
    ("Business summary", "business_summary"),
    ("Core thesis", "core_thesis"),
    ("Why mispriced", "why_mispriced"),
    ("Future earnings change", "future_earnings_change"),
    ("Upside case", "upside_case"),
    ("Downside / key risks", "downside_or_key_risks"),
    ("Catalysts", "catalysts"),
    ("What must be true", "what_must_be_true"),
    ("Evidence of market misunderstanding", "evidence_of_market_misunderstanding"),
    ("Known unknowns", "known_unknowns"),
)


class ShadowPrediction(BaseModel):
    overall_prediction: Literal["INVESTIGATE_NOW", "WATCH", "PASS", "INSUFFICIENT_INFORMATION"]
    mispricing: Literal["Strong", "Plausible", "Weak", "Unknown"]
    variant_perception: Literal["Strong", "Plausible", "Weak", "Unknown"]
    upside: Literal["Potentially sufficient", "Probably insufficient", "Unknown"]
    business_quality: Literal["Strong", "Plausible", "Weak", "Unknown"]
    downside: Literal["Attractive", "Acceptable", "Problematic", "Unknown"]
    key_reasons: List[str] = Field(default_factory=list)
    key_concerns: List[str] = Field(default_factory=list)
    critical_questions: List[str] = Field(default_factory=list)
    confidence: Literal["HIGH", "MEDIUM", "LOW"]


def build_client(api_key: str):
    import anthropic

    return anthropic.Anthropic(api_key=api_key)


def format_idea_record_for_screening(idea_record) -> str:
    """idea_record is a sqlite3.Row (or any mapping) from idea_records --
    ONLY the compact Level-1 fields, never source_text, never anything
    from feedback.
    """
    lines = [f"{label}: {idea_record[column] or '(not stated)'}" for label, column in _IDEA_RECORD_FIELDS]
    return "\n".join(lines)


def screen_idea(
    client,
    model_name: str,
    *,
    idea_taste_body: str,
    screen_rules_body: str,
    idea_record,
    logger=None,
) -> ShadowPrediction:
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        idea_taste_body=idea_taste_body, screen_rules_body=screen_rules_body
    )
    user_content = "New idea to screen:\n\n" + format_idea_record_for_screening(idea_record)
    return structured_llm.generate_structured(
        client,
        model_name=model_name,
        system_prompt=system_prompt,
        user_content=user_content,
        output_model=ShadowPrediction,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        max_attempts=MAX_ATTEMPTS,
        label="shadow screening",
        logger=logger,
    )
