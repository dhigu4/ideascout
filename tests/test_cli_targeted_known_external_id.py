"""Targeted collection of known historical external_ids.

Production failure: `collect-source yellowbrick --external-id 144273` reported
NOT_FOUND because the pitch had fallen out of the recent discovery listing,
even though it was already known and had historical versions. A targeted
request for a previously-known document must fetch its exact page directly,
through the same pipeline as normal collection. An unknown id still fails
safely, and a discovered id behaves exactly as before.

Every run here uses a fake browser page whose feed is controlled per run, so
an id can be listed in one run and absent in the next. Extraction, screening,
and the narrative repair are all stubbed; no LLM or network call is made.
"""

from __future__ import annotations

import contextlib
import io

from ideascout import cli, db, shadow
from ideascout.sources import browser as source_browser
from ideascout.sources import yellowbrick
from tests.browser_fakes import FakePage, make_fake_persistent_chrome_context
from tests.test_cli_collect_source_idempotent_retry import (
    FEED_HTML,
    NOT_FOUND_HTML,
    PITCH_URL,
    REAL_PITCH_HTML,
    build_taste_v1,
    fail_llm,
    make_config,
    patch_extraction_and_screening,
    write_screen_rules,
)

EXTERNAL_ID = PITCH_URL.rsplit("/", 1)[-1]
EMPTY_FEED_HTML = "<html><body><p>No recent pitches.</p></body></html>"
REAL_PITCH_VARIANT_HTML = REAL_PITCH_HTML.replace("trades at a discount after", "trades at a deep discount after")


def _page(feed_html: str, pitch_html: str) -> FakePage:
    return FakePage(
        {yellowbrick.HOME_URL: feed_html, yellowbrick.FEED_URL: feed_html, PITCH_URL: pitch_html}
    )


def _run(config, monkeypatch, *, feed_html, pitch_html, external_id=None, dry_run=False):
    fake_cm, _ = make_fake_persistent_chrome_context(_page(feed_html, pitch_html))
    monkeypatch.setattr(source_browser, "persistent_chrome_context", fake_cm)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.cmd_collect_source(config, "yellowbrick", dry_run=dry_run, limit=10, external_id=external_id)
    return code, buf.getvalue()


def _rows(config):
    conn = db.connect(config.database_path)
    rows = [dict(r) for r in conn.execute(
        "SELECT source_id, content_hash, collection_status, raw_html_path, metadata_json, extraction_status "
        "FROM collected_sources WHERE external_id = ? ORDER BY source_id", (EXTERNAL_ID,))]
    current = db.get_latest_collected_source(conn, "yellowbrick", EXTERNAL_ID)
    conn.close()
    return rows, (current["source_id"] if current else None)


def _stub_screening(monkeypatch, *, overall="WATCH", reasons=None, concerns=None, questions=None,
                    extraction_calls=None, screening_calls=None):
    """Extraction + screening + repair, all stubbed. Repair returns nothing so the
    narrative stays blank when a test wants that."""
    from tests.test_cli_shadow import make_extracted_idea

    prediction = shadow.ShadowPrediction(
        overall_prediction=overall, mispricing="Plausible", variant_perception="Plausible",
        upside="Potentially sufficient", business_quality="Plausible", downside="Problematic",
        key_reasons=list(reasons or []), key_concerns=list(concerns or []),
        critical_questions=list(questions or []), confidence="MEDIUM",
    )

    def fake_extract(client, model, source_text, logger=None):
        if extraction_calls is not None:
            extraction_calls.append(source_text)
        return make_extracted_idea()

    def fake_screen(client, model, *, idea_taste_body, screen_rules_body, idea_record, logger=None):
        if screening_calls is not None:
            screening_calls.append(dict(idea_record))
        return prediction

    from ideascout import idea_extraction

    monkeypatch.setattr(idea_extraction, "build_client", lambda api_key: object())
    monkeypatch.setattr(idea_extraction, "extract_idea", fake_extract)
    monkeypatch.setattr(shadow, "build_client", lambda api_key: object())
    monkeypatch.setattr(shadow, "screen_idea", fake_screen)
    monkeypatch.setattr(shadow, "repair_narrative", lambda *a, **k: shadow.NarrativeRepair())


