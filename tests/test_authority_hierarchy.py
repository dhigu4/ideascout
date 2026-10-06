"""Authority hierarchy for permanent rules, inferred Taste, and source material.

Covers the screening prompt, the taste-builder prompts and their user content,
the deterministic hard-threshold check, and build-taste's refusal path. Every
database and artifact lives under pytest's tmp_path. No real LLM, network,
AgentMail, or browser is touched, and production state is never read.
"""

from __future__ import annotations

import contextlib
import io
import types
from pathlib import Path

from ideascout import authority, cli, db, shadow, taste
from tests.test_cli_build_taste import RULES_TEXT, insert_n_eligible, make_config, patch_taste
from tests.test_narrative_repair_diagnostics import _snapshot_all_tables
from tests.test_screening_narrative import build_taste_v1

CONFLICT_BODY = (
    "## Strong Positive Signals\n"
    "Brad requires 3x upside before an idea is worth attention.\n\n"
    "## Upside / Asymmetry Preferences\n"
    "A 3x hurdle is the effective hurdle for every idea.\n"
)
BENIGN_BODY = (
    "## Upside / Asymmetry Preferences\n"
    "Brad appears to prefer larger upside; several judgments found 3x-style multiples attractive.\n"
)


class RecordingClient:
    def __init__(self, text: str):
        self.calls: list[dict] = []
        self._text = text
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(
            stop_reason="end_turn",
            content=[types.SimpleNamespace(type="text", text=self._text)],
        )


# --- screening prompt --------------------------------------------------------------------


def _screening_prompt(rules: str = "RULES-MARK", taste_body: str = "TASTE-MARK") -> str:
    return shadow.SYSTEM_PROMPT_TEMPLATE.format(
        idea_taste_body=taste_body,
        screen_rules_body=rules,
        authority_block=authority.SCREENING_AUTHORITY_BLOCK,
    )


def test_screening_prompt_states_permanent_rules_outrank_taste():
    prompt = _screening_prompt()

    assert "AUTHORITATIVE" in prompt
    assert "INFERRED, SECONDARY" in prompt
    assert "If the taste profile conflicts with the permanent rules, the permanent rules win." in prompt


def test_screening_prompt_presents_permanent_rules_before_taste():
    prompt = _screening_prompt()

    assert prompt.index("RULES-MARK") < prompt.index("TASTE-MARK")


def test_screening_prompt_keeps_the_canonical_100_percent_standard():
    prompt = _screening_prompt()

    assert "greater than 100%+ upside over a multi-year period" in prompt


def test_screening_prompt_forbids_describing_3x_as_a_hurdle():
    prompt = _screening_prompt()

    assert "is NOT a hurdle, floor, or minimum" in prompt
    assert "Do not describe 3x as Brad's hurdle, effective hurdle, required hurdle, or minimum" in prompt
    assert "must not be PASSed merely because it falls short of a 3x preference" in prompt
    assert authority.find_hard_threshold_conflicts(prompt) == []


def test_screening_prompt_cites_preferences_as_suggestions_not_adoptions():
    prompt = _screening_prompt()

    assert "Do not cite an inferred preference as though Brad formally adopted it" in prompt


def test_screening_prompt_keeps_permanent_exclusions_authoritative():
    assert "Permanent exclusions in the rules apply regardless of anything the taste profile says." in _screening_prompt()


def test_screening_prompt_still_states_the_classification_contract():
    prompt = _screening_prompt()

    assert "INSUFFICIENT_INFORMATION" in prompt
    assert "never invent a numeric score" in prompt


# --- taste builder ---------------------------------------------------------------------


def test_taste_builder_system_prompt_separates_hard_rules_from_inferred_tendencies():
    system = taste.IDEA_TASTE_SYSTEM_PROMPT

    assert "AUTHORITY AND PERMANENT RULES" in system
    assert "Separate the two kinds of statement" in system
    assert "Brad-approved rules are restated only as the rules themselves" in system
    assert "Inferred tendencies are written as tendencies" in system
    assert "Taste is an inferred summary, not a rulebook." in system


def test_taste_builder_forbids_hardening_numeric_thresholds_and_rules_from_small_samples():
    system = taste.IDEA_TASTE_SYSTEM_PROMPT

    for phrase in (
        "contradict a permanent rule",
        "turn a numeric threshold from the rules into a harder one",
        "weaken a permanent exclusion",
        "invent a permanent screening criterion",
        "turn an observed tendency into a categorical rule",
        "Do not invent permanent rules from small-sample correlations",
    ):
        assert phrase in system


