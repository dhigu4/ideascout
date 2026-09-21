"""Fake Playwright page/context objects, shared by tests that exercise
website-source adapters and cli.py's collection commands WITHOUT ever
launching a real browser or touching a live website.
"""

from __future__ import annotations

from contextlib import contextmanager


class FakePage:
    def __init__(self, html_by_url: dict[str, str], default_html: str = ""):
        self.html_by_url = html_by_url
        self.default_html = default_html
        self.urls_visited: list[str] = []
        self._current_url = None
        self.wait_for_selector_calls: list[tuple] = []
        # Set True in a test to simulate the selector never appearing
        # (Playwright's real wait_for_selector raises TimeoutError) --
        # default False means "found immediately", so existing tests that
        # don't care about this need no changes.
        self.raise_on_wait = False

    def goto(self, url, wait_until=None):
        self.urls_visited.append(url)
        self._current_url = url

    @property
    def url(self) -> str | None:
        return self._current_url

    def content(self) -> str:
        return self.html_by_url.get(self._current_url, self.default_html)

    def wait_for_selector(self, selector, timeout=None):
        self.wait_for_selector_calls.append((selector, timeout))
        if self.raise_on_wait:
            raise TimeoutError(f"fake: selector {selector!r} did not appear")


class FakeContext:
    def __init__(self, page: FakePage):
        self._page = page
        self.closed = False
        self.new_page_calls = 0

    def new_page(self):
        self.new_page_calls += 1
        return self._page

    def close(self):
        self.closed = True


def make_fake_persistent_chrome_context(page: FakePage):
    """Returns (fake_context_manager_fn, context) -- monkeypatch
    ideascout.sources.browser.persistent_chrome_context (as imported into
    cli.py as source_browser) with the returned function, then inspect
    `context` afterward (e.g. context.closed, context.new_page_calls).
    """
    context = FakeContext(page)

    @contextmanager
    def _fake(source_name, browser_profiles_dir, *, headless):
        try:
            yield context
        finally:
            context.close()

    return _fake, context
