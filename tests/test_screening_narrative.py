"""Tests for the screening-output fixes: the narrative-completeness contract
(ideascout/screening_narrative.py), the one-shot narrative repair
(shadow.repair_narrative + cli._repair_screening_narrative_once), the shared
digest eligibility guard, the default show-source-screenings view, and the
targeted rescreen-source command.

Real production evidence: ITECH.ST (external_id 144273) was saved as a valid
WATCH whose why/concerns/questions were all empty -- the screening schema
permits empty lists and nothing validated them -- and the digest would have
shown it. Stale screens attached to source versions later repaired to
INCOMPLETE_CONTENT also still appeared in the default screening view.

No real LLM or network calls: shadow.screen_idea / shadow.repair_narrative /
shadow.build_client are monkeypatched everywhere. Browser access is a fake
page. Every database lives under pytest's tmp_path.
"""

from __future__ import annotations

import dataclasses
import io
import contextlib
import json

import pytest

from ideascout import cli, db, idea_extraction, screening_narrative, shadow, structured_llm, taste
from ideascout.sources import browser as source_browser
from ideascout.sources import yellowbrick
from tests.browser_fakes import FakePage, make_fake_persistent_chrome_context
from tests.test_cli_build_taste import insert_n_eligible, make_config as _base_make_config, patch_taste
from tests.test_cli_send_digest import patch_agentmail

RULES_TEXT = "1. Mispricing\n   Is there a specific reason the market may be wrong?\n"
REAL_PITCH_HTML = """
<html><head><title>ITECH stock pitch - 2026-09-28</title></head><body>
<article class="pitch-card">
<h1>I-Tech AB (ITECH)</h1>
<p>I-Tech trades at a discount after a sector-wide derating. Catalysts
include asset disposals and a return to dividend payments. Key risks
include financing costs and continued sector weakness in the business.</p>
</article>
</body></html>
"""
PITCH_URL = "https://www.joinyellowbrick.com/sp/144273"
FEED_HTML = (
    '<html><body><article class="pitch-card"><h2>I-Tech AB</h2>'
    '<a href="/sp/144273">Read full article</a></article></body></html>'
)


def make_config(tmp_path, **overrides):
    config = _base_make_config(tmp_path)
    fields = {
        "browser_profiles_dir": tmp_path / "browser-profiles",
        "raw_storage_dir": tmp_path / "raw",
        "screen_rules_path": tmp_path / "IDEA_SCREEN_RULES.md",
        "agentmail_api_key": "fake-agentmail-key",
        "agentmail_inbox_id": "inbox_abc",
        "digest_recipient_email": "brad@example.com",
        "alerts_enabled": True,
    }
    fields.update(overrides)
    return dataclasses.replace(config, **fields)


def write_rules(config) -> str:
    config.screen_rules_path.write_text(RULES_TEXT, encoding="utf-8", newline="")
    return taste.compute_content_sha256(RULES_TEXT)


def build_taste_v1(config, monkeypatch):
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()
    patch_taste(monkeypatch, body="## Strong Positive Signals\nHidden earnings power.\n")
    assert cli.cmd_build_taste(config) == 0
    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    conn.close()
    return latest


def prediction(overall="WATCH", reasons=None, concerns=None, questions=None, confidence="MEDIUM", downside="Problematic"):
    return shadow.ShadowPrediction(
        overall_prediction=overall,
        mispricing="Plausible",
        variant_perception="Plausible",
        upside="Potentially sufficient",
        business_quality="Plausible",
        downside=downside,
        key_reasons=list(reasons or []),
        key_concerns=list(concerns or []),
        critical_questions=list(questions or []),
        confidence=confidence,
    )


FULL = dict(reasons=["Hidden earnings power."], concerns=["Execution risk."], questions=["When does the segment breakeven?"])


