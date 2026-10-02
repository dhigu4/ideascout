"""Tests for the collect-source idempotent-retry production fix.

Real production failure: after the Yellowbrick invalid-page repair,
re-running `collect-source yellowbrick` crashed with
`sqlite3.IntegrityError: UNIQUE constraint failed: collected_sources.
source_name, collected_sources.external_id, collected_sources.content_hash`
while retrying a known INCOMPLETE_CONTENT source. Root cause: an
INCOMPLETE_CONTENT row's completeness can change even when its semantic
content_hash does not (a full-summary switch's checked state is an HTML
attribute, not canonical text), and content_hash can also flip back and
forth across repeated attempts -- but cmd_collect_source only ever
compared a freshly fetched hash against the LATEST row's hash, so a match
against an EARLIER, non-latest row's hash was missed and a plain INSERT
collided with it.

Never launches a real browser; source_browser.persistent_chrome_context is
monkeypatched to a fake context/page (tests/browser_fakes.py). No real LLM
calls -- idea_extraction/shadow are monkeypatched everywhere extraction or
screening could run. Every database lives under pytest's tmp_path.
"""

from __future__ import annotations

import dataclasses

from ideascout import cli, db, idea_extraction, shadow
from ideascout.sources import browser as source_browser
from ideascout.sources import yellowbrick
from tests.browser_fakes import FakePage, make_fake_persistent_chrome_context
from tests.test_cli_build_taste import insert_n_eligible, make_config as _base_make_config, patch_taste
from tests.test_cli_shadow import make_extracted_idea, make_prediction

HOME = yellowbrick.HOME_URL
FEED = yellowbrick.FEED_URL
PITCH_URL = "https://www.joinyellowbrick.com/sp/144246"
FEED_HTML = (
    '<html><body><article class="pitch-card"><h2>Some Co</h2>'
    '<a href="/sp/144246">Read full article</a></article></body></html>'
)

NOT_FOUND_HTML = """
<html><head><title>Yellowbrick Investing</title></head><body>
<h1>Page not found</h1>
<p>The page you are looking for does not exist, has been removed, or is
temporarily unavailable.</p>
</body></html>
"""

# COLLAPSED_UNCHECKED and COLLAPSED_CHECKED have IDENTICAL canonical text
# (the switch's aria-checked/data-state attributes are not part of
# canonical text) -- but _content_is_complete disagrees, since that is
# attribute-based. This is the exact mechanism behind the production bug.
COLLAPSED_UNCHECKED = """
<html><head><title>Berner: Some Co</title></head><body>
<article class="pitch-card">
<h1>Some Co (SOME)</h1>
<span class="mr-2 text-xs">Show full summary:</span>
<button id="full-summary-switch" type="button" role="switch" aria-checked="false" data-state="unchecked"></button>
<p>Short paragraph version of the summary text here for this pitch.</p>
</article>
</body></html>
"""

COLLAPSED_CHECKED = COLLAPSED_UNCHECKED.replace(
    'aria-checked="false" data-state="unchecked"', 'aria-checked="true" data-state="checked"'
)

REAL_PITCH_HTML = """
<html><head><title>Some Co stock pitch - 2026-09-28</title></head><body>
<article class="pitch-card">
<h1>Some Co (SOME)</h1>
<p>Some Co trades at a discount after a sector-wide derating. Catalysts
include asset disposals. Key risks include financing costs and continued
sector weakness overall for the business.</p>
</article>
</body></html>
"""

CHECKED_SWITCH_ELEMENTS = {
    "switch": [{"visible": True, "enabled": True, "aria_checked": "true", "data_state": "checked"}]
}


def make_config(tmp_path, **overrides):
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


def make_page(pitch_html: str, *, role_elements=None) -> FakePage:
    return FakePage(
        {HOME: FEED_HTML, FEED: FEED_HTML, PITCH_URL: pitch_html},
        role_elements=role_elements,
    )


def patch_browser(monkeypatch, page: FakePage):
    fake_cm, context = make_fake_persistent_chrome_context(page)
    monkeypatch.setattr(source_browser, "persistent_chrome_context", fake_cm)
    return context


def patch_extraction_and_screening(monkeypatch, *, extraction_calls=None, screening_calls=None):
    extracted = make_extracted_idea()
    prediction = make_prediction()

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


