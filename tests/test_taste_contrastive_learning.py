"""Tests for the label-distribution diagnostic, contrastive Taste generation,
WATCH/PASS classification semantics, and the false-negative Brad-reason
diagnostic.

No real LLM or network calls: a recording fake client captures exactly what
the taste builder sends. Every database lives under pytest's tmp_path.
"""

from __future__ import annotations

import contextlib
import io
import json
import types

from ideascout import authority, cli, db, shadow, taste, taste_evaluation as te
from tests.test_cli_build_taste import RULES_TEXT, make_config, make_parsed_message, patch_taste
from tests.test_narrative_repair_diagnostics import _snapshot_all_tables
from tests.test_taste_evaluation import _evaluate, _member, _setup_v2, _standard_holdout


def _squeeze(text: str) -> str:
    return " ".join(text.split())


class RecordingClient:
    def __init__(self, text: str):
        self.calls: list[dict] = []
        self._text = text
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(
            stop_reason="end_turn", content=[types.SimpleNamespace(type="text", text=self._text)]
        )


def _insert_imbalanced_feedback(conn, *, extra_pass=0):
    """13 PASS-family, 2 LIKE, 5 MAYBE -- the same shape as the verified Taste
    v3 holdout (13 PASS/STRONG PASS, 2 LIKE, 5 MAYBE), used here as a realistic
    imbalanced training/eligibility fixture. Returns the feedback_ids of the
    two minority LIKE rows.
    """
    for i in range(11):
        make_parsed_message(
            conn, f"pass_{i}", verdict="PASS", ticker=f"P{i}", company=f"Pass Co {i}",
            user_comment=f"No edge, pass {i}.", extra_json={"concerns": ["No credible mispricing."]},
        )
    for i in range(2):
        make_parsed_message(
            conn, f"strongpass_{i}", verdict="STRONG_PASS", ticker=f"SP{i}", company=f"Strong Pass Co {i}",
            user_comment="Hard pass.", extra_json={"concerns": ["Structurally uninteresting."]},
        )
    like_ids = [
        make_parsed_message(
            conn, "like_0", verdict="LIKE", ticker="LIKE1", company="Minority Like Co",
            user_comment="Rare hidden-asset situation I want to dig into.",
            extra_json={"positive_reasons": [
                "Hidden real-estate value not reflected in the multiple.", "Insider buying.",
            ]},
        ),
        make_parsed_message(
            conn, "like_1", verdict="LIKE", ticker="LIKE2", company="Minority Like Co 2",
            user_comment="Second minority LIKE, different reasoning.",
            extra_json={"positive_reasons": ["Spin-off creates a cleaner pure-play."]},
        ),
    ]
    for i in range(5):
        make_parsed_message(
            conn, f"maybe_{i}", verdict="MAYBE", ticker=f"M{i}", company=f"Maybe Co {i}",
            user_comment=f"Interesting but unresolved question {i}.",
            extra_json={"positive_reasons": [f"Attractive pattern {i}."], "concerns": [f"Unresolved question {i}."]},
        )
    for i in range(extra_pass):
        make_parsed_message(conn, f"extra_pass_{i}", verdict="PASS", ticker=f"EP{i}", company=f"Extra Pass {i}")
    return like_ids


# --- 1. label distribution diagnostic ----------------------------------------------------


def test_label_distribution_counts_and_shares_are_correct():
    rows = [{"verdict": v} for v in (["LIKE"] * 2 + ["MAYBE"] * 5 + ["PASS"] * 11 + ["STRONG_PASS"] * 2)]

    dist = taste.label_distribution(rows)

    assert dist.counts == {"STRONG LIKE": 0, "LIKE": 2, "MAYBE": 5, "PASS": 11, "STRONG PASS": 2}
    assert dist.other == 0
    assert dist.total == 20
    assert dist.worth_watching == 7
    assert dist.pass_family == 13
    assert dist.positive_share == 7 / 20


def test_label_distribution_counts_rows_with_no_recognized_verdict_separately():
    rows = [{"verdict": "LIKE"}, {"verdict": None}, {"verdict": "UNCLEAR"}, {"verdict": ""}]

    dist = taste.label_distribution(rows)

    assert dist.counts["LIKE"] == 1
    assert dist.other == 3
    assert dist.total == 4


def test_label_distribution_positive_share_is_none_when_nothing_is_scored():
    dist = taste.label_distribution([{"verdict": None}, {"verdict": "UNCLEAR"}])

    assert dist.positive_share is None


