"""Tests for `python run.py diagnose-source-live yellowbrick --external-id <id>`
-- the READ-ONLY live-browser diagnostic added in Stage 5.9/5.10.

Never touches the database (there is no db.open_production_database call
anywhere in cmd_diagnose_source_live), never writes a raw artifact, never
calls extraction/screening/any LLM client -- see
ideascout/sources/yellowbrick.py's diagnose_live for the pure diagnostic
logic these tests build on. Everything here uses tests/browser_fakes.py's
FakePage/FakeContext; no real Playwright, no live Yellowbrick site.
"""

from __future__ import annotations

from ideascout import cli, db, idea_extraction, shadow
from ideascout.sources import yellowbrick
from tests.browser_fakes import FakePage
from tests.test_cli_collect_source import (
    ABC_PITCH_URL,
    DEF_PITCH_URL,
    FEED_HTML,
    PITCH_ABC_HTML,
    PITCH_DEF_HTML,
    make_config,
    patch_browser,
)

ONE_VISIBLE_ELEMENT = [
    {
        "tag_name": "a",
        "visible": True,
        "enabled": True,
        "role": None,
        "href": "#",
        "aria_expanded": "false",
        "aria_controls": None,
        "type": None,
        "outer_html": '<a href="#">Show full summary:</a>',
        "bounding_box": {"x": 0, "y": 0, "width": 100, "height": 20},
        # No dom_context configured -- Stage 5.10: the text match itself is
        # never assumed clickable, so with no ancestor/sibling info at all
        # these tests exercise the "no actionable ancestor found" path.
        "dom_context": None,
    }
]

# A span whose immediate parent IS a real <button> -- lets CLI-level tests
# exercise the full ancestor-click-and-observe path, not just "unresolved".
BUTTON_ANCESTOR_ELEMENT = [
    {
        "tag_name": "span",
        "visible": True,
        "enabled": True,
        "role": None,
        "href": None,
        "aria_expanded": None,
        "aria_controls": None,
        "type": None,
        "outer_html": '<span class="mr-2 text-xs">Show full summary:</span>',
        "bounding_box": {"x": 0, "y": 0, "width": 100, "height": 20},
        "dom_context": {
            "ancestors": [
                {
                    "tag": "button",
                    "class_name": "expand-btn",
                    "role": None,
                    "href": None,
                    "tabindex": None,
                    "aria_expanded": "false",
                    "aria_controls": None,
                    "has_onclick": False,
                    "cursor": "pointer",
                    "visible": True,
                    "enabled": True,
                    "bounding_box": {"x": 0, "y": 0, "width": 120, "height": 30},
                    "outer_html_start": '<button class="expand-btn">',
                    "text_excerpt": "Show full summary",
                }
            ],
            "previous_sibling": None,
            "next_sibling": None,
            "container_outer_html": '<div class="card">...</div>',
        },
    }
]


def make_live_page(*, feed_html=FEED_HTML, abc_html=PITCH_ABC_HTML, def_html=PITCH_DEF_HTML, elements=None) -> FakePage:
    return FakePage(
        {
            yellowbrick.HOME_URL: feed_html,
            yellowbrick.FEED_URL: feed_html,
            ABC_PITCH_URL: abc_html,
            DEF_PITCH_URL: def_html,
        },
        show_full_summary_elements=elements,
    )


