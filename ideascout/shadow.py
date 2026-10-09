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

from typing import Annotated, List, Literal

from pydantic import BaseModel, Field, StringConstraints, create_model

from . import authority, structured_llm

MAX_OUTPUT_TOKENS = 2048
MAX_ATTEMPTS = 3

LEGACY_SYSTEM_PROMPT_TEMPLATE = """\
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

SYSTEM_PROMPT_TEMPLATE = """\
You are testing whether a documented investment taste profile and a fixed \
set of screening rules can predict a specific investor's ("Brad's") \
reaction to a new idea, before he has reviewed it himself. You will be \
given his firm's permanent screening rules, his taste profile, and a \
compact, neutral summary of one new idea. You have NOT been given, and \
must not guess at, Brad's actual verdict on this specific idea -- this is \
a blind prediction.

=== FAR VIEW'S PERMANENT SCREENING RULES (IDEA_SCREEN_RULES.md) -- AUTHORITATIVE ===
{screen_rules_body}

=== BRAD'S TASTE PROFILE (idea-taste.md) -- INFERRED, SECONDARY ===
{idea_taste_body}

{authority_block}
CLASSIFICATION GUIDANCE for overall_prediction (apply after the permanent \
rules and the taste profile above):

INVESTIGATE_NOW -- the strongest current candidates: credible mispricing, a \
plausible path to the permanent rules' upside standard, and enough evidence \
already in hand to justify starting work now.

WATCH -- a potentially attractive Far View pattern is present, but one or \
more important things remain unproven or unresolved: uncertain upside, an \
uncertain mispricing mechanism, incomplete information, timing uncertainty, \
or an attractive structural pattern not yet sufficiently evidenced. WATCH \
does NOT require every INVESTIGATE_NOW criterion to already be proven -- \
additional diligence could plausibly change the conclusion. Uncertainty is \
NOT automatically PASS: if a potentially attractive setup exists and \
resolving a small number of questions could change the conclusion, prefer \
WATCH over PASS.

PASS -- further reasonable diligence is unlikely to make the idea \
compelling: it is structurally outside Far View's interests, has clearly \
insufficient economics or upside under the permanent rules, shows no \
plausible edge or mispricing, or its thesis fundamentally depends on \
something Brad does not want to underwrite. WATCH is not a dumping ground \
for weak ideas -- if the idea lacks a credible path to becoming compelling, \
PASS it.

Missing proof alone must never automatically imply PASS -- ask whether the \
missing piece is resolvable and whether resolving it could plausibly change \
the answer. This never changes the permanent rules' upside standard (still a \
plausible path to greater than 100%+ upside over a multi-year period) and \
never reintroduces a 3x or any other multiple as a hurdle -- see the \
authority guidance above.

Apply the permanent screening rules first, then the taste profile as a \
secondary guide, to the idea you are given below. Rate each dimension using \
ONLY the exact categorical labels specified in the schema -- never invent a \
numeric score, and never claim more precision than the material supports. If \
the source material doesn't give you enough to judge a dimension, say Unknown \
(or, for the overall prediction, INSUFFICIENT_INFORMATION) rather than guessing.
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


REPAIR_MAX_ITEMS_PER_FIELD = 2
REPAIR_MAX_ITEM_CHARS = 300
# Worst case compliant payload is 3 fields x 2 items x 300 chars, roughly
# 450 tokens of JSON. The budget is a ceiling only (output tokens are billed
# as generated), so it sits well above that rather than at it.
REPAIR_MAX_OUTPUT_TOKENS = 1024

_RepairItem = Annotated[str, StringConstraints(max_length=REPAIR_MAX_ITEM_CHARS)]