def insert_screened_source(conn, latest_taste, rules_sha, *, external_id, pred, collection_status="COLLECTED",
                           ticker="ITECH", company="I-Tech AB"):
    source_id = db.insert_collected_source(
        conn, source_name="yellowbrick", external_id=external_id,
        canonical_url=f"https://www.joinyellowbrick.com/sp/{external_id}",
        discovered_at="2026-01-01T00:00:00+00:00", discovery_title=company, source_date=None,
        source_title=company, author=None, ticker=ticker, company=company, source_type="stock_pitch",
        content_hash=f"hash-{external_id}", raw_html_path=f"/x/{external_id}.html", metadata_json="{}",
        created_at="2026-01-01T00:00:00+00:00", collection_status=collection_status,
    )
    db.update_collected_source_extracted(
        conn, source_id=source_id, company=company, ticker=ticker, source_title=company, source_date=None,
        business_summary="x", core_thesis="x", why_mispriced="x", future_earnings_change="x", upside_case="x",
        downside_or_key_risks="x", catalysts="x", what_must_be_true="x",
        evidence_of_market_misunderstanding="x", known_unknowns="x",
        extraction_model_name="fake-model", extracted_at="2026-01-01T00:00:00+00:00",
    )
    screening_id = db.insert_source_screening(
        conn, source_id=source_id, created_at="2026-01-01T00:00:00+00:00",
        taste_version=latest_taste["version_number"], taste_sha256=latest_taste["idea_taste_sha256"],
        screen_rules_path="IDEA_SCREEN_RULES.md", screen_rules_sha256=rules_sha,
        content_hash=f"hash-{external_id}", model_name="fake-model",
        overall_prediction=pred.overall_prediction, mispricing=pred.mispricing,
        variant_perception=pred.variant_perception, upside=pred.upside,
        business_quality=pred.business_quality, downside=pred.downside,
        key_reasons_json=json.dumps(pred.key_reasons), key_concerns_json=json.dumps(pred.key_concerns),
        critical_questions_json=json.dumps(pred.critical_questions), confidence=pred.confidence,
    )
    return source_id, screening_id


class _LLMCounter:
    def __init__(self):
        self.screen_calls = 0
        self.repair_calls = 0
        self.repair_fields: list[list[str]] = []


def patch_llm(monkeypatch, *, screen_result=None, repair_result=None, repair_raises=None, counter=None):
    counter = counter or _LLMCounter()
    monkeypatch.setattr(shadow, "build_client", lambda api_key: object())

    def fake_screen(client, model, *, idea_taste_body, screen_rules_body, idea_record, logger=None):
        counter.screen_calls += 1
        return screen_result

    def fake_repair(client, model, *, idea_taste_body, screen_rules_body, idea_record, prediction,
                    missing_fields, logger=None):
        counter.repair_calls += 1
        counter.repair_fields.append(list(missing_fields))
        if repair_raises is not None:
            raise repair_raises
        return repair_result

    monkeypatch.setattr(shadow, "screen_idea", fake_screen)
    monkeypatch.setattr(shadow, "repair_narrative", fake_repair)
    return counter


