"""Regression tests for the numeric-threshold validator's canonical-threshold
exception (see ideascout/authority.py).

Root cause of the production false positive: the keyword-then-number patterns
(e.g. "minimum ... 100%") used a 40-character gap that could skip PAST an
intervening number to reach a second, unrelated one later in the same clause
-- so a sentence that faithfully restated the canonical >100% threshold using
the word "minimum", and separately mentioned an unrelated, benign 3x
preference, got flagged twice: once for "minimum" allegedly governing the 3x
mention, and once for "minimum" allegedly inventing a new 100% floor (when
100% is literally the rules' own number).

The fix: (1) the gap between a keyword and its number may not contain another
digit, so a keyword can only ever reach the number it actually governs; (2) a
match is not flagged if its captured number is a FAITHFUL RESTATEMENT of a
threshold that already appears in permanent_rules_text, in the same unit. A
non-canonical number, or a canonical sentence that also introduces a genuinely
different hard threshold, is still rejected.

No LLM or network calls: generation is stubbed. Every database and artifact
lives under pytest's tmp_path.
"""

from __future__ import annotations

from ideascout import authority, cli, db
from tests.test_authority_hierarchy import _unfrozen_build
from tests.test_cli_build_taste import RULES_TEXT
from tests.test_narrative_repair_diagnostics import _snapshot_all_tables
from tests.test_taste_governance import _decision, _holdout, _patch_generation, _record, _unlocked

RULES = RULES_TEXT  # "1. Upside\n   Plausible path to greater than 100%+ upside.\n"

# Reconstructed from the truncated quote in the production failure report: a
# faithful backward-reference to the canonical >100% threshold via "minimum",
# plus an unrelated, benign 3x mention in the same sentence -- exercises both
# of the originally (mis-)flagged reasons at once.
PRODUCTION_SENTENCE = (
    "The permanent standard is a plausible path to >100%+ over a multi-year period. "
    "Brad appears to favor larger upside than that minimum (>100%), especially when "
    "3x-style outcomes look achievable."
)


# --- ALLOWED --------------------------------------------------------------------------------


def test_exact_production_sentence_is_allowed():
    assert authority.find_hard_threshold_conflicts(PRODUCTION_SENTENCE, permanent_rules_text=RULES) == []


def test_canonical_100_percent_restatement_is_allowed():
    for text in (
        "The permanent standard is >100% upside.",
        "The permanent rule requires >100% upside.",
    ):
        assert authority.find_hard_threshold_conflicts(text, permanent_rules_text=RULES) == [], text


def test_minimum_referring_to_the_canonical_threshold_is_allowed():
    for text in (
        "Brad prefers larger upside than that permanent minimum.",
        "Larger upside than the permanent minimum is preferable.",
        "Brad appears to favor larger upside than that minimum (>100%).",
    ):
        assert authority.find_hard_threshold_conflicts(text, permanent_rules_text=RULES) == [], text


def test_canonical_threshold_plus_benign_3x_preference_is_allowed():
    for text in (
        "The canonical rule is >100%; 3x outcomes are especially attractive but are not required.",
        "The permanent standard is >100%. 3x outcomes are attractive when available.",
    ):
        assert authority.find_hard_threshold_conflicts(text, permanent_rules_text=RULES) == [], text


def test_negated_hard_thresholds_remain_allowed_with_canonical_rules_present():
    for text in (
        "Brad does not require 3x upside.",
        "There is no 25% IRR hurdle for this firm.",
        "3x is not an effective hurdle for him.",
    ):
        assert authority.find_hard_threshold_conflicts(text, permanent_rules_text=RULES) == [], text


# --- REJECTED -------------------------------------------------------------------------------


def test_requires_3x_is_still_rejected():
    findings = authority.find_hard_threshold_conflicts("Brad requires 3x.", permanent_rules_text=RULES)
    assert [f.reason for f in findings] == ["a multiple stated as a required minimum or floor"]


def test_effective_3x_hurdle_is_still_rejected():
    findings = authority.find_hard_threshold_conflicts("An effective 3x hurdle applies.", permanent_rules_text=RULES)
    assert "an effective hurdle" in [f.reason for f in findings]