def fail_llm(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("must never call an LLM for an invalid/incomplete source")

    monkeypatch.setattr(idea_extraction, "build_client", fail_if_called)
    monkeypatch.setattr(idea_extraction, "extract_idea", fail_if_called)
    monkeypatch.setattr(shadow, "build_client", fail_if_called)
    monkeypatch.setattr(shadow, "screen_idea", fail_if_called)


# --- CASE A: persistent incomplete, identical hash, retried repeatedly ----------


def test_persistent_404_retried_twice_identical_hash_one_row_no_crash(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)

    patch_browser(monkeypatch, make_page(NOT_FOUND_HTML))
    fail_llm(monkeypatch)
    exit_code = cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert exit_code == 0
    capsys.readouterr()

    # A second run refetches the SAME 404 content -- must not crash, must
    # not create a second row.
    patch_browser(monkeypatch, make_page(NOT_FOUND_HTML))
    fail_llm(monkeypatch)
    exit_code = cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    row = db.get_latest_collected_source(conn, "yellowbrick", "144246")
    versions = db.count_collected_source_versions(conn, "yellowbrick")
    conn.close()

    assert versions == 1
    assert row["collection_status"] == "INCOMPLETE_CONTENT"


def test_persistent_incomplete_switch_page_retried_twice_identical_hash_one_row_no_crash(tmp_path, monkeypatch, capsys):
    """The exact production mechanism: a collapsed full-summary page whose
    switch stays unchecked across two separate fetches -- canonical text
    (and therefore content_hash) is identical both times.
    """
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)

    patch_browser(monkeypatch, make_page(COLLAPSED_UNCHECKED))
    fail_llm(monkeypatch)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    capsys.readouterr()

    patch_browser(monkeypatch, make_page(COLLAPSED_UNCHECKED))
    fail_llm(monkeypatch)
    exit_code = cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert exit_code == 0  # no IntegrityError

    conn = db.connect(config.database_path)
    row = db.get_latest_collected_source(conn, "yellowbrick", "144246")
    versions = db.count_collected_source_versions(conn, "yellowbrick")
    conn.close()

    assert versions == 1
    assert row["collection_status"] == "INCOMPLETE_CONTENT"
    assert row["extraction_status"] == "PENDING"


def test_three_repeated_runs_against_persistent_incomplete_source_stay_safe(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)

    for _ in range(3):
        patch_browser(monkeypatch, make_page(COLLAPSED_UNCHECKED))
        fail_llm(monkeypatch)
        exit_code = cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
        assert exit_code == 0

    conn = db.connect(config.database_path)
    versions = db.count_collected_source_versions(conn, "yellowbrick")
    conn.close()
    assert versions == 1


# --- CASE B: incomplete -> valid, SAME content hash (the production flip-flop) --


def test_incomplete_promoted_to_collected_in_place_with_same_hash(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)

    assert yellowbrick.build_canonical_source_text(COLLAPSED_UNCHECKED) == yellowbrick.build_canonical_source_text(
        COLLAPSED_CHECKED
    )

    patch_browser(monkeypatch, make_page(COLLAPSED_UNCHECKED))
    fail_llm(monkeypatch)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    capsys.readouterr()

    conn = db.connect(config.database_path)
    original = db.get_latest_collected_source(conn, "yellowbrick", "144246")
    conn.close()
    assert original["collection_status"] == "INCOMPLETE_CONTENT"

    extraction_calls, screening_calls = [], []
    patch_browser(monkeypatch, make_page(COLLAPSED_CHECKED, role_elements=CHECKED_SWITCH_ELEMENTS))
    patch_extraction_and_screening(monkeypatch, extraction_calls=extraction_calls, screening_calls=screening_calls)

    exit_code = cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "RECOVERED: 144246" in output
    assert "promoted from INCOMPLETE_CONTENT to COLLECTED" in output

    conn = db.connect(config.database_path)
    promoted = db.get_latest_collected_source(conn, "yellowbrick", "144246")
    versions = db.count_collected_source_versions(conn, "yellowbrick")
    conn.close()

    assert versions == 1  # NO duplicate row
    assert promoted["source_id"] == original["source_id"]  # updated IN PLACE
    assert promoted["collection_status"] == "COLLECTED"
    assert promoted["extraction_status"] == "EXTRACTED"  # extraction became eligible and ran
    assert len(extraction_calls) == 1
    assert len(screening_calls) == 1


def test_promotion_preserves_original_raw_html_path(tmp_path, monkeypatch, capsys):
    """content_hash is unchanged, so the original content-addressed raw
    capture is still correct -- promotion must never touch raw_html_path
    or raw_capture_hash.
    """
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)

    patch_browser(monkeypatch, make_page(COLLAPSED_UNCHECKED))
    fail_llm(monkeypatch)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    capsys.readouterr()

    conn = db.connect(config.database_path)
    original = db.get_latest_collected_source(conn, "yellowbrick", "144246")
    conn.close()
    original_raw_path = original["raw_html_path"]
    original_raw_capture_hash = original["raw_capture_hash"]

    patch_browser(monkeypatch, make_page(COLLAPSED_CHECKED, role_elements=CHECKED_SWITCH_ELEMENTS))
    patch_extraction_and_screening(monkeypatch)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)

    conn = db.connect(config.database_path)
    promoted = db.get_latest_collected_source(conn, "yellowbrick", "144246")
    conn.close()

    assert promoted["raw_html_path"] == original_raw_path
    assert promoted["raw_capture_hash"] == original_raw_capture_hash