class NarrativeRepair(BaseModel):
    """The ONLY thing the narrative repair call may return. Deliberately has
    no classification, confidence, or dimension fields at all, so a repair
    response cannot change them even if the model tried to -- anything else
    it returns is simply ignored (pydantic drops unknown fields).

    Length limits are enforced here, not by the provider: the structured-output
    transform only passes maxItems/maxLength to the model as description text.
    An over-long response fails validation and is recorded as VALIDATION_ERROR.
    """

    key_reasons: List[_RepairItem] = Field(
        default_factory=list,
        max_length=REPAIR_MAX_ITEMS_PER_FIELD,
        description="1 or 2 concise reasons this idea may deserve attention.",
    )
    key_concerns: List[_RepairItem] = Field(
        default_factory=list,
        max_length=REPAIR_MAX_ITEMS_PER_FIELD,
        description="1 or 2 concise, specific risks or weaknesses.",
    )
    critical_questions: List[_RepairItem] = Field(
        default_factory=list,
        max_length=REPAIR_MAX_ITEMS_PER_FIELD,
        description="1 or 2 concise, specific questions that must be answered.",
    )


def _repair_output_model(missing_fields: list[str]) -> type[BaseModel]:
    """A schema containing ONLY the requested fields, so the model is never
    asked to generate narrative that will be discarded anyway.
    """
    return create_model(
        "NarrativeRepairRequested",
        **{name: (NarrativeRepair.model_fields[name].annotation, NarrativeRepair.model_fields[name]) for name in missing_fields},
    )


_REPAIR_INSTRUCTIONS = """\
A previous screening of this same idea was classified {overall_prediction} \
(confidence {confidence}), but its narrative was missing. Your ONLY task now \
is to supply the missing narrative fields listed below, grounded in the \
idea summary and the taste profile and rules above. Do NOT revisit or \
change the classification, confidence, or any dimension -- those are already \
decided and are shown for context only.

Strict output limits, per missing field:
- Return 1 or 2 items. Never more.
- Each item is one short, specific sentence of at most 250 characters.
- Do not restate the classification, confidence, dimension ratings, company \
description, the full thesis, or the screening rationale. Write only the items.
- Never invent facts. If the material truly does not support a field, return \
an empty list for it.

Missing fields to fill: {missing_fields}
"""


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


def repair_narrative(
    client,
    model_name: str,
    *,
    idea_taste_body: str,
    screen_rules_body: str,
    idea_record,
    prediction,
    missing_fields: list[str],
    logger=None,
) -> NarrativeRepair:
    """Exactly ONE narrow call (max_attempts=1 -- no internal retry either)
    to fill only the narrative fields a WATCH/INVESTIGATE_NOW screening is
    missing. Same taste profile, same permanent rules, same compact idea
    record; the already-returned classification is passed as read-only
    context. Callers merge only the requested, still-missing fields (see
    screening_narrative.merge_narrative) -- nothing else is ever read from
    the response.
    """
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        idea_taste_body=idea_taste_body,
        screen_rules_body=screen_rules_body,
        authority_block=authority.SCREENING_AUTHORITY_BLOCK,
    )
    context = (
        f"Existing screening (read-only, do not change): overall={prediction.overall_prediction}, "
        f"confidence={prediction.confidence}, mispricing={prediction.mispricing}, "
        f"variant_perception={prediction.variant_perception}, upside={prediction.upside}, "
        f"business_quality={prediction.business_quality}, downside={prediction.downside}."
    )
    instructions = _REPAIR_INSTRUCTIONS.format(
        overall_prediction=prediction.overall_prediction,
        confidence=prediction.confidence,
        missing_fields=", ".join(missing_fields),
    )
    user_content = "New idea to screen:\n\n" + format_idea_record_for_screening(idea_record) + "\n\n" + context + "\n\n" + instructions
    requested = structured_llm.generate_structured(
        client,
        model_name=model_name,
        system_prompt=system_prompt,
        user_content=user_content,
        output_model=_repair_output_model(missing_fields),
        max_output_tokens=REPAIR_MAX_OUTPUT_TOKENS,
        max_attempts=1,
        label="screening narrative repair",
        logger=logger,
    )
    return NarrativeRepair(**requested.model_dump())


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
        idea_taste_body=idea_taste_body,
        screen_rules_body=screen_rules_body,
        authority_block=authority.SCREENING_AUTHORITY_BLOCK,
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