def test_minimum_200_percent_upside_is_still_rejected():
    findings = authority.find_hard_threshold_conflicts("Minimum upside is 200%.", permanent_rules_text=RULES)
    assert [f.reason for f in findings] == ["a percentage stated as a required minimum, floor, or threshold"]


def test_requires_150_percent_upside_is_still_rejected():
    assert authority.find_hard_threshold_conflicts("Requires 150% upside.", permanent_rules_text=RULES)


def test_canonical_sentence_plus_require_3x_still_fails():
    text = "The permanent rule is >100%, but in practice require 3x."
    findings = authority.find_hard_threshold_conflicts(text, permanent_rules_text=RULES)
    assert [f.reason for f in findings] == ["a multiple stated as a required minimum or floor"]


def test_canonical_sentence_plus_minimum_200_percent_still_fails():
    text = "The permanent rule is >100%, but minimum 200% is really wanted."
    findings = authority.find_hard_threshold_conflicts(text, permanent_rules_text=RULES)
    assert [f.reason for f in findings] == ["a percentage stated as a required minimum, floor, or threshold"]


def test_25_percent_irr_hurdle_rejected_when_not_in_permanent_rules():
    findings = authority.find_hard_threshold_conflicts("25% IRR is the hurdle.", permanent_rules_text=RULES)
    assert [f.reason for f in findings] == ["a percentage stated as a hurdle, floor, or threshold"]


def test_25_percent_irr_hurdle_allowed_when_it_is_in_permanent_rules():
    rules_with_irr = RULES + "2. Hurdle\n   Minimum acceptable return is a 25% IRR.\n"
    assert authority.find_hard_threshold_conflicts("25% IRR is the hurdle.", permanent_rules_text=rules_with_irr) == []


def test_without_permanent_rules_text_the_canonical_exception_is_disabled():
    # No rules text supplied (the default) keeps the strict pre-fix behavior --
    # the exception is opt-in, never a blanket weakening of the validator.
    assert authority.find_hard_threshold_conflicts("The permanent rule requires >100% upside.") != []


# --- build-taste integration: rejected candidate inactive, authorization preserved ----------


def test_canonical_restatement_still_builds_a_taste_version(tmp_path, monkeypatch):
    body = "## Upside / Asymmetry Preferences\n" + PRODUCTION_SENTENCE + "\n"
    config = _unfrozen_build(tmp_path, monkeypatch, body)

    assert cli.cmd_build_taste(config) == 0

    conn = db.connect(config.database_path)
    assert db.get_latest_taste_version(conn)["version_number"] == 1
    conn.close()


def test_mixed_canonical_and_new_threshold_still_refuses_and_creates_no_version(tmp_path, monkeypatch):
    body = "## Upside / Asymmetry Preferences\nThe permanent rule is >100%, but in practice require 3x.\n"
    config = _unfrozen_build(tmp_path, monkeypatch, body)
    before = _snapshot_all_tables(config)

    code = cli.cmd_build_taste(config)

    assert code == 1
    assert _snapshot_all_tables(config) == before
    assert not (config.database_path.parent / "taste-versions").exists()


def test_rejected_candidate_from_the_mixed_case_is_preserved_inactive(tmp_path, monkeypatch):
    body = "## Upside / Asymmetry Preferences\nThe permanent rule is >100%, but in practice require 3x.\n"
    config = _unfrozen_build(tmp_path, monkeypatch, body)

    cli.cmd_build_taste(config)

    saved = list((config.database_path.parent / "taste-rejected").glob("*rejected-candidate.md"))
    assert len(saved) == 1
    text = saved[0].read_text(encoding="utf-8")
    assert text.startswith("REJECTED CANDIDATE -- NOT ACTIVE.")
    assert "require 3x" in text


def test_build_authorization_is_not_consumed_when_validation_fails(tmp_path, monkeypatch):
    config, _ = _holdout(tmp_path, monkeypatch)
    config.screen_rules_path.write_text(RULES, encoding="utf-8")
    _record(config)
    _patch_generation(monkeypatch, body="The permanent rule is >100%, but in practice require 3x.\n")

    code = cli.cmd_build_taste(config)

    assert code == 1
    assert _decision(config)["consumed_by_version"] is None
    assert _unlocked(config, 2) == 1
    conn = db.connect(config.database_path)
    assert db.get_latest_taste_version(conn)["version_number"] == 2
    conn.close()