def _fail_if_llm_called(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("diagnose-source-live must never build an LLM client")

    monkeypatch.setattr(idea_extraction, "build_client", fail)
    monkeypatch.setattr(shadow, "build_client", fail)


def test_diagnose_live_unknown_source(tmp_path, capsys):
    config = make_config(tmp_path)
    exit_code = cli.cmd_diagnose_source_live(config, "not-a-real-source", "1")
    assert exit_code == 2
    assert "Unknown source" in capsys.readouterr().out


def test_diagnose_live_not_found_makes_no_db_or_llm_calls(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)  # make_config already creates an empty schema
    db_mtime_before = config.database_path.stat().st_mtime
    patch_browser(monkeypatch, make_live_page())
    _fail_if_llm_called(monkeypatch)

    exit_code = cli.cmd_diagnose_source_live(config, "yellowbrick", "000000")
    assert exit_code == 1
    output = capsys.readouterr().out
    assert "NOT_FOUND" in output
    assert "000000" in output

    assert config.database_path.stat().st_mtime == db_mtime_before  # untouched


def test_diagnose_live_auth_required_during_discovery(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    db_mtime_before = config.database_path.stat().st_mtime
    login_html = '<html><body><input type="password"></body></html>'
    patch_browser(monkeypatch, make_live_page(feed_html=login_html))
    _fail_if_llm_called(monkeypatch)

    exit_code = cli.cmd_diagnose_source_live(config, "yellowbrick", "139483")
    assert exit_code == 1
    assert "AUTH_REQUIRED" in capsys.readouterr().out
    assert config.database_path.stat().st_mtime == db_mtime_before


def test_diagnose_live_selects_exact_document_and_skips_others(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    page = make_live_page(elements=ONE_VISIBLE_ELEMENT)
    patch_browser(monkeypatch, page)
    _fail_if_llm_called(monkeypatch)

    exit_code = cli.cmd_diagnose_source_live(config, "yellowbrick", "139483")
    assert exit_code == 0
    output = capsys.readouterr().out

    assert "external_id='139483'" in output
    assert ABC_PITCH_URL in page.urls_visited
    assert DEF_PITCH_URL not in page.urls_visited  # the other document was never touched


def test_diagnose_live_reports_candidates_metrics_and_hidden_data(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    padding = "x" * yellowbrick._LARGE_SCRIPT_CHAR_THRESHOLD
    abc_html_with_hydration = PITCH_ABC_HTML.replace(
        "<body>",
        f'<body><script id="__NEXT_DATA__" type="application/json">{{"ticker":"XYZ","pad":"{padding}"}}</script>',
    )
    page = make_live_page(abc_html=abc_html_with_hydration, elements=ONE_VISIBLE_ELEMENT)
    patch_browser(monkeypatch, page)
    _fail_if_llm_called(monkeypatch)

    exit_code = cli.cmd_diagnose_source_live(config, "yellowbrick", "139483")
    assert exit_code == 0
    output = capsys.readouterr().out

    assert "'Show full summary' text matches found: 1" in output
    assert "BEFORE:" in output
    assert "canonical_text_chars:" in output
    assert "__NEXT_DATA__" in output
    assert "aria-expanded" in output


def test_diagnose_live_makes_no_database_writes_no_raw_files_no_llm_calls(tmp_path, monkeypatch):
    config = make_config(tmp_path)  # make_config already creates an empty schema
    db_mtime_before = config.database_path.stat().st_mtime
    page = make_live_page(elements=ONE_VISIBLE_ELEMENT)
    patch_browser(monkeypatch, page)
    _fail_if_llm_called(monkeypatch)

    cli.cmd_diagnose_source_live(config, "yellowbrick", "139483")

    assert config.database_path.stat().st_mtime == db_mtime_before  # untouched
    # raw_storage_dir is never created/written to by the live diagnostic.
    assert not config.raw_storage_dir.exists() or not any(config.raw_storage_dir.glob("**/*"))


def test_diagnose_live_does_not_click_when_no_actionable_ancestor_found(tmp_path, monkeypatch, capsys):
    """Stage 5.10: even a visible+enabled text match must not be clicked
    directly -- with no button/a/[role=button]/[tabindex]/onclick/
    cursor:pointer anywhere in its ancestors or siblings, the diagnostic
    must report this as unresolved rather than guessing.
    """
    config = make_config(tmp_path)
    page = make_live_page(elements=ONE_VISIBLE_ELEMENT)
    patch_browser(monkeypatch, page)
    _fail_if_llm_called(monkeypatch)

    exit_code = cli.cmd_diagnose_source_live(config, "yellowbrick", "139483")
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "No click attempted" in output
    assert "no actionable ancestor" in output
    assert page.click_calls == []


def test_diagnose_live_full_flow_clicks_resolved_button_ancestor_and_reports_change(tmp_path, monkeypatch, capsys):
    """End-to-end CLI test of the Stage 5.10 fix: a span whose parent is a
    real <button> gets that button clicked (never the span itself), and
    the resulting expansion is detected and reported.
    """
    config = make_config(tmp_path)
    expanded_abc_html = PITCH_ABC_HTML.replace(
        "<p>XYZ Corp trades at 5x normalized earnings due to a temporary loss-making segment.</p>",
        "<p>Full thesis: valuation, catalysts, and risks discussed at much greater length here.</p>",
    )
    page = make_live_page(elements=BUTTON_ANCESTOR_ELEMENT)
    page.expanded_html_by_url[ABC_PITCH_URL] = expanded_abc_html
    patch_browser(monkeypatch, page)
    _fail_if_llm_called(monkeypatch)

    exit_code = cli.cmd_diagnose_source_live(config, "yellowbrick", "139483")
    assert exit_code == 0
    output = capsys.readouterr().out

    assert "Actionable target resolved: relation='ancestor[0]'" in output
    assert "Meaningful change observed" in output
    assert "True" in output
    assert all("::" in call for call in page.click_calls)  # never a bare span click


def test_diagnose_live_does_not_affect_collect_source_state(tmp_path, monkeypatch):
    """Running the live diagnostic must have zero effect on subsequent
    normal collection -- they are completely separate code paths.
    """
    from tests.test_cli_collect_source import patch_extraction_and_screening

    config = make_config(tmp_path)
    page = make_live_page(elements=ONE_VISIBLE_ELEMENT)
    patch_browser(monkeypatch, page)
    _fail_if_llm_called(monkeypatch)
    cli.cmd_diagnose_source_live(config, "yellowbrick", "139483")

    patch_browser(monkeypatch, make_live_page())
    patch_extraction_and_screening(monkeypatch)
    exit_code = cli.cmd_collect_source(config, "yellowbrick", dry_run=False, limit=10)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    assert db.count_collected_sources(conn, "yellowbrick") == 2
    conn.close()
