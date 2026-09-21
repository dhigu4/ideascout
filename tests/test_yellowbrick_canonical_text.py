"""Unit tests for ideascout/sources/yellowbrick.py's build_canonical_source_text
and the two-hash split (content_hash vs raw_capture_hash) -- Stage 5.4 fix.

A real production immediate-refetch of an unchanged pitch was wrongly
treated as a changed version because content_hash was computed over the
full raw page HTML, which is never byte-identical between two fetches
(volatile scripts, session/hydration state, timestamps). These tests pin
down that content_hash is now driven by normalized, substantive-only text
only, while raw_capture_hash (kept separately, for provenance) is allowed
to differ freely.

Pure parsing only -- no Playwright, no network, no live Yellowbrick site.
"""

from __future__ import annotations

from ideascout.sources import base, yellowbrick

DISCOVERED = base.DiscoveredItem(external_id="1", canonical_url="https://www.joinyellowbrick.com/sp/1")


def _parse(html: str):
    return yellowbrick.parse_pitch_page(html, DISCOVERED, discovered_at="2026-01-01T00:00:00+00:00")


BASE_PITCH_HTML = """
<html><head><title>XYZ Corp: Hidden Value Play</title></head>
<body>
<script>window.__NEXT_DATA__ = {"session": "abc123", "csrfToken": "t0"};</script>
<nav>Home | Recent | Logged in as brad@example.com</nav>
<article class="pitch-card">
  <time datetime="2026-09-05">September 5, 2026</time>
  <h1>XYZ Corp: Hidden Value Play</h1>
  <p>XYZ Corp trades at 5x normalized earnings due to a temporary
  loss-making segment expected to reach breakeven next year.</p>
  <a href="/sp/1">Share this pitch</a>
  <a href="/sp/1/report">Report error for this pitch</a>
</article>
<footer>Request-ID: req-1111, rendered at 2026-09-17T10:00:00Z</footer>
</body></html>
"""


def _with_volatile_noise(html: str, *, session="zzz999", request_id="req-9999") -> str:
    return (
        html.replace("abc123", session)
        .replace("t0", "t" + session)
        .replace("req-1111", request_id)
        .replace("2026-09-17T10:00:00Z", "2026-09-17T10:00:07Z")
    )


# --- the actual bug: volatile page differences must NOT create a new version --


def test_identical_substantive_text_different_raw_html_is_not_a_change():
    html_a = BASE_PITCH_HTML
    html_b = _with_volatile_noise(BASE_PITCH_HTML)

    item_a = _parse(html_a)
    item_b = _parse(html_b)

    assert item_a.raw_html != item_b.raw_html
    assert item_a.content_hash == item_b.content_hash


def test_dynamic_script_and_session_differences_are_not_a_change():
    html_a = BASE_PITCH_HTML
    html_b = BASE_PITCH_HTML.replace("abc123", "totally-different-session-token")

    assert _parse(html_a).content_hash == _parse(html_b).content_hash


def test_nav_login_account_differences_are_not_a_change():
    html_a = BASE_PITCH_HTML
    html_b = BASE_PITCH_HTML.replace("Logged in as brad@example.com", "Logged in as someone.else@example.com")

    assert _parse(html_a).content_hash == _parse(html_b).content_hash


def test_footer_request_id_and_render_timestamp_differences_are_not_a_change():
    html_a = BASE_PITCH_HTML
    html_b = BASE_PITCH_HTML.replace("req-1111", "req-2222").replace("2026-09-17T10:00:00Z", "2026-09-17T10:05:33Z")

    assert _parse(html_a).content_hash == _parse(html_b).content_hash


def test_ui_share_and_report_button_presence_is_not_a_change():
    html_with_buttons = BASE_PITCH_HTML
    html_without_buttons = BASE_PITCH_HTML.replace(
        '<a href="/sp/1">Share this pitch</a>\n  <a href="/sp/1/report">Report error for this pitch</a>', ""
    )

    assert _parse(html_with_buttons).content_hash == _parse(html_without_buttons).content_hash