def fail_all_llm(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("no LLM call allowed here")

    monkeypatch.setattr(shadow, "build_client", boom)
    monkeypatch.setattr(shadow, "screen_idea", boom)
    monkeypatch.setattr(shadow, "repair_narrative", boom)


def snapshot_screenings(config):
    conn = db.connect(config.database_path)
    rows = [dict(r) for r in conn.execute("SELECT * FROM source_screenings ORDER BY screening_id")]
    conn.close()
    return rows


def snapshot_collected(config):
    conn = db.connect(config.database_path)
    rows = [dict(r) for r in conn.execute("SELECT * FROM collected_sources ORDER BY source_id")]
    conn.close()
    return rows


# --- PART 1: narrative contract (pure) -------------------------------------------


def test_full_watch_is_narrative_complete():
    assert screening_narrative.narrative_gaps("WATCH", ["r"], ["c"], ["q"]) == []


def test_full_investigate_now_is_narrative_complete():
    assert screening_narrative.narrative_gaps("INVESTIGATE_NOW", ["r"], ["c"], ["q"]) == []


def test_pass_may_stay_sparse():
    assert screening_narrative.narrative_gaps("PASS", [], [], []) == []


def test_insufficient_information_unaffected():
    assert screening_narrative.narrative_gaps("INSUFFICIENT_INFORMATION", [], [], []) == []


@pytest.mark.parametrize("missing", ["key_reasons", "key_concerns", "critical_questions"])
def test_each_required_field_is_independently_required(missing):
    fields = {"key_reasons": ["r"], "key_concerns": ["c"], "critical_questions": ["q"]}
    fields[missing] = []
    assert screening_narrative.narrative_gaps("WATCH", fields["key_reasons"], fields["key_concerns"], fields["critical_questions"]) == [missing]


@pytest.mark.parametrize("placeholder", ["", "   ", "N/A", "none.", "-", "...", "(none)", "TBD", "unknown", "!!!"])
def test_placeholder_and_blank_text_counts_as_missing(placeholder):
    assert screening_narrative.narrative_gaps("WATCH", [placeholder], ["c"], ["q"]) == ["key_reasons"]


def test_null_and_non_string_items_count_as_missing():
    assert screening_narrative.narrative_gaps("WATCH", None, ["c"], ["q"]) == ["key_reasons"]
    assert screening_narrative.narrative_gaps("WATCH", [None, 5], ["c"], ["q"]) == ["key_reasons"]


def test_merge_never_overwrites_a_substantive_original_field():
    original = {"key_reasons": ["Original reason."], "key_concerns": [], "critical_questions": []}
    repair = {"key_reasons": ["Repair reason."], "key_concerns": ["Repair concern."], "critical_questions": ["Q?"]}
    merged = screening_narrative.merge_narrative(original, repair)
    assert merged["key_reasons"] == ["Original reason."]
    assert merged["key_concerns"] == ["Repair concern."]
    assert merged["critical_questions"] == ["Q?"]


# --- PART 2: collect-source's one bounded repair ----------------------------------


def _collect_with_prediction(tmp_path, monkeypatch, pred, *, repair_result=None, repair_raises=None):
    config = make_config(tmp_path)
    write_rules(config)
    build_taste_v1(config, monkeypatch)
    page = FakePage({yellowbrick.HOME_URL: FEED_HTML, yellowbrick.FEED_URL: FEED_HTML, PITCH_URL: REAL_PITCH_HTML})
    fake_cm, _ = make_fake_persistent_chrome_context(page)
    monkeypatch.setattr(source_browser, "persistent_chrome_context", fake_cm)
    monkeypatch.setattr(idea_extraction, "build_client", lambda api_key: object())
    monkeypatch.setattr(idea_extraction, "extract_idea", lambda *a, **k: _extracted())
    yellowbrick.POLITE_DELAY_SECONDS = 0
    counter = patch_llm(monkeypatch, screen_result=pred, repair_result=repair_result, repair_raises=repair_raises)
    assert cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10) == 0
    return config, counter


def _extracted():
    from tests.test_cli_shadow import make_extracted_idea

    return make_extracted_idea()


def test_collect_full_watch_makes_zero_repair_calls(tmp_path, monkeypatch):
    _, counter = _collect_with_prediction(tmp_path, monkeypatch, prediction("WATCH", **FULL))
    assert counter.screen_calls == 1
    assert counter.repair_calls == 0


def test_collect_full_investigate_now_makes_zero_repair_calls(tmp_path, monkeypatch):
    _, counter = _collect_with_prediction(tmp_path, monkeypatch, prediction("INVESTIGATE_NOW", **FULL))
    assert counter.repair_calls == 0


def test_collect_sparse_pass_makes_zero_repair_calls(tmp_path, monkeypatch):
    _, counter = _collect_with_prediction(tmp_path, monkeypatch, prediction("PASS"))
    assert counter.repair_calls == 0


@pytest.mark.parametrize(
    "missing_kwargs, expected_field",
    [
        (dict(reasons=[], concerns=["c"], questions=["q"]), "key_reasons"),
        (dict(reasons=["r"], concerns=[], questions=["q"]), "key_concerns"),
        (dict(reasons=["r"], concerns=["c"], questions=[]), "critical_questions"),
    ],
)
def test_collect_watch_missing_one_field_triggers_exactly_one_repair(tmp_path, monkeypatch, missing_kwargs, expected_field):
    repair = shadow.NarrativeRepair(key_reasons=["Filled."], key_concerns=["Filled."], critical_questions=["Filled?"])
    _, counter = _collect_with_prediction(
        tmp_path, monkeypatch, prediction("WATCH", **missing_kwargs), repair_result=repair
    )
    assert counter.screen_calls == 1
    assert counter.repair_calls == 1
    assert counter.repair_fields == [[expected_field]]


