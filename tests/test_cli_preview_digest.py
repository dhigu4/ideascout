"""Tests for `python run.py preview-digest`.

LOCAL PREVIEW ONLY: no outbound notification exists anywhere in this
codebase (see test_notifier.py), so these tests only check what gets
printed and written to digest_shown_sources.
"""

from __future__ import annotations

import dataclasses

from ideascout import cli, db
from ideascout.config import Config
from tests.test_cli_build_taste import make_config as _base_make_config
from tests.test_cli_collect_source import (
    make_page,
    patch_browser,
    patch_extraction_and_screening,
    write_screen_rules,
    build_taste_v1,
)
from tests.test_cli_shadow import make_prediction


def make_config(tmp_path, **overrides) -> Config:
    config = _base_make_config(tmp_path)
    fields = {
        "browser_profiles_dir": tmp_path / "browser-profiles",
        "raw_storage_dir": tmp_path / "raw",
        "screen_rules_path": tmp_path / "IDEA_SCREEN_RULES.md",
    }
    fields.update(overrides)
    return dataclasses.replace(config, **fields)


def collect_two_sources(config, monkeypatch, *, predictions=None):
    """predictions: optional dict {external_id: ShadowPrediction} to give
    different predictions per pitch; default WATCH for both.
    """
    patch_browser(monkeypatch, make_page())
    if predictions is None:
        patch_extraction_and_screening(monkeypatch)
    else:
        # Screen each source with its own scripted prediction, keyed by
        # ticker (abc123 -> XYZ, def456 -> ABC per make_page's fixture).
        from ideascout import idea_extraction, shadow
        from tests.test_cli_shadow import make_extracted_idea

        def fake_extract(client, model, source_text, logger=None):
            if "XYZ Corp" in source_text or "5x normalized" in source_text or "6x normalized" in source_text:
                return make_extracted_idea(company="XYZ Corp", ticker="XYZ")
            return make_extracted_idea(company="ABC Inc", ticker="ABC")

        def fake_screen(client, model, *, idea_taste_body, screen_rules_body, idea_record, logger=None):
            return predictions[idea_record["ticker"]]

        monkeypatch.setattr(idea_extraction, "build_client", lambda api_key: object())
        monkeypatch.setattr(idea_extraction, "extract_idea", fake_extract)
        monkeypatch.setattr(shadow, "build_client", lambda api_key: object())
        monkeypatch.setattr(shadow, "screen_idea", fake_screen)

    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)


def test_preview_digest_no_taste_yet(tmp_path, capsys):
    config = make_config(tmp_path)
    exit_code = cli.cmd_preview_digest(config)
    assert exit_code == 0
    assert "No taste model" in capsys.readouterr().out


def test_preview_digest_no_candidates(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)

    exit_code = cli.cmd_preview_digest(config)
    assert exit_code == 0
    assert "No new ideas to show" in capsys.readouterr().out


def test_preview_digest_shows_investigate_now_and_watch(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)

    collect_two_sources(
        config, monkeypatch,
        predictions={
            "XYZ": make_prediction(overall_prediction="INVESTIGATE_NOW"),
            "ABC": make_prediction(overall_prediction="WATCH"),
        },
    )

    exit_code = cli.cmd_preview_digest(config)
    assert exit_code == 0
    output = capsys.readouterr().out

    assert "XYZ Corp" in output
    assert "ABC Inc" in output
    assert "Shadow prediction" not in output  # exact field label isn't required, content is
    assert "Why surfaced: INVESTIGATE_NOW" in output
    assert "Why surfaced: WATCH" in output
    assert "Original source:" in output


def test_preview_digest_excludes_pass_predictions(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)

    collect_two_sources(
        config, monkeypatch,
        predictions={
            "XYZ": make_prediction(overall_prediction="PASS"),
            "ABC": make_prediction(overall_prediction="WATCH"),
        },
    )

    exit_code = cli.cmd_preview_digest(config)
    assert exit_code == 0
    output = capsys.readouterr().out

    assert "XYZ Corp" not in output
    assert "ABC Inc" in output