def _audit(config):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.cmd_audit_screening_narratives(config, "yellowbrick")
    return buf.getvalue()


def _digest_ids(config, taste_version):
    conn = db.connect(config.database_path)
    selected, _, narrative_suppressed = cli.select_digest_candidates_detailed(conn, taste_version)
    conn.close()
    return {row["source_id"] for row in selected}, narrative_suppressed


def _seed_a_b_a(config, monkeypatch, *, extraction_calls=None, screening_calls=None):
    """A (valid, discovered) -> B (404, targeted, absent from the feed) ->
    returns after the first two steps; the caller performs the A recovery."""
    _stub_screening(monkeypatch, extraction_calls=extraction_calls, screening_calls=screening_calls)
    code, _ = _run(config, monkeypatch, feed_html=FEED_HTML, pitch_html=REAL_PITCH_HTML)
    assert code == 0
    code, out = _run(config, monkeypatch, feed_html=EMPTY_FEED_HTML, pitch_html=NOT_FOUND_HTML, external_id=EXTERNAL_ID)
    assert code == 0
    assert "DIRECT:" in out
    return _rows(config)


# --- semantics: direct fetch of a known id absent from discovery ----------------------


def test_known_absent_external_id_is_fetched_directly_and_pointer_returns_to_a(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)
    rows, current = _seed_a_b_a(config, monkeypatch)
    a_id, b_id = rows[0]["source_id"], rows[1]["source_id"]
    assert current == b_id  # B is current after the 404 direct fetch

    fail_llm(monkeypatch)
    code, out = _run(config, monkeypatch, feed_html=EMPTY_FEED_HTML, pitch_html=REAL_PITCH_HTML, external_id=EXTERNAL_ID)
    assert code == 0
    assert "DIRECT:" in out

    rows, current = _rows(config)
    assert len(rows) == 2  # no duplicate A row
    assert current == a_id  # the recovered A is current again
    assert rows[1]["collection_status"] == "INCOMPLETE_CONTENT"  # B preserved historically
    assert rows[1]["raw_html_path"]


def test_persistent_404_targeted_fetch_leaves_b_current(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)
    _stub_screening(monkeypatch)
    _run(config, monkeypatch, feed_html=FEED_HTML, pitch_html=REAL_PITCH_HTML)  # A

    fail_llm(monkeypatch)
    code, _ = _run(config, monkeypatch, feed_html=EMPTY_FEED_HTML, pitch_html=NOT_FOUND_HTML, external_id=EXTERNAL_ID)
    assert code == 0
    rows, current = _rows(config)
    assert len(rows) == 2
    assert current == rows[1]["source_id"]
    assert rows[1]["collection_status"] == "INCOMPLETE_CONTENT"

    # A second persistent 404 is idempotent: still two rows, B still current.
    code, _ = _run(config, monkeypatch, feed_html=EMPTY_FEED_HTML, pitch_html=NOT_FOUND_HTML, external_id=EXTERNAL_ID)
    assert code == 0
    rows, current = _rows(config)
    assert len(rows) == 2
    assert current == rows[1]["source_id"]


def test_changed_valid_content_inserts_new_version_and_becomes_current(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)
    _stub_screening(monkeypatch)
    _run(config, monkeypatch, feed_html=FEED_HTML, pitch_html=REAL_PITCH_HTML)  # A

    extraction_calls, screening_calls = [], []
    _stub_screening(monkeypatch, extraction_calls=extraction_calls, screening_calls=screening_calls)
    code, _ = _run(config, monkeypatch, feed_html=EMPTY_FEED_HTML, pitch_html=REAL_PITCH_VARIANT_HTML, external_id=EXTERNAL_ID)
    assert code == 0

    rows, current = _rows(config)
    assert len(rows) == 2
    assert current == rows[1]["source_id"]  # the new valid version is current
    assert len(extraction_calls) == 1  # extracted once
    assert len(screening_calls) == 1  # screened once