def test_more_upside_is_better_is_phrased_as_a_preference_not_a_minimum():
    system = taste.IDEA_TASTE_SYSTEM_PROMPT

    assert "phrase that as a preference" in system
    assert "Never phrase it as a minimum, a hurdle, or a requirement." in system
    assert authority.find_hard_threshold_conflicts(BENIGN_BODY) == []


def test_candidate_rules_prompt_forbids_duplicating_or_hardening_permanent_rules():
    system = taste.CANDIDATE_RULES_SYSTEM_PROMPT

    assert "Do not propose a candidate rule that duplicates, hardens, weakens, or contradicts a permanent rule." in system
    assert "must be reworded as a preference, or omitted" in system


def test_taste_builder_request_carries_the_permanent_rules():
    client = RecordingClient("## Strong Positive Signals\nAppears to prefer quality.\n")

    taste.generate_idea_taste_body(client, "fake-model", [], permanent_rules_text="PERMANENT-RULES-MARK")

    call = client.calls[0]
    user = call["messages"][0]["content"]
    assert user.startswith("=== PERMANENT SCREENING RULES (IDEA_SCREEN_RULES.md -- authoritative) ===\nPERMANENT-RULES-MARK")
    assert call["system"] == taste.IDEA_TASTE_SYSTEM_PROMPT


def test_candidate_rules_request_carries_the_permanent_rules():
    client = RecordingClient('{"rules": []}')

    taste.generate_candidate_rules(client, "fake-model", [], permanent_rules_text="PERMANENT-RULES-MARK")

    user = client.calls[0]["messages"][0]["content"]
    assert "PERMANENT-RULES-MARK" in user
    assert user.index("PERMANENT-RULES-MARK") < user.index("Here are 0 of Brad's")


# --- deterministic hard-threshold check --------------------------------------------------


def test_obvious_hard_threshold_conflicts_are_caught():
    for text in (
        "Brad requires 3x upside.",
        "A 3x hurdle applies to every idea.",
        "This is an effective 3x hurdle.",
        "Set a minimum 3x return.",
        "Ideas must show at least 3x.",
        "A hard 3x floor.",
    ):
        findings = authority.find_hard_threshold_conflicts(text)
        assert findings, text
        assert findings[0].line_number == 1


def test_conflict_check_reports_line_number_and_excerpt():
    findings = authority.find_hard_threshold_conflicts("Fine line.\nBrad requires 3x upside.\n")

    assert [(f.line_number, f.excerpt) for f in findings] == [(2, "Brad requires 3x upside.")]


def test_benign_preference_language_is_allowed():
    for text in (
        "Brad appears to prefer larger upside.",
        "Several judgments found ~3x-style multiples attractive.",
        "Multi-baggers (about 3x) were attractive in a few cases, but mixed evidence.",
        "He likes at least two catalysts.",
    ):
        assert authority.find_hard_threshold_conflicts(text) == [], text


def test_negated_statements_are_not_flagged():
    assert authority.find_hard_threshold_conflicts("Brad does not require 3x upside.") == []
    assert authority.find_hard_threshold_conflicts("3x is not an effective hurdle for him.") == []


def test_conflict_check_is_pure_and_deterministic():
    first = authority.find_hard_threshold_conflicts(CONFLICT_BODY)
    second = authority.find_hard_threshold_conflicts(CONFLICT_BODY)

    assert first == second
    assert len(first) == 3


# --- build-taste refusal path --------------------------------------------------------------


def _unfrozen_build(tmp_path, monkeypatch, body):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()
    patch_taste(monkeypatch, body=body)
    return config


def test_build_taste_refuses_a_hard_threshold_conflict_and_creates_no_version(tmp_path, monkeypatch, capsys):
    config = _unfrozen_build(tmp_path, monkeypatch, CONFLICT_BODY)
    before = _snapshot_all_tables(config)

    code = cli.cmd_build_taste(config)

    out = capsys.readouterr().out
    assert code == 1
    assert "build-taste REFUSED" in out
    assert "idea-taste.md, line 2" in out
    assert "the permanent rules win" in out
    assert _snapshot_all_tables(config) == before
    assert not (config.database_path.parent / "taste-versions").exists()