def test_preview_digest_prefers_investigate_now_before_watch_under_cap(tmp_path, monkeypatch, capsys):
    """With more than 5 candidates, INVESTIGATE_NOW ones must be included
    ahead of WATCH ones under the cap-at-5 rule.
    """
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)

    # Directly construct 6 sources + screenings (3 WATCH, 3 INVESTIGATE_NOW)
    # to exercise the cap/priority logic without needing 6 fake pitches.
    conn = db.connect(config.database_path)
    predictions_plan = ["WATCH", "WATCH", "WATCH", "INVESTIGATE_NOW", "INVESTIGATE_NOW", "INVESTIGATE_NOW"]
    source_ids = []
    for i, overall in enumerate(predictions_plan):
        source_id = db.insert_collected_source(
            conn, source_name="yellowbrick", external_id=f"id{i}", canonical_url=f"https://x/pitch/id{i}",
            discovered_at="2026-01-01T00:00:00+00:00", discovery_title=f"Title {i}", source_date=None,
            source_title=f"Title {i}", author=None, ticker=f"T{i}", company=f"Company {i}",
            source_type="stock_pitch", content_hash=f"hash{i}", raw_html_path=f"/x/{i}.html",
            metadata_json="{}", created_at="2026-01-01T00:00:00+00:00",
        )
        db.update_collected_source_extracted(
            conn, source_id=source_id, company=f"Company {i}", ticker=f"T{i}", source_title=f"Title {i}",
            source_date=None, business_summary="x", core_thesis="x", why_mispriced="x",
            future_earnings_change="x", upside_case="x", downside_or_key_risks="x", catalysts="x",
            what_must_be_true="x", evidence_of_market_misunderstanding="x", known_unknowns="x",
            extraction_model_name="fake-model", extracted_at="2026-01-01T00:00:00+00:00",
        )
        db.insert_source_screening(
            conn, source_id=source_id, created_at=f"2026-01-01T00:0{i}:00+00:00",
            taste_version=latest_taste["version_number"], taste_sha256=latest_taste["idea_taste_sha256"],
            screen_rules_path=str(config.screen_rules_path), screen_rules_sha256="rules_hash",
            content_hash=f"hash{i}", model_name="fake-model", overall_prediction=overall,
            mispricing="Plausible", variant_perception="Plausible", upside="Potentially sufficient",
            business_quality="Plausible", downside="Acceptable", key_reasons_json="[]",
            key_concerns_json="[]", critical_questions_json="[]", confidence="MEDIUM",
        )
        source_ids.append(source_id)
    conn.close()

    exit_code = cli.cmd_preview_digest(config)
    assert exit_code == 0
    output = capsys.readouterr().out

    for i in (3, 4, 5):  # the three INVESTIGATE_NOW ones
        assert f"Company {i}" in output
    shown_watch_count = sum(1 for i in (0, 1, 2) if f"Company {i}" in output)
    assert shown_watch_count == 2  # exactly 2 of the 3 WATCH candidates fill the remaining slot(s)


def test_preview_digest_never_shows_more_than_five(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)

    conn = db.connect(config.database_path)
    for i in range(8):
        source_id = db.insert_collected_source(
            conn, source_name="yellowbrick", external_id=f"id{i}", canonical_url=f"https://x/pitch/id{i}",
            discovered_at="2026-01-01T00:00:00+00:00", discovery_title=f"Title {i}", source_date=None,
            source_title=f"Title {i}", author=None, ticker=f"T{i}", company=f"Company {i}",
            source_type="stock_pitch", content_hash=f"hash{i}", raw_html_path=f"/x/{i}.html",
            metadata_json="{}", created_at="2026-01-01T00:00:00+00:00",
        )
        db.update_collected_source_extracted(
            conn, source_id=source_id, company=f"Company {i}", ticker=f"T{i}", source_title=f"Title {i}",
            source_date=None, business_summary="x", core_thesis="x", why_mispriced="x",
            future_earnings_change="x", upside_case="x", downside_or_key_risks="x", catalysts="x",
            what_must_be_true="x", evidence_of_market_misunderstanding="x", known_unknowns="x",
            extraction_model_name="fake-model", extracted_at="2026-01-01T00:00:00+00:00",
        )
        db.insert_source_screening(
            conn, source_id=source_id, created_at="2026-01-01T00:00:00+00:00",
            taste_version=latest_taste["version_number"], taste_sha256=latest_taste["idea_taste_sha256"],
            screen_rules_path=str(config.screen_rules_path), screen_rules_sha256="rules_hash",
            content_hash=f"hash{i}", model_name="fake-model", overall_prediction="INVESTIGATE_NOW",
            mispricing="Plausible", variant_perception="Plausible", upside="Potentially sufficient",
            business_quality="Plausible", downside="Acceptable", key_reasons_json="[]",
            key_concerns_json="[]", critical_questions_json="[]", confidence="MEDIUM",
        )
    conn.close()

    cli.cmd_preview_digest(config)
    output = capsys.readouterr().out
    shown = sum(1 for i in range(8) if f"Company {i}" in output)
    assert shown == 5


def test_preview_digest_deduplicates_already_shown_ideas(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)
    collect_two_sources(
        config, monkeypatch,
        predictions={"XYZ": make_prediction(overall_prediction="WATCH"), "ABC": make_prediction(overall_prediction="WATCH")},
    )

    cli.cmd_preview_digest(config)
    first_output = capsys.readouterr().out
    assert "XYZ Corp" in first_output

    exit_code = cli.cmd_preview_digest(config)
    assert exit_code == 0
    second_output = capsys.readouterr().out
    assert "XYZ Corp" not in second_output
    assert "No new ideas to show" in second_output


def test_preview_digest_does_not_update_taste(tmp_path, monkeypatch):
    from pathlib import Path

    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)
    original_content = Path(latest_taste["idea_taste_path"]).read_text(encoding="utf-8")

    collect_two_sources(config, monkeypatch)
    cli.cmd_preview_digest(config)

    conn = db.connect(config.database_path)
    all_taste_versions = conn.execute("SELECT * FROM taste_versions").fetchall()
    conn.close()
    assert len(all_taste_versions) == 1
    assert Path(latest_taste["idea_taste_path"]).read_text(encoding="utf-8") == original_content


def test_preview_digest_never_sends_anything():
    """Structural guarantee: cli.py never imports ideascout.notifier at
    all, so cmd_preview_digest has no way to reach any (even disabled)
    send path -- this is stronger than just checking notifier.py raises.
    """
    assert not hasattr(cli, "notifier")
    assert "notifier" not in vars(cli)
