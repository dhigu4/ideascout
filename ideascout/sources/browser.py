"""Playwright browser/session management for authenticated website-source
adapters.

Uses a DEDICATED persistent Chrome profile per source -- never Brad's
normal Chrome profile -- so a manual login survives between runs entirely
inside Chrome's own profile storage. This module never reads, parses,
copies, or logs anything from that profile directory; it only computes a
path to it and asks Playwright to launch Chrome against it. No website
credentials or cookies are ever handled by, or visible to, IdeaScout code.

browser_profiles_dir is always passed in explicitly by the caller (from
Config.browser_profiles_dir) -- this module never computes its own
default, matching the rest of the app's rule that only ideascout/config.py
ever decides what the real production paths are.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path


def profile_dir(source_name: str, browser_profiles_dir: Path) -> Path:
    return browser_profiles_dir / source_name


@contextmanager
def persistent_chrome_context(source_name: str, browser_profiles_dir: Path, *, headless: bool):
    """Launch (or resume) a persistent Chrome profile dedicated to one
    source. Yields a Playwright BrowserContext; closes it (and the
    underlying Playwright process) cleanly on exit either way. The profile
    directory itself is never deleted -- closing the context just ends
    this run's browser process, exactly like closing a normal Chrome
    window, so the next run resumes the same authenticated session.
    """
    from playwright.sync_api import sync_playwright

    profile = profile_dir(source_name, browser_profiles_dir)
    profile.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            channel="chrome",
            headless=headless,
        )
        try:
            yield context
        finally:
            context.close()
