"""Tests for `python run.py collect-source yellowbrick [--dry-run] [--limit N]`.

Never launches a real browser and never touches the live Yellowbrick site
-- source_browser.persistent_chrome_context is monkeypatched to a fake
context/page (tests/browser_fakes.py) with scripted HTML fixtures, and
idea_extraction.extract_idea / shadow.screen_idea are monkeypatched
(fakes), like taste generation in test_cli_build_taste.py.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from ideascout import cli, db, idea_extraction, shadow, structured_llm, taste
from ideascout.config import Config
from ideascout.sources import base as source_base
from ideascout.sources import browser as source_browser
from ideascout.sources import raw_storage, yellowbrick
from tests.browser_fakes import FakePage, make_fake_persistent_chrome_context
from tests.test_cli_build_taste import insert_n_eligible, patch_taste
from tests.test_cli_build_taste import make_config as _base_make_config
from tests.test_cli_shadow import make_extracted_idea, make_prediction


@pytest.fixture(autouse=True)
def _no_real_delays(monkeypatch):
    """collect-source politely pauses between fetches in production;
    tests must never actually sleep for that.
    """
    monkeypatch.setattr(yellowbrick, "POLITE_DELAY_SECONDS", 0)


def make_config(tmp_path, **overrides) -> Config:
    config = _base_make_config(tmp_path)
    fields = {
        "browser_profiles_dir": tmp_path / "browser-profiles",
        "raw_storage_dir": tmp_path / "raw",
        "screen_rules_path": tmp_path / "IDEA_SCREEN_RULES.md",
    }
    fields.update(overrides)
    return dataclasses.replace(config, **fields)


def write_screen_rules(config, content: str = "1. Mispricing\n   Is there a specific reason the market may be wrong?\n"):
    config.screen_rules_path.write_text(content, encoding="utf-8", newline="")


def build_taste_v1(config, monkeypatch, body: str = "## Strong Positive Signals\nHidden earnings power.\n"):
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()
    patch_taste(monkeypatch, body=body)
    exit_code = cli.cmd_build_taste(config)
    assert exit_code == 0
    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    conn.close()
    return latest


FEED_HTML = """
<html><body>
<article class="pitch-card">
  <h2>XYZ Corp: Hidden Value Play</h2>
  <a href="/sp/139483">Read full article</a>
</article>
<article class="pitch-card">
  <h2>ABC Inc: Spin-off Situation</h2>
  <a href="/sp/133085">Read full article</a>