def test_unknown_external_id_not_discovered_fails_safely(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)
    fail_llm(monkeypatch)

    code, out = _run(config, monkeypatch, feed_html=EMPTY_FEED_HTML, pitch_html=REAL_PITCH_HTML, external_id="999999")
    assert code == 1
    assert "NOT_FOUND" in out
    assert "not a previously-known document" in out
    conn = db.connect(config.database_path)
    assert conn.execute("SELECT COUNT(*) FROM collected_sources").fetchone()[0] == 0
    conn.close()


def test_adapter_without_direct_fetch_capability_fails_safely(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)
    _stub_screening(monkeypatch)
    _run(config, monkeypatch, feed_html=FEED_HTML, pitch_html=REAL_PITCH_HTML)  # known A

    monkeypatch.delattr(yellowbrick, "direct_discovered_item")
    fail_llm(monkeypatch)
    code, out = _run(config, monkeypatch, feed_html=EMPTY_FEED_HTML, pitch_html=REAL_PITCH_HTML, external_id=EXTERNAL_ID)
    assert code == 1
    assert "NOT_FOUND" in out
    assert "DIRECT" not in out


# --- unchanged behavior --------------------------------------------------------------


def test_normal_non_targeted_discovery_is_unchanged(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)
    _stub_screening(monkeypatch)
    code, out = _run(config, monkeypatch, feed_html=FEED_HTML, pitch_html=REAL_PITCH_HTML)
    assert code == 0
    assert "Discovered: 1" in out
    assert "DIRECT" not in out


def test_targeted_discovered_id_behaves_exactly_as_before(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)
    _stub_screening(monkeypatch)
    code, out = _run(config, monkeypatch, feed_html=FEED_HTML, pitch_html=REAL_PITCH_HTML, external_id=EXTERNAL_ID)
    assert code == 0
    assert "DIRECT" not in out  # discovered normally, so no direct fallback
    assert "Discovered: 1" in out


def test_unchanged_recovered_a_makes_no_llm_calls(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    build_taste_v1(config, monkeypatch)
    rows, _ = _seed_a_b_a(config, monkeypatch)
    a_id = rows[0]["source_id"]

    fail_llm(monkeypatch)
    code, _ = _run(config, monkeypatch, feed_html=EMPTY_FEED_HTML, pitch_html=REAL_PITCH_HTML, external_id=EXTERNAL_ID)
    assert code == 0
    assert _rows(config)[1] == a_id  # recovered with zero LLM calls


# --- effect on screening outputs ----------------------------------------------------------


def test_recovered_blank_watch_is_flagged_by_narrative_audit_and_suppressed_from_digest(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    write_screen_rules(config)
    latest = build_taste_v1(config, monkeypatch)
    # A's original WATCH screening has no narrative at all.
    _stub_screening(monkeypatch, overall="WATCH", reasons=[], concerns=[], questions=[])
    _run(config, monkeypatch, feed_html=FEED_HTML, pitch_html=REAL_PITCH_HTML)  # A, blank WATCH
    _run(config, monkeypatch, feed_html=EMPTY_FEED_HTML, pitch_html=NOT_FOUND_HTML, external_id=EXTERNAL_ID)  # B current

    assert f"external_id={EXTERNAL_ID}" not in _audit(config)  # suppressed while B is current

    fail_llm(monkeypatch)
    _run(config, monkeypatch, feed_html=EMPTY_FEED_HTML, pitch_html=REAL_PITCH_HTML, external_id=EXTERNAL_ID)  # A current again
    assert f"external_id={EXTERNAL_ID}" in _audit(config)  # now visible to the audit

    ids, narrative_suppressed = _digest_ids(config, latest["version_number"])
    assert narrative_suppressed == 1
    rows, current = _rows(config)
    assert current not in ids  # digest still suppresses the incomplete WATCH