def test_whitespace_only_differences_are_not_a_change():
    html_a = BASE_PITCH_HTML
    html_b = BASE_PITCH_HTML.replace(
        "XYZ Corp trades at 5x normalized earnings",
        "XYZ   Corp trades   at 5x normalized earnings  ",
    )

    assert _parse(html_a).content_hash == _parse(html_b).content_hash


def test_line_ending_differences_are_not_a_change():
    html_a = BASE_PITCH_HTML
    html_b = BASE_PITCH_HTML.replace("\n", "\r\n")

    assert _parse(html_a).content_hash == _parse(html_b).content_hash


# --- a real change must still be detected -------------------------------------


def test_real_thesis_text_change_is_a_change():
    html_a = BASE_PITCH_HTML
    html_b = BASE_PITCH_HTML.replace(
        "XYZ Corp trades at 5x normalized earnings due to a temporary",
        "XYZ Corp now trades at 2x normalized earnings after a guidance cut due to a temporary",
    )

    assert _parse(html_a).content_hash != _parse(html_b).content_hash


def test_title_change_is_a_change():
    html_a = BASE_PITCH_HTML
    html_b = BASE_PITCH_HTML.replace(
        "<h1>XYZ Corp: Hidden Value Play</h1>", "<h1>XYZ Corp: Revised Outlook</h1>"
    )

    assert _parse(html_a).content_hash != _parse(html_b).content_hash


# --- raw_capture_hash: separate, allowed to differ freely ---------------------


def test_raw_capture_hash_differs_when_raw_bytes_differ_even_if_content_hash_is_equal():
    html_a = BASE_PITCH_HTML
    html_b = _with_volatile_noise(BASE_PITCH_HTML)

    item_a = _parse(html_a)
    item_b = _parse(html_b)

    assert item_a.raw_capture_hash != item_b.raw_capture_hash
    assert item_a.content_hash == item_b.content_hash


def test_raw_capture_hash_is_exact_sha256_of_raw_html_bytes():
    import hashlib

    item = _parse(BASE_PITCH_HTML)
    assert item.raw_capture_hash == hashlib.sha256(BASE_PITCH_HTML.encode("utf-8")).hexdigest()


def test_content_hash_is_not_the_raw_html_hash():
    item = _parse(BASE_PITCH_HTML)
    assert item.content_hash != item.raw_capture_hash


# --- build_canonical_source_text directly --------------------------------------


def test_canonical_source_text_excludes_scripts_and_nav_and_footer():
    text = yellowbrick.build_canonical_source_text(BASE_PITCH_HTML)
    assert "session" not in text
    assert "csrfToken" not in text
    assert "Logged in as" not in text
    assert "Request-ID" not in text
    assert "rendered at" not in text


def test_canonical_source_text_excludes_known_action_phrases():
    text = yellowbrick.build_canonical_source_text(BASE_PITCH_HTML)
    assert "Share this pitch" not in text
    assert "Report error for this pitch" not in text


def test_canonical_source_text_preserves_substantive_content():
    text = yellowbrick.build_canonical_source_text(BASE_PITCH_HTML)
    assert "XYZ Corp: Hidden Value Play" in text
    assert "5x normalized earnings" in text
    assert "September 5, 2026" in text


def test_canonical_source_text_does_not_lowercase():
    text = yellowbrick.build_canonical_source_text(BASE_PITCH_HTML)
    assert "XYZ Corp" in text
    assert "xyz corp" not in text


def test_canonical_source_text_preserves_numbers_and_punctuation():
    text = yellowbrick.build_canonical_source_text(BASE_PITCH_HTML)
    assert "5x" in text
    assert "September 5, 2026" in text  # the <time> tag's own visible text, numbers and comma intact


def test_canonical_source_text_is_deterministic():
    assert yellowbrick.build_canonical_source_text(BASE_PITCH_HTML) == yellowbrick.build_canonical_source_text(
        BASE_PITCH_HTML
    )