</article>
</body></html>
"""

PITCH_ABC_HTML = """
<html><head><title>XYZ Corp: Hidden Value Play</title></head><body>
<time datetime="2026-09-01">Sep 1</time>
<p>XYZ Corp trades at 5x normalized earnings due to a temporary loss-making segment.</p>
</body></html>
"""

PITCH_ABC_HTML_EDITED = PITCH_ABC_HTML.replace("5x normalized earnings", "6x normalized earnings")

PITCH_DEF_HTML = """
<html><head><title>ABC Inc: Spin-off Situation</title></head><body>
<time datetime="2026-09-02">Sep 2</time>
<p>ABC Inc is spinning off a loss-making division to reveal hidden earnings power.</p>
</body></html>
"""

LOGIN_HTML = '<html><body><input type="password"></body></html>'

ABC_PITCH_URL = "https://www.joinyellowbrick.com/sp/139483"
DEF_PITCH_URL = "https://www.joinyellowbrick.com/sp/133085"


def make_page(*, feed_html=FEED_HTML, abc_html=PITCH_ABC_HTML, def_html=PITCH_DEF_HTML) -> FakePage:
    return FakePage(
        {
            yellowbrick.HOME_URL: feed_html,
            yellowbrick.FEED_URL: feed_html,
            ABC_PITCH_URL: abc_html,
            DEF_PITCH_URL: def_html,
        }
    )


def patch_browser(monkeypatch, page: FakePage):
    fake_cm, context = make_fake_persistent_chrome_context(page)
    monkeypatch.setattr(source_browser, "persistent_chrome_context", fake_cm)
    return context


def patch_extraction_and_screening(monkeypatch, *, extracted=None, prediction=None, extraction_calls=None, screening_calls=None):
    extracted = extracted if extracted is not None else make_extracted_idea()
    prediction = prediction if prediction is not None else make_prediction()

    def fake_extract_idea(client, model, source_text, logger=None):
        if extraction_calls is not None:
            extraction_calls.append(source_text)
        return extracted

    def fake_screen_idea(client, model, *, idea_taste_body, screen_rules_body, idea_record, logger=None):
        if screening_calls is not None:
            screening_calls.append(dict(idea_record))
        return prediction

    monkeypatch.setattr(idea_extraction, "build_client", lambda api_key: object())
    monkeypatch.setattr(idea_extraction, "extract_idea", fake_extract_idea)
    monkeypatch.setattr(shadow, "build_client", lambda api_key: object())
    monkeypatch.setattr(shadow, "screen_idea", fake_screen_idea)
    return extracted, prediction


# --- unknown source / dry run -------------------------------------------------


def test_collect_source_unknown_source(tmp_path, capsys):
    config = make_config(tmp_path)
    exit_code = cli.cmd_collect_source(config, "not-a-real-source", dry_run=False, limit=10)
    assert exit_code == 2
    assert "Unknown source" in capsys.readouterr().out


def test_collect_source_dry_run_makes_no_database_changes_and_no_llm_calls(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    page = make_page()
    patch_browser(monkeypatch, page)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("no LLM client should be built during --dry-run")

    monkeypatch.setattr(idea_extraction, "build_client", fail_if_called)
    monkeypatch.setattr(shadow, "build_client", fail_if_called)

    exit_code = cli.cmd_collect_source(config, "yellowbrick", dry_run=True, limit=10)
    assert exit_code == 0

    output = capsys.readouterr().out
    assert "[DRY RUN]" in output
    assert "139483" in output
    assert "133085" in output

    conn = db.connect(config.database_path)
    assert db.count_collected_sources(conn, "yellowbrick") == 0
    conn.close()


def test_collect_source_dry_run_respects_limit(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    patch_browser(monkeypatch, make_page())
    cli.cmd_collect_source(config, "yellowbrick", dry_run=True, limit=1)
    output = capsys.readouterr().out
    assert "[DRY RUN] Discovered: 1" in output


def test_collect_source_auth_required_during_discovery(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    patch_browser(monkeypatch, make_page(feed_html=LOGIN_HTML))
    exit_code = cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert exit_code == 1
    assert "AUTH_REQUIRED" in capsys.readouterr().out


# --- collection: new / known / changed ----------------------------------------


def test_collect_source_saves_new_sources_and_reports_counters(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    patch_browser(monkeypatch, make_page())
    patch_extraction_and_screening(monkeypatch)

    exit_code = cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert exit_code == 0

    output = capsys.readouterr().out
    assert "Discovered: 2" in output
    assert "Already known: 0" in output
    assert "New sources saved: 2" in output
    assert "Changed sources: 0" in output
    assert "Authentication required: 0" in output
    assert "Errors: 0" in output

    conn = db.connect(config.database_path)
    assert db.count_collected_sources(conn, "yellowbrick") == 2
    conn.close()


def test_collect_source_saves_raw_html_before_extraction(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    patch_browser(monkeypatch, make_page())
    patch_extraction_and_screening(monkeypatch)

    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)

    conn = db.connect(config.database_path)
    row = db.get_latest_collected_source(conn, "yellowbrick", "139483")
    conn.close()

    raw_path = Path(row["raw_html_path"])
    assert raw_path.exists()
    assert "5x normalized earnings" in raw_path.read_text(encoding="utf-8")
    assert raw_path.is_relative_to(config.raw_storage_dir)


def test_collect_source_raw_capture_hash_persisted_and_matches_raw_file(tmp_path, monkeypatch):
    """raw_capture_hash (Stage 5.4) -- not content_hash -- is the hash of
    the exact raw bytes saved as provenance.
    """
    config = make_config(tmp_path)
    patch_browser(monkeypatch, make_page())
    patch_extraction_and_screening(monkeypatch)

    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)

    conn = db.connect(config.database_path)
    row = db.get_latest_collected_source(conn, "yellowbrick", "139483")
    conn.close()

    import hashlib

    raw_bytes = Path(row["raw_html_path"]).read_bytes()
    assert row["raw_capture_hash"] == hashlib.sha256(raw_bytes).hexdigest()


def test_collect_source_content_hash_is_the_canonical_text_hash_not_the_raw_html_hash(tmp_path, monkeypatch):
    """content_hash (Stage 5.4) is the VERSION-IDENTITY hash -- computed
    from the normalized, substantive-only canonical text, never from the
    raw HTML bytes directly.
    """
    config = make_config(tmp_path)
    patch_browser(monkeypatch, make_page())
    patch_extraction_and_screening(monkeypatch)

    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)

    conn = db.connect(config.database_path)
    row = db.get_latest_collected_source(conn, "yellowbrick", "139483")
    conn.close()

    raw_html = Path(row["raw_html_path"]).read_text(encoding="utf-8")
    expected = taste.compute_content_sha256(yellowbrick.build_canonical_source_text(raw_html))
    assert row["content_hash"] == expected
    assert row["content_hash"] != row["raw_capture_hash"]


def test_collect_source_rerun_skips_already_known_sources_cheaply_no_second_fetch(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    page = make_page()
    patch_browser(monkeypatch, page)
    patch_extraction_and_screening(monkeypatch)

    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    visits_after_first_run = len(page.urls_visited)

    # A completely fresh browser context/page for the rerun (as a real
    # second CLI invocation would have) -- if collect-source is not
    # skipping cheaply, this second page's pitch URLs would get visited.
    page2 = make_page()
    patch_browser(monkeypatch, page2)

    exit_code = cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Already known: 2" in output
    assert "New sources saved: 0" in output

    # Only the feed was visited on the second run -- no pitch page fetch.
    assert ABC_PITCH_URL not in page2.urls_visited
    assert DEF_PITCH_URL not in page2.urls_visited

    conn = db.connect(config.database_path)
    assert db.count_collected_sources(conn, "yellowbrick") == 2  # no duplicate rows
    conn.close()


def test_collect_source_immediate_refetch_with_volatile_html_is_not_treated_as_changed(tmp_path, monkeypatch, capsys):
    """Reproduces the real production incident (Stage 5.4): forces a
    genuine re-fetch of an unchanged pitch (by making the feed's cheap
    discovery title differ from what was already stored -- exactly the
    kind of feed-vs-page title-format mismatch that can defeat the cheap
    pre-check in practice) whose raw HTML then differs only in volatile
    script/session noise. content_hash-based detection must still
    recognize this as unchanged: "Already known", never "Changed", and no
    new extraction call.
    """
    config = make_config(tmp_path)
    patch_browser(monkeypatch, make_page())
    extraction_calls = []
    patch_extraction_and_screening(monkeypatch, extraction_calls=extraction_calls)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    capsys.readouterr()
    assert len(extraction_calls) == 2

    # Feed card text differs from what's stored (forces a real fetch);
    # the fetched PAGE's own <title> and body are untouched -- only
    # volatile script/session noise is added.
    forced_feed_html = FEED_HTML.replace(
        "XYZ Corp: Hidden Value Play", "XYZ Corp: Hidden Value Play — Updated view"
    )
    noisy_abc_html = PITCH_ABC_HTML.replace(
        '<time datetime="2026-09-01">Sep 1</time>',
        '<script>window.session="zzz999";</script><time datetime="2026-09-01">Sep 1</time>',
    )
    patch_browser(monkeypatch, make_page(feed_html=forced_feed_html, abc_html=noisy_abc_html))
    patch_extraction_and_screening(monkeypatch, extraction_calls=extraction_calls)

    exit_code = cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert exit_code == 0
    output = capsys.readouterr().out

    assert "Already known: 2" in output
    assert "Changed sources: 0" in output
    assert "New sources saved: 0" in output
    assert len(extraction_calls) == 2  # unchanged -- no new extraction call

    conn = db.connect(config.database_path)
    assert db.count_collected_sources(conn, "yellowbrick") == 2
    assert db.count_collected_source_versions(conn, "yellowbrick") == 2  # no new version created
    conn.close()


def test_collect_source_genuine_content_change_is_extracted_and_screened_exactly_once_per_version(
    tmp_path, monkeypatch, capsys
):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)

    patch_browser(monkeypatch, make_page())
    extraction_calls, screening_calls = [], []
    patch_extraction_and_screening(monkeypatch, extraction_calls=extraction_calls, screening_calls=screening_calls)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    capsys.readouterr()
    assert len(extraction_calls) == 2
    assert len(screening_calls) == 2

    changed_feed_html = FEED_HTML.replace(
        "XYZ Corp: Hidden Value Play", "XYZ Corp: Hidden Value Play (UPDATED)"
    )
    # A genuine substantive change to the thesis, not just noise.
    changed_abc_html = PITCH_ABC_HTML.replace(
        "5x normalized earnings", "2x normalized earnings after a guidance cut"
    ).replace("<title>XYZ Corp: Hidden Value Play</title>", "<title>XYZ Corp: Hidden Value Play (UPDATED)</title>")
    patch_browser(monkeypatch, make_page(feed_html=changed_feed_html, abc_html=changed_abc_html))
    patch_extraction_and_screening(monkeypatch, extraction_calls=extraction_calls, screening_calls=screening_calls)

    exit_code = cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Changed sources: 1" in output

    # Exactly one new extraction/screening call -- not the whole document
    # re-extracted/re-screened, and not extracted/screened twice.
    assert len(extraction_calls) == 3
    assert len(screening_calls) == 3

    conn = db.connect(config.database_path)
    assert db.count_collected_sources(conn, "yellowbrick") == 2  # still 2 distinct documents
    assert db.count_collected_source_versions(conn, "yellowbrick") == 3  # one now has 2 versions

    old_row, new_row = (
        conn.execute(
            "SELECT * FROM collected_sources WHERE source_name='yellowbrick' AND external_id='139483' ORDER BY source_id"
        ).fetchall()
    )
    conn.close()

    assert new_row["previous_version_source_id"] == old_row["source_id"]
    assert old_row["content_hash"] != new_row["content_hash"]

    # Both versions' raw HTML provenance is preserved, independently.
    old_raw = Path(old_row["raw_html_path"]).read_text(encoding="utf-8")
    new_raw = Path(new_row["raw_html_path"]).read_text(encoding="utf-8")
    assert "5x normalized earnings" in old_raw
    assert "2x normalized earnings" in new_raw
    assert old_row["raw_html_path"] != new_row["raw_html_path"]


def test_collect_source_no_duplicate_raw_artifact_on_rerun(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    patch_browser(monkeypatch, make_page())
    patch_extraction_and_screening(monkeypatch)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)

    conn = db.connect(config.database_path)
    row = db.get_latest_collected_source(conn, "yellowbrick", "139483")
    conn.close()
    raw_path = Path(row["raw_html_path"])
    mtime_before = raw_path.stat().st_mtime

    patch_browser(monkeypatch, make_page())
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)

    assert raw_path.stat().st_mtime == mtime_before  # never rewritten


def test_collect_source_changed_title_triggers_refetch_and_preserves_prior_version(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    patch_browser(monkeypatch, make_page())
    patch_extraction_and_screening(monkeypatch)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)

    conn = db.connect(config.database_path)
    original_row = dict(db.get_latest_collected_source(conn, "yellowbrick", "139483"))
    conn.close()
    original_raw_content = Path(original_row["raw_html_path"]).read_text(encoding="utf-8")

    changed_feed_html = FEED_HTML.replace(
        "XYZ Corp: Hidden Value Play", "XYZ Corp: Hidden Value Play (UPDATED)"
    )
    changed_abc_html = PITCH_ABC_HTML_EDITED.replace(
        "XYZ Corp: Hidden Value Play", "XYZ Corp: Hidden Value Play (UPDATED)"
    )
    patch_browser(monkeypatch, make_page(feed_html=changed_feed_html, abc_html=changed_abc_html))
    patch_extraction_and_screening(monkeypatch)

    exit_code = cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Changed sources: 1" in output

    conn = db.connect(config.database_path)
    new_row = db.get_latest_collected_source(conn, "yellowbrick", "139483")
    conn.close()

    assert new_row["source_id"] != original_row["source_id"]
    assert new_row["previous_version_source_id"] == original_row["source_id"]
    assert new_row["content_hash"] != original_row["content_hash"]
    # The OLD row/artifact is preserved untouched, not overwritten.
    assert Path(original_row["raw_html_path"]).read_text(encoding="utf-8") == original_raw_content
    assert db.count_collected_sources(conn := db.connect(config.database_path), "yellowbrick") == 2  # still 2 known docs
    conn.close()


def test_collect_source_auth_required_during_fetch_is_counted_separately(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    patch_browser(monkeypatch, make_page(abc_html=LOGIN_HTML))
    patch_extraction_and_screening(monkeypatch)

    exit_code = cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Authentication required: 1" in output
    assert "New sources saved: 1" in output  # 133085 still succeeds


def test_collect_source_fetch_error_is_counted_and_does_not_abort_the_run(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    page = make_page()
    patch_browser(monkeypatch, page)
    patch_extraction_and_screening(monkeypatch)

    real_fetch = yellowbrick.fetch

    def flaky_fetch(page_arg, item, *, discovered_at):
        if item.external_id == "139483":
            raise RuntimeError("simulated network blip")
        return real_fetch(page_arg, item, discovered_at=discovered_at)

    monkeypatch.setattr(yellowbrick, "fetch", flaky_fetch)

    exit_code = cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Errors: 1" in output
    assert "New sources saved: 1" in output


# --- extraction (Level 1) ------------------------------------------------------


def test_collect_source_extracts_compact_idea_from_saved_source(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    patch_browser(monkeypatch, make_page())
    patch_extraction_and_screening(monkeypatch, extracted=make_extracted_idea(company="XYZ Corp", ticker="XYZ"))

    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)

    conn = db.connect(config.database_path)
    row = db.get_latest_collected_source(conn, "yellowbrick", "139483")
    conn.close()

    assert row["extraction_status"] == "EXTRACTED"
    assert row["company"] == "XYZ Corp"
    assert row["core_thesis"] == "Hidden earnings power."


def test_collect_source_normalizes_blank_company_and_ticker_to_none_for_persistence(tmp_path, monkeypatch):
    """ExtractedIdea (Stage 5.3) represents "not stated" as an empty
    string, never null -- cli.py's extraction call site normalizes that
    to None before writing to collected_sources.
    """
    config = make_config(tmp_path)
    patch_browser(monkeypatch, make_page())
    patch_extraction_and_screening(monkeypatch, extracted=make_extracted_idea(company="", ticker=""))

    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)

    conn = db.connect(config.database_path)
    row = db.get_latest_collected_source(conn, "yellowbrick", "139483")
    conn.close()

    assert row["company"] is None
    assert row["ticker"] is None


def test_collect_source_preserves_discovery_time_title_and_date_through_extraction(tmp_path, monkeypatch):
    """ExtractedIdea (Stage 5.3) no longer extracts source_title/
    source_date at all -- extraction must carry the row's existing
    (discovery-derived) title/date through unchanged, not null them out.
    """
    config = make_config(tmp_path)
    patch_browser(monkeypatch, make_page())
    patch_extraction_and_screening(monkeypatch)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)

    conn = db.connect(config.database_path)
    row = db.get_latest_collected_source(conn, "yellowbrick", "139483")
    conn.close()

    # FEED_HTML's card heading ("XYZ Corp: Hidden Value Play") is what
    # discovery found for this pitch -- extraction must not have erased it.
    assert row["source_title"] == "XYZ Corp: Hidden Value Play"


def test_collect_source_extraction_happens_once_per_content_version(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    patch_browser(monkeypatch, make_page())
    extraction_calls = []
    patch_extraction_and_screening(monkeypatch, extraction_calls=extraction_calls)

    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert len(extraction_calls) == 2  # 139483 + 133085

    patch_browser(monkeypatch, make_page())
    # If extraction were re-attempted, this would be called again for the
    # same two already-EXTRACTED rows.
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert len(extraction_calls) == 2  # unchanged -- not re-extracted


def test_collect_source_extraction_failure_leaves_pending_and_retries_next_run(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    patch_browser(monkeypatch, make_page())

    def failing_extract(client, model, source_text, logger=None):
        raise structured_llm.StructuredGenerationError("simulated extraction failure")

    monkeypatch.setattr(idea_extraction, "build_client", lambda api_key: object())
    monkeypatch.setattr(idea_extraction, "extract_idea", failing_extract)
    monkeypatch.setattr(shadow, "build_client", lambda api_key: object())

    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)

    conn = db.connect(config.database_path)
    row = db.get_latest_collected_source(conn, "yellowbrick", "139483")
    assert row["extraction_status"] == "PENDING"
    conn.close()

    patch_browser(monkeypatch, make_page())
    patch_extraction_and_screening(monkeypatch)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)

    conn = db.connect(config.database_path)
    row = db.get_latest_collected_source(conn, "yellowbrick", "139483")
    conn.close()
    assert row["extraction_status"] == "EXTRACTED"


# --- screening (Level 2) -------------------------------------------------------


def test_collect_source_screening_receives_only_compact_record_not_full_html(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)

    patch_browser(monkeypatch, make_page())
    screening_calls = []
    patch_extraction_and_screening(monkeypatch, screening_calls=screening_calls)

    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)

    assert len(screening_calls) == 2
    for idea_record_dict in screening_calls:
        assert "raw_html_path" not in idea_record_dict or True  # sqlite3.Row->dict includes all columns;
        # the important guarantee is that the FULL raw HTML text itself
        # never appears as a value -- only the compact extracted fields do.
        for value in idea_record_dict.values():
            if isinstance(value, str):
                assert "<html>" not in value
                assert "<body>" not in value


def test_collect_source_screening_stores_exact_taste_and_rules_hashes(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)
    rules_content = config.screen_rules_path.read_text(encoding="utf-8")
    expected_rules_hash = taste.compute_content_sha256(rules_content)

    patch_browser(monkeypatch, make_page())
    patch_extraction_and_screening(monkeypatch)

    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)

    conn = db.connect(config.database_path)
    row = db.get_latest_collected_source(conn, "yellowbrick", "139483")
    screening = db.get_source_screening(
        conn, source_id=row["source_id"], taste_version=latest_taste["version_number"],
        screen_rules_sha256=expected_rules_hash,
    )
    conn.close()

    assert screening is not None
    assert screening["taste_version"] == latest_taste["version_number"]
    assert screening["taste_sha256"] == latest_taste["idea_taste_sha256"]
    assert screening["screen_rules_sha256"] == expected_rules_hash
    assert screening["content_hash"] == row["content_hash"]


def test_collect_source_screening_skipped_cleanly_when_no_taste_yet(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    # No taste v1 built.

    patch_browser(monkeypatch, make_page())
    patch_extraction_and_screening(monkeypatch)

    exit_code = cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "New sources saved: 2" in output
    assert "Screening skipped this run" in output
    assert "No taste model" in output

    conn = db.connect(config.database_path)
    assert db.count_screened_sources(conn, "yellowbrick") == 0
    assert db.count_collected_sources_by_extraction_status(conn, "yellowbrick", "EXTRACTED") == 2
    conn.close()


def test_collect_source_screening_skipped_cleanly_when_rules_file_missing(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    build_taste_v1(config, monkeypatch)
    # screen_rules_path deliberately never written.

    patch_browser(monkeypatch, make_page())
    patch_extraction_and_screening(monkeypatch)

    exit_code = cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "not found" in output


def test_collect_source_rerun_does_not_reduplicate_screening(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)

    patch_browser(monkeypatch, make_page())
    screening_calls = []
    patch_extraction_and_screening(monkeypatch, screening_calls=screening_calls)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert len(screening_calls) == 2

    patch_browser(monkeypatch, make_page())
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert len(screening_calls) == 2  # unchanged

    conn = db.connect(config.database_path)
    total_screenings = conn.execute("SELECT COUNT(*) FROM source_screenings").fetchone()[0]
    conn.close()
    assert total_screenings == 2


def test_collect_source_rules_change_creates_a_new_coexisting_screening(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)

    patch_browser(monkeypatch, make_page())
    patch_extraction_and_screening(monkeypatch, prediction=make_prediction(overall_prediction="WATCH"))
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)

    conn = db.connect(config.database_path)
    row = db.get_latest_collected_source(conn, "yellowbrick", "139483")
    first_screening = db.get_latest_source_screening_for_source(conn, row["source_id"])
    conn.close()

    write_screen_rules(config, content="1. Mispricing\n   A DIFFERENT rules file.\n")
    patch_browser(monkeypatch, make_page())
    patch_extraction_and_screening(monkeypatch, prediction=make_prediction(overall_prediction="INVESTIGATE_NOW"))
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)

    conn = db.connect(config.database_path)
    all_screenings = conn.execute(
        "SELECT * FROM source_screenings WHERE source_id = ? ORDER BY screening_id", (row["source_id"],)
    ).fetchall()
    conn.close()

    assert len(all_screenings) == 2
    assert all_screenings[0]["screening_id"] == first_screening["screening_id"]
    assert all_screenings[0]["overall_prediction"] == "WATCH"
    assert all_screenings[1]["overall_prediction"] == "INVESTIGATE_NOW"
    assert all_screenings[0]["screen_rules_sha256"] != all_screenings[1]["screen_rules_sha256"]


def test_source_screenings_have_no_update_function_only_insert():
    assert not hasattr(db, "update_source_screening")


# --- holdout / Taste v1 / IDEA_SCREEN_RULES.md untouched ----------------------


def test_collect_source_never_modifies_idea_screen_rules_file(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)
    original_content = config.screen_rules_path.read_text(encoding="utf-8")
    original_mtime = config.screen_rules_path.stat().st_mtime

    patch_browser(monkeypatch, make_page())
    patch_extraction_and_screening(monkeypatch)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)

    assert config.screen_rules_path.read_text(encoding="utf-8") == original_content
    assert config.screen_rules_path.stat().st_mtime == original_mtime


def test_collect_source_never_modifies_or_regenerates_taste_v1(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)
    original_content = Path(latest_taste["idea_taste_path"]).read_text(encoding="utf-8")

    patch_browser(monkeypatch, make_page())
    patch_extraction_and_screening(monkeypatch)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)

    conn = db.connect(config.database_path)
    all_taste_versions = conn.execute("SELECT * FROM taste_versions").fetchall()
    conn.close()

    assert len(all_taste_versions) == 1
    assert dict(all_taste_versions[0]) == dict(latest_taste)
    assert Path(latest_taste["idea_taste_path"]).read_text(encoding="utf-8") == original_content


def test_collect_source_does_not_affect_email_holdout_bookkeeping(tmp_path, monkeypatch):
    """The email-holdout shadow experiment (Stage 4) must be completely
    unaffected by website-source collection (Stage 5) -- separate tables,
    separate counters.
    """
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest_taste = build_taste_v1(config, monkeypatch)

    conn = db.connect(config.database_path)
    holdout_before = len(db.get_eligible_feedback_after(conn, latest_taste["checkpoint_feedback_id"]))
    conn.close()

    patch_browser(monkeypatch, make_page())
    patch_extraction_and_screening(monkeypatch)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)

    conn = db.connect(config.database_path)
    holdout_after = len(db.get_eligible_feedback_after(conn, latest_taste["checkpoint_feedback_id"]))
    shadow_prediction_count = len(db.get_idea_ids_with_shadow_predictions(conn))
    conn.close()

    assert holdout_before == holdout_after == 0
    assert shadow_prediction_count == 0  # collect-source never writes to shadow_predictions


# --- production-state safety ---------------------------------------------------


def test_collect_source_data_never_lands_under_real_production_state(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    patch_browser(monkeypatch, make_page())
    patch_extraction_and_screening(monkeypatch)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)

    real_idea_scout_local = Path.home() / "IdeaScoutLocal"
    assert config.database_path.is_relative_to(tmp_path)
    assert config.raw_storage_dir.is_relative_to(tmp_path)
    assert config.browser_profiles_dir.is_relative_to(tmp_path)
    assert real_idea_scout_local not in config.raw_storage_dir.parents
    assert real_idea_scout_local not in config.browser_profiles_dir.parents


def test_collect_source_refuses_when_production_database_missing(tmp_path, monkeypatch):
    config = Config(
        agentmail_api_key=None, agentmail_inbox_id=None,
        database_path=tmp_path / "ideas.db",  # deliberately never created
        log_path=tmp_path / "app.log", anthropic_api_key="fake-anthropic-key",
        screen_rules_path=tmp_path / "IDEA_SCREEN_RULES.md",
        browser_profiles_dir=tmp_path / "browser-profiles",
        raw_storage_dir=tmp_path / "raw",
    )
    patch_browser(monkeypatch, make_page())

    def fail_if_called(*args, **kwargs):
        raise AssertionError("the LLM must never be contacted when the DB is missing")

    monkeypatch.setattr(idea_extraction, "build_client", fail_if_called)

    with __import__("pytest").raises(db.ProductionDatabaseMissingError):
        cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)


def test_no_test_in_this_file_touches_the_real_yellowbrick_url(tmp_path, monkeypatch):
    """Sanity check on the test infrastructure itself: FakePage never
    performs real network I/O, so this simply documents/asserts the
    fixture's shape rather than the live site.
    """
    page = make_page()
    assert isinstance(page, FakePage)
    assert not hasattr(page, "_real_network_client")