def test_repair_cannot_change_classification_or_dimensions(tmp_path, monkeypatch):
    """The repair response model has no classification fields at all, so a
    response can only ever contribute narrative. Verify the stored screening
    row is byte-identical to the original model output afterwards.
    """
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    _, screening_id = insert_screened_source(conn, latest, rules_sha, external_id="144273", pred=prediction("WATCH", reasons=[], concerns=["c"], questions=["q"]))
    conn.close()
    before = snapshot_screenings(config)

    assert set(shadow.NarrativeRepair.model_fields) == {"key_reasons", "key_concerns", "critical_questions"}

    patch_llm(monkeypatch, repair_result=shadow.NarrativeRepair(key_reasons=["Filled."]))
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.cmd_rescreen_source(config, "yellowbrick", "144273") == 0

    after = snapshot_screenings(config)
    assert after == before  # the original screening row is untouched, bit for bit
    assert after[0]["overall_prediction"] == "WATCH"
    assert after[0]["confidence"] == "MEDIUM"


def test_repair_does_not_overwrite_existing_substantive_field(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    source_id, _ = insert_screened_source(conn, latest, rules_sha, external_id="144273", pred=prediction("WATCH", reasons=["Original reason."], concerns=[], questions=["Q?"]))
    conn.close()

    patch_llm(monkeypatch, repair_result=shadow.NarrativeRepair(key_reasons=["Different reason."], key_concerns=["Filled concern."]))
    capture = io.StringIO()
    with contextlib.redirect_stdout(capture):
        cli.cmd_rescreen_source(config, "yellowbrick", "144273")

    conn = db.connect(config.database_path)
    screening = db.get_latest_source_screening_for_source(conn, source_id)
    effective = cli._effective_narrative(conn, screening)
    conn.close()
    assert effective["key_reasons"] == ["Original reason."]
    assert effective["key_concerns"] == ["Filled concern."]


def test_failed_repair_is_persisted_and_digest_ineligible(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    source_id, screening_id = insert_screened_source(conn, latest, rules_sha, external_id="144273", pred=prediction("WATCH", reasons=[], concerns=[], questions=[]))
    conn.close()

    patch_llm(monkeypatch, repair_raises=structured_llm.StructuredGenerationError("simulated failure"))
    capture = io.StringIO()
    with contextlib.redirect_stdout(capture):
        cli.cmd_rescreen_source(config, "yellowbrick", "144273")

    conn = db.connect(config.database_path)
    attempts = conn.execute("SELECT status FROM source_screening_narrative_repairs WHERE screening_id = ?", (screening_id,)).fetchall()
    selected, _, narrative_suppressed = cli.select_digest_candidates_detailed(conn, latest["version_number"])
    conn.close()
    assert [a["status"] for a in attempts] == ["FAILED"]
    assert narrative_suppressed == 1
    assert source_id not in {r["source_id"] for r in selected}


def test_successful_repair_becomes_digest_eligible(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    source_id, _ = insert_screened_source(conn, latest, rules_sha, external_id="144273", pred=prediction("WATCH", reasons=["r"], concerns=["c"], questions=[]))
    conn.close()

    patch_llm(monkeypatch, repair_result=shadow.NarrativeRepair(critical_questions=["What drives the recovery?"]))
    with contextlib.redirect_stdout(io.StringIO()):
        cli.cmd_rescreen_source(config, "yellowbrick", "144273")

    conn = db.connect(config.database_path)
    selected, _, narrative_suppressed = cli.select_digest_candidates_detailed(conn, latest["version_number"])
    conn.close()
    assert narrative_suppressed == 0
    assert source_id in {r["source_id"] for r in selected}


# --- PART 3: shared digest guard (preview + send) ----------------------------------


def _digest_fixture(tmp_path, monkeypatch, *, incomplete_pred, complete_pred):
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    incomplete_id, _ = insert_screened_source(conn, latest, rules_sha, external_id="111", pred=incomplete_pred, ticker="BAD", company="Bad Co")
    complete_id, _ = insert_screened_source(conn, latest, rules_sha, external_id="222", pred=complete_pred, ticker="GOOD", company="Good Co")
    conn.close()
    return config, latest, incomplete_id, complete_id


def test_preview_digest_suppresses_incomplete_watch_and_prints_count(tmp_path, monkeypatch):
    config, latest, incomplete_id, complete_id = _digest_fixture(
        tmp_path, monkeypatch, incomplete_pred=prediction("WATCH", reasons=[], concerns=[], questions=[]), complete_pred=prediction("WATCH", **FULL)
    )
    fail_all_llm(monkeypatch)
    capture = io.StringIO()
    with contextlib.redirect_stdout(capture):
        cli.cmd_preview_digest(config)
    output = capture.getvalue()
    assert "Incomplete screening narratives suppressed: 1" in output
    assert "Good Co" in output
    assert "Bad Co" not in output


def test_send_digest_suppresses_incomplete_watch_and_does_not_mark_it_shown(tmp_path, monkeypatch):
    config, latest, incomplete_id, complete_id = _digest_fixture(
        tmp_path, monkeypatch, incomplete_pred=prediction("WATCH", reasons=[], concerns=[], questions=[]), complete_pred=prediction("WATCH", **FULL)
    )
    messages = patch_agentmail(monkeypatch)
    fail_all_llm(monkeypatch)
    capture = io.StringIO()
    with contextlib.redirect_stdout(capture):
        assert cli.cmd_send_digest(config, dry_run=False) == 0
    output = capture.getvalue()
    assert "Incomplete screening narratives suppressed: 1" in output
    body = messages.calls[0]["text"]
    assert "Good Co" in body and "Bad Co" not in body

    conn = db.connect(config.database_path)
    shown = {r["source_id"] for r in conn.execute("SELECT source_id FROM digest_shown_sources")}
    conn.close()
    assert incomplete_id not in shown
    assert complete_id in shown


def test_digest_selection_makes_zero_llm_calls(tmp_path, monkeypatch):
    config, latest, _, _ = _digest_fixture(
        tmp_path, monkeypatch, incomplete_pred=prediction("WATCH"), complete_pred=prediction("WATCH", **FULL)
    )
    fail_all_llm(monkeypatch)
    conn = db.connect(config.database_path)
    cli.select_digest_candidates_detailed(conn, latest["version_number"])
    conn.close()  # AssertionError from fail_all_llm would surface here


def test_pass_sparse_is_not_suppressed_by_the_guard(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_screened_source(conn, latest, rules_sha, external_id="333", pred=prediction("PASS"))
    conn.close()
    conn = db.connect(config.database_path)
    selected, _, narrative_suppressed = cli.select_digest_candidates_detailed(conn, latest["version_number"])
    conn.close()
    assert narrative_suppressed == 0
    assert selected == []  # PASS never enters the digest in the first place


# --- PART 5 + DISPLAY -----------------------------------------------------------------


def _display_fixture(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    _, current_id = insert_screened_source(conn, latest, rules_sha, external_id="500", pred=prediction("WATCH", **FULL), ticker="CUR", company="Current Co")
    _, stale_id = insert_screened_source(conn, latest, rules_sha, external_id="501", pred=prediction("INSUFFICIENT_INFORMATION", downside="Unknown"), ticker="STALE", company="Stale Co", collection_status="INCOMPLETE_CONTENT")
    conn.close()
    return config, current_id, stale_id


def test_default_show_source_screenings_excludes_incomplete_content_source(tmp_path, monkeypatch, capsys):
    config, _, _ = _display_fixture(tmp_path, monkeypatch)
    cli.cmd_show_source_screenings(config, limit=10)
    output = capsys.readouterr().out
    assert "Current Co" in output
    assert "Stale Co" not in output


def test_default_still_shows_current_collected_screens(tmp_path, monkeypatch, capsys):
    config, _, _ = _display_fixture(tmp_path, monkeypatch)
    cli.cmd_show_source_screenings(config, limit=10)
    assert "Current Co" in capsys.readouterr().out


def test_all_versions_still_displays_old_invalid_screening(tmp_path, monkeypatch, capsys):
    config, _, _ = _display_fixture(tmp_path, monkeypatch)
    cli.cmd_show_source_screenings(config, limit=10, all_versions=True)
    output = capsys.readouterr().out
    assert "Stale Co" in output
    assert "Current Co" in output


def test_display_filtering_never_mutates_or_deletes_rows(tmp_path, monkeypatch, capsys):
    config, _, _ = _display_fixture(tmp_path, monkeypatch)
    before_s, before_c = snapshot_screenings(config), snapshot_collected(config)
    cli.cmd_show_source_screenings(config, limit=10)
    cli.cmd_show_source_screenings(config, limit=10, all_versions=True)
    assert snapshot_screenings(config) == before_s
    assert snapshot_collected(config) == before_c


def test_display_marks_incomplete_narrative_visibly(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_screened_source(conn, latest, rules_sha, external_id="600", pred=prediction("WATCH"), ticker="EMPTY", company="Empty Narrative Co")
    conn.close()
    cli.cmd_show_source_screenings(config, limit=10)
    assert "Narrative: INCOMPLETE" in capsys.readouterr().out


# --- PART 4: targeted rescreen-source ---------------------------------------------------


def test_rescreen_exact_source_only_affects_requested_source(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    target_id, target_screening = insert_screened_source(conn, latest, rules_sha, external_id="144273", pred=prediction("WATCH"), ticker="ITECH", company="I-Tech AB")
    other_id, other_screening = insert_screened_source(conn, latest, rules_sha, external_id="144274", pred=prediction("WATCH"), ticker="OTHER", company="Other Co")
    conn.close()
    other_before = [r for r in snapshot_screenings(config) if r["screening_id"] == other_screening][0]

    patch_llm(monkeypatch, repair_result=shadow.NarrativeRepair(key_reasons=["Reason."], key_concerns=["Concern."], critical_questions=["Question?"]))
    capture = io.StringIO()
    with contextlib.redirect_stdout(capture):
        assert cli.cmd_rescreen_source(config, "yellowbrick", "144273") == 0
    assert "REPAIRED" in capture.getvalue()

    conn = db.connect(config.database_path)
    target_effective = cli._effective_narrative(conn, db.get_latest_source_screening_for_source(conn, target_id))
    other_effective = cli._effective_narrative(conn, db.get_latest_source_screening_for_source(conn, other_id))
    other_repairs = conn.execute("SELECT COUNT(*) FROM source_screening_narrative_repairs WHERE screening_id = ?", (other_screening,)).fetchone()[0]
    conn.close()

    assert target_effective["key_reasons"] == ["Reason."]
    assert other_repairs == 0
    assert [r for r in snapshot_screenings(config) if r["screening_id"] == other_screening][0] == other_before


def test_rescreen_keeps_old_screening_provenance(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    _, screening_id = insert_screened_source(conn, latest, rules_sha, external_id="144273", pred=prediction("WATCH"))
    conn.close()
    before = [r for r in snapshot_screenings(config) if r["screening_id"] == screening_id][0]

    patch_llm(monkeypatch, repair_result=shadow.NarrativeRepair(key_reasons=["r"], key_concerns=["c"], critical_questions=["q"]))
    with contextlib.redirect_stdout(io.StringIO()):
        cli.cmd_rescreen_source(config, "yellowbrick", "144273")

    after = [r for r in snapshot_screenings(config) if r["screening_id"] == screening_id][0]
    assert after == before  # the original row is never edited


def test_rescreen_complete_narrative_makes_no_llm_call(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    rules_sha = write_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    conn = db.connect(config.database_path)
    insert_screened_source(conn, latest, rules_sha, external_id="144273", pred=prediction("WATCH", **FULL))
    conn.close()
    counter = patch_llm(monkeypatch, repair_result=shadow.NarrativeRepair())
    assert cli.cmd_rescreen_source(config, "yellowbrick", "144273") == 0
    assert counter.repair_calls == 0
    assert counter.screen_calls == 0
    assert "already complete" in capsys.readouterr().out


def test_rescreen_unknown_external_id_changes_nothing(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_rules(config)
    build_taste_v1(config, monkeypatch)
    before = snapshot_screenings(config)
    fail_all_llm(monkeypatch)
    assert cli.cmd_rescreen_source(config, "yellowbrick", "999999") == 1
    assert snapshot_screenings(config) == before