# --- CASE C: incomplete -> valid, DIFFERENT content hash ------------------------


def test_incomplete_recovery_with_different_hash_inserts_new_version(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)

    patch_browser(monkeypatch, make_page(COLLAPSED_UNCHECKED))
    fail_llm(monkeypatch)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    capsys.readouterr()

    conn = db.connect(config.database_path)
    original = db.get_latest_collected_source(conn, "yellowbrick", "144246")
    conn.close()

    extraction_calls, screening_calls = [], []
    patch_browser(monkeypatch, make_page(REAL_PITCH_HTML))
    patch_extraction_and_screening(monkeypatch, extraction_calls=extraction_calls, screening_calls=screening_calls)

    exit_code = cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    latest = db.get_latest_collected_source(conn, "yellowbrick", "144246")
    versions = db.count_collected_source_versions(conn, "yellowbrick")
    conn.close()

    assert versions == 2  # a genuine NEW version, not an in-place update
    assert latest["source_id"] != original["source_id"]
    assert latest["previous_version_source_id"] == original["source_id"]
    assert latest["collection_status"] == "COLLECTED"
    assert latest["extraction_status"] == "EXTRACTED"
    assert len(extraction_calls) == 1
    assert len(screening_calls) == 1


# --- CASE D: already-COLLECTED, same hash, unchanged/idempotent ----------------


def test_already_collected_same_hash_remains_unchanged_and_idempotent(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)

    extraction_calls, screening_calls = [], []
    patch_browser(monkeypatch, make_page(REAL_PITCH_HTML))
    patch_extraction_and_screening(monkeypatch, extraction_calls=extraction_calls, screening_calls=screening_calls)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    capsys.readouterr()

    conn = db.connect(config.database_path)
    before = dict(db.get_latest_collected_source(conn, "yellowbrick", "144246"))
    conn.close()

    # A title change forces a refetch (bypassing the pre-fetch
    # looks_unchanged short-circuit) but the fetched content is identical.
    feed_html_retitled = FEED_HTML.replace("Some Co", "Some Co Updated")
    page = FakePage({HOME: feed_html_retitled, FEED: feed_html_retitled, PITCH_URL: REAL_PITCH_HTML})
    patch_browser(monkeypatch, page)
    patch_extraction_and_screening(monkeypatch, extraction_calls=extraction_calls, screening_calls=screening_calls)

    exit_code = cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Already known: 1" in output

    conn = db.connect(config.database_path)
    after = dict(db.get_latest_collected_source(conn, "yellowbrick", "144246"))
    versions = db.count_collected_source_versions(conn, "yellowbrick")
    conn.close()

    assert versions == 1
    assert after == before  # byte-for-byte unchanged
    assert len(extraction_calls) == 1  # no re-extraction triggered
    assert len(screening_calls) == 1


# --- general safety / regression -------------------------------------------------


def test_persistent_invalid_page_causes_zero_llm_calls(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)

    patch_browser(monkeypatch, make_page(NOT_FOUND_HTML))
    fail_llm(monkeypatch)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    capsys.readouterr()

    patch_browser(monkeypatch, make_page(NOT_FOUND_HTML))
    fail_llm(monkeypatch)
    exit_code = cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert exit_code == 0  # fail_llm would have raised if any LLM call happened


def test_recovered_valid_page_extracted_and_screened_exactly_once(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)

    patch_browser(monkeypatch, make_page(NOT_FOUND_HTML))
    fail_llm(monkeypatch)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    capsys.readouterr()

    extraction_calls, screening_calls = [], []
    patch_browser(monkeypatch, make_page(REAL_PITCH_HTML))
    patch_extraction_and_screening(monkeypatch, extraction_calls=extraction_calls, screening_calls=screening_calls)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)

    # A third, immediate rerun must not re-extract/re-screen.
    patch_browser(monkeypatch, make_page(REAL_PITCH_HTML))
    patch_extraction_and_screening(monkeypatch, extraction_calls=extraction_calls, screening_calls=screening_calls)
    cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)

    assert len(extraction_calls) == 1
    assert len(screening_calls) == 1
