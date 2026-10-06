"""Deterministic hard-threshold validator: multiples and percentages.

A generated taste may state a numeric preference, but must not turn it into a
required minimum or hurdle. Obvious cases are caught; negated and benign
preference language is allowed. No LLM, no rewriting, and no retry is involved.
"""

from __future__ import annotations

from ideascout import authority, cli, taste
from tests.test_cli_build_taste import insert_n_eligible, make_config, patch_taste
from ideascout import db


def _caught(text: str) -> bool:
    return bool(authority.find_hard_threshold_conflicts(text))


def test_multiple_hurdles_are_rejected():
    for text in ("3x hurdle", "minimum 3x", "requires 3x upside", "an effective 3x hurdle"):
        assert _caught(text), text


def test_percentage_minimums_are_rejected():
    for text in (
        "minimum 200% upside",
        "requires 150% upside",
        "at least 200% return",
        "a hard 50% floor on upside",
        "threshold of 100% upside",
    ):
        assert _caught(text), text


def test_percentage_irr_hurdles_are_rejected():
    for text in ("25% IRR hurdle", "a 15% CAGR floor", "a 20% return threshold"):
        assert _caught(text), text


def test_negated_percentage_statements_are_allowed():
    for text in (
        "The strategy does not require 200% upside.",
        "There is no 25% IRR hurdle for this firm.",
        "Brad does not treat 150% upside as a minimum.",
    ):
        assert not _caught(text), text


def test_negated_multiple_statements_are_allowed():
    assert not _caught("Brad does not require 3x upside.")
    assert not _caught("3x is not an effective hurdle for him.")


def test_benign_preference_language_is_allowed():
    for text in (
        "200% upside is especially attractive.",
        "Higher upside is preferred.",
        "3x outcomes are attractive when available.",
        "Several judgments found ~3x-style multiples attractive.",
        "Brad appears to prefer larger upside.",
    ):
        assert not _caught(text), text


def test_conflict_is_reported_with_its_line_number():
    findings = authority.find_hard_threshold_conflicts("Preferences are noted.\nBrad requires 150% upside.\n")

    assert [f.line_number for f in findings] == [2]


def test_build_taste_refuses_a_percentage_minimum_without_retrying(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()
    counts = {"body": 0, "rules": 0}

    def body(client, model, records, *, permanent_rules_text, logger=None):
        counts["body"] += 1
        return "## Upside / Asymmetry Preferences\nBrad's minimum 200% upside is required for every idea.\n"

    def rules(client, model, records, *, permanent_rules_text, logger=None):
        counts["rules"] += 1
        return []

    monkeypatch.setattr(taste, "build_client", lambda api_key: object())
    monkeypatch.setattr(taste, "generate_idea_taste_body", body)
    monkeypatch.setattr(taste, "generate_candidate_rules", rules)

    code = cli.cmd_build_taste(config)

    out = capsys.readouterr().out
    assert code == 1
    assert "build-taste REFUSED" in out
    assert "idea-taste.md, line 2" in out
    assert counts == {"body": 1, "rules": 1}