def test_taste_status_reports_both_distributions_using_the_canonical_eligibility_helper(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    _insert_imbalanced_feedback(conn)
    conn.close()
    patch_taste(monkeypatch)
    assert cli.cmd_build_taste(config) == 0
    capsys.readouterr()

    conn = db.connect(config.database_path)
    make_parsed_message(conn, "after_build_pass", verdict="PASS", ticker="LATE1", company="Late Pass Co")
    canonical_eligible = len(db.get_feedback_eligible_for_learning(conn))
    conn.close()

    assert cli.cmd_taste_status(config) == 0
    out = capsys.readouterr().out

    assert "Label distribution -- current Taste training set" in out
    assert "Label distribution -- all currently learning-eligible feedback" in out
    assert "LIKE: 2 | MAYBE: 5 | PASS: 11 | STRONG PASS: 2" in out
    assert "LIKE: 2 | MAYBE: 5 | PASS: 12 | STRONG PASS: 2" in out
    assert f"({canonical_eligible} judgment(s))" in out
    assert "Worth-at-least-watching (STRONG LIKE+LIKE+MAYBE): 7" in out
    assert "PASS-family (PASS+STRONG PASS): 13" in out
    assert "Positive share: 35.0%" in out


# --- 2 & 4. contrastive generation; Brad's reasons used ---------------------------------


def test_taste_builder_prompt_forbids_defaulting_to_the_majority_label():
    system = _squeeze(taste.IDEA_TASTE_SYSTEM_PROMPT)

    assert "Do not let label frequency set your default answer." in system
    assert "does NOT mean" in system and "default outcome" in system
    assert "Learn contrastively" in system
    assert "the same analytical weight as a PASS judgment" in system
    assert "Do not rebalance, reweight, resample, or omit any judgment because of" in system
    assert "do not manufacture a positive preference" in system


def test_taste_builder_receives_every_eligible_judgment_including_minority_positives(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    _insert_imbalanced_feedback(conn)
    eligible = db.get_feedback_eligible_for_learning(conn)
    conn.close()
    assert len(eligible) == 20

    records = taste.training_records_from_rows(eligible)
    assert len(records) == 20

    client = RecordingClient("## Strong Positive Signals\nx\n")
    taste.generate_idea_taste_body(client, "fake-model", records, permanent_rules_text=RULES_TEXT)

    assert client.calls[0]["system"] == taste.IDEA_TASTE_SYSTEM_PROMPT
    user_content = client.calls[0]["messages"][0]["content"]
    assert user_content.count("### Judgment") == 20
    # the two minority LIKE judgments' distinguishing reasons survive verbatim,
    # unseparated from and un-diluted by the 13 PASS-family judgments
    assert "Hidden real-estate value not reflected in the multiple." in user_content
    assert "Spin-off creates a cleaner pure-play." in user_content
    assert "Rare hidden-asset situation I want to dig into." in user_content
    # minority MAYBE reasoning is present too
    assert "Attractive pattern 3." in user_content


def test_candidate_rules_builder_also_receives_every_eligible_judgment(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    _insert_imbalanced_feedback(conn)
    eligible = db.get_feedback_eligible_for_learning(conn)
    conn.close()
    records = taste.training_records_from_rows(eligible)

    client = RecordingClient('{"rules": []}')
    taste.generate_candidate_rules(client, "fake-model", records, permanent_rules_text=RULES_TEXT)

    user_content = client.calls[0]["messages"][0]["content"]
    assert user_content.count("### Judgment") == 20
    assert "Hidden real-estate value not reflected in the multiple." in user_content


# --- 3. WATCH vs PASS screening semantics ------------------------------------------------


def _screening_prompt():
    return _squeeze(shadow.SYSTEM_PROMPT_TEMPLATE.format(
        idea_taste_body="TASTE", screen_rules_body="RULES", authority_block=authority.SCREENING_AUTHORITY_BLOCK
    ))


def test_watch_semantics_allow_unresolved_but_promising_ideas():
    prompt = _screening_prompt()

    assert "WATCH does NOT require every INVESTIGATE_NOW criterion to already be proven" in prompt
    assert "Uncertainty is NOT automatically PASS" in prompt
    assert "prefer WATCH over PASS" in prompt
    assert "uncertain upside" in prompt and "timing uncertainty" in prompt


def test_missing_information_alone_does_not_force_pass():
    assert "Missing proof alone must never automatically imply PASS" in _screening_prompt()


def test_structurally_weak_ideas_can_still_be_pass():
    prompt = _screening_prompt()

    assert "WATCH is not a dumping ground for weak ideas" in prompt
    assert "PASS it" in prompt
    assert "further reasonable diligence is unlikely to make the idea" in prompt


def test_permanent_upside_rule_text_is_unchanged():
    prompt = _screening_prompt()

    assert prompt.count("greater than 100%+ upside over a multi-year period") >= 1


def test_no_3x_hurdle_language_in_either_prompt():
    prompt = _screening_prompt()

    assert authority.find_hard_threshold_conflicts(prompt) == []
    assert authority.find_hard_threshold_conflicts(_squeeze(taste.IDEA_TASTE_SYSTEM_PROMPT)) == []
    assert "never reintroduces a 3x or any other multiple as a hurdle" in prompt


# --- 5. false-negative evaluation diagnostic: Brad's stored reason ----------------------


def _fake_screening(**overrides):
    base = dict(
        mispricing="Plausible", variant_perception="Weak", upside="Potentially sufficient",
        business_quality="Plausible", downside="Acceptable", confidence="MEDIUM",
        key_reasons_json="[]", key_concerns_json="[]", critical_questions_json="[]",
    )
    base.update(overrides)
    return base


def test_false_negative_report_includes_brads_stored_reason_next_to_model_rationale():
    row = te.EvalRow(
        ref="BR-1", feedback_id=1, company="Minority Like Co", ticker="LIKE1", source="yellowbrick/1",
        sr_ref="SR-1", prediction="PASS", label="LIKE", actual="INVESTIGATE_NOW",
        screening=_fake_screening(key_concerns_json=json.dumps(["No clear mispricing identified."])),
        judged_at="2026-06-01T00:00:00+00:00", prompt_version=2,
        brad_comment="Rare hidden-asset situation I want to dig into.",
    )
    gate = te.GateResult(checks=[("placeholder check", True, "ok")], audit=None, rows=[row])

    out = "\n".join(te.render_report(
        version_label="v3", checkpoint_feedback_id=60, rules_sha256="deadbeef", gate=gate, rules_text=RULES_TEXT,
    ))

    fn_section = out.split("FALSE NEGATIVES:", 1)[1]
    assert "Brad's stated reason: Rare hidden-asset situation I want to dig into." in fn_section
    assert "No clear mispricing identified." in fn_section


def test_false_negative_report_shows_placeholder_when_brad_left_no_comment():
    row = te.EvalRow(
        ref="BR-2", feedback_id=2, company="Co", ticker="T", source="yellowbrick/2", sr_ref="SR-2",
        prediction="PASS", label="MAYBE", actual="WATCH", screening=_fake_screening(),
        judged_at="2026-06-01T00:00:00+00:00", prompt_version=2, brad_comment=None,
    )
    gate = te.GateResult(checks=[("placeholder check", True, "ok")], audit=None, rows=[row])

    out = "\n".join(te.render_report(
        version_label="v3", checkpoint_feedback_id=60, rules_sha256="deadbeef", gate=gate, rules_text=RULES_TEXT,
    ))

    assert "Brad's stated reason: (none recorded)" in out


def test_evaluate_taste_integration_shows_brads_stored_comment_for_a_false_negative(tmp_path, monkeypatch):
    config, _ = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _standard_holdout(conn)
    fn_feedback_id = conn.execute(
        "SELECT feedback_id FROM feedback WHERE ticker = 'H5'"
    ).fetchone()[0]
    conn.execute(
        "UPDATE feedback SET user_comment = ? WHERE feedback_id = ?",
        ("I think the turnaround thesis is real even if unproven yet.", fn_feedback_id),
    )
    conn.commit()
    conn.close()

    code, output = _evaluate(config)

    assert code == 0
    assert "I think the turnaround thesis is real even if unproven yet." in output


# --- v3 (and any frozen Taste) must stay untouched by these diagnostics ------------------


def test_label_distribution_and_evaluation_diagnostics_are_read_only(tmp_path, monkeypatch):
    config, _ = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _standard_holdout(conn)
    conn.close()
    before = _snapshot_all_tables(config)

    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.cmd_taste_status(config) == 0
        assert cli.cmd_evaluate_taste(config, version=2) == 0

    after = _snapshot_all_tables(config)
    assert after == before

    conn = db.connect(config.database_path)
    versions = [r["version_number"] for r in conn.execute("SELECT version_number FROM taste_versions")]
    build_unlocked_values = [r["build_unlocked"] for r in conn.execute("SELECT build_unlocked FROM taste_versions")]
    conn.close()
    assert versions == [1, 2]
    assert build_unlocked_values == [0, 0]