def test_rejected_candidate_is_preserved_for_review_and_never_activated(tmp_path, monkeypatch, capsys):
    config = _unfrozen_build(tmp_path, monkeypatch, CONFLICT_BODY)

    cli.cmd_build_taste(config)

    saved = list((config.database_path.parent / "taste-rejected").glob("*rejected-candidate.md"))
    assert len(saved) == 1
    text = saved[0].read_text(encoding="utf-8")
    assert text.startswith("REJECTED CANDIDATE -- NOT ACTIVE.")
    assert "Brad requires 3x upside" in text


def test_benign_generation_still_builds_a_version(tmp_path, monkeypatch, capsys):
    config = _unfrozen_build(tmp_path, monkeypatch, BENIGN_BODY)

    code = cli.cmd_build_taste(config)

    assert code == 0
    conn = db.connect(config.database_path)
    assert db.get_latest_taste_version(conn)["version_number"] == 1
    conn.close()


def test_conflict_refusal_makes_no_extra_llm_call(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()
    counts = {"body": 0, "rules": 0}

    def body(client, model, records, *, permanent_rules_text, logger=None):
        counts["body"] += 1
        return CONFLICT_BODY

    def rules(client, model, records, *, permanent_rules_text, logger=None):
        counts["rules"] += 1
        return []

    monkeypatch.setattr(taste, "build_client", lambda api_key: object())
    monkeypatch.setattr(taste, "generate_idea_taste_body", body)
    monkeypatch.setattr(taste, "generate_candidate_rules", rules)

    cli.cmd_build_taste(config)

    assert counts == {"body": 1, "rules": 1}


def test_build_taste_gives_the_builder_the_permanent_rules(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()
    seen = {}

    def body(client, model, records, *, permanent_rules_text, logger=None):
        seen["body"] = permanent_rules_text
        return BENIGN_BODY

    def rules(client, model, records, *, permanent_rules_text, logger=None):
        seen["rules"] = permanent_rules_text
        return []

    monkeypatch.setattr(taste, "build_client", lambda api_key: object())
    monkeypatch.setattr(taste, "generate_idea_taste_body", body)
    monkeypatch.setattr(taste, "generate_candidate_rules", rules)

    assert cli.cmd_build_taste(config) == 0
    assert seen == {"body": RULES_TEXT, "rules": RULES_TEXT}


def test_build_taste_refuses_when_permanent_rules_cannot_be_read(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()
    patch_taste(monkeypatch)
    config.screen_rules_path.unlink()

    code = cli.cmd_build_taste(config)

    assert code == 1
    assert "cannot read the permanent screening rules" in capsys.readouterr().out


# --- frozen v2 and historical evidence -------------------------------------------------------


def test_frozen_taste_is_untouched_by_a_refused_rebuild(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    latest = build_taste_v1(config, monkeypatch)
    artifact = latest["idea_taste_path"]
    artifact_bytes = Path(artifact).read_bytes()
    before = _snapshot_all_tables(config)
    patch_taste(monkeypatch, body=BENIGN_BODY)

    code = cli.cmd_build_taste(config)

    assert code == 1
    assert "is frozen" in capsys.readouterr().out
    assert Path(artifact).read_bytes() == artifact_bytes
    assert _snapshot_all_tables(config) == before
    conn = db.connect(config.database_path)
    assert conn.execute("SELECT build_unlocked FROM taste_versions WHERE version_number = 1").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM taste_versions").fetchone()[0] == 1
    conn.close()


def test_historical_screenings_are_untouched_by_a_rejected_build(tmp_path, monkeypatch, capsys):
    config = _unfrozen_build(tmp_path, monkeypatch, CONFLICT_BODY)
    before = _snapshot_all_tables(config)

    cli.cmd_build_taste(config)

    assert _snapshot_all_tables(config) == before


def test_rejected_build_never_writes_the_canonical_convenience_copies(tmp_path, monkeypatch, capsys):
    config = _unfrozen_build(tmp_path, monkeypatch, CONFLICT_BODY)

    cli.cmd_build_taste(config)

    assert not (config.database_path.parent / "idea-taste.md").exists()
    assert not (config.database_path.parent / "candidate-permanent-rules.md").exists()
