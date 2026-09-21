"""Fake Playwright page/context objects, shared by tests that exercise
website-source adapters and cli.py's collection commands WITHOUT ever
launching a real browser or touching a live website.
"""

from __future__ import annotations

from contextlib import contextmanager

_ARIA_ATTR_KEY_MAP = {
    "aria-expanded": "aria_expanded",
    "aria-controls": "aria_controls",
    "aria-checked": "aria_checked",
    "data-state": "data_state",
}


class FakePage:
    def __init__(
        self,
        html_by_url: dict[str, str],
        default_html: str = "",
        expanded_html_by_url: dict[str, str] | None = None,
        show_full_summary_elements: list[dict] | None = None,
        role_elements: dict[str, list[dict]] | None = None,
    ):
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

        # --- click-to-expand simulation (e.g. Yellowbrick's "Show full
        # summary" control) -- see get_by_text/_FakeLocator.click below.
        # expanded_html_by_url is the HTML content() should return for a
        # given URL once its control has been clicked AND wait_for_timeout
        # has subsequently been called (modeling a real async DOM update
        # that isn't visible until something gives it a moment to render).
        self.expanded_html_by_url: dict[str, str] = expanded_html_by_url or {}
        self._expanded_urls: set[str] = set()
        self._pending_expand_urls: set[str] = set()
        self.click_calls: list[str] = []
        self.wait_for_timeout_calls: list[int] = []
        # Set True in a test to simulate the control existing but not
        # actually being clickable (Playwright's real .click() would
        # raise a TimeoutError if the element never becomes actionable).
        self.raise_on_click = False

        # --- live-diagnostic support (Stage 5.9) ---------------------------
        # Per-candidate element metadata for whatever get_by_text() is
        # asked to find (e.g. tag/visible/enabled/role/href/aria-*/type/
        # outer_html/bounding_box), keyed by list position -- .nth(i) reads
        # entry i. None (the default) means "one implicit, fully-actionable
        # match", which is enough for every pre-Stage-5.9 test that never
        # calls .count()/.nth()/attribute getters at all.
        self.show_full_summary_elements: list[dict] | None = show_full_summary_elements

        # --- role-based locator simulation (Stage 5.11: page.get_by_role,
        # e.g. the real Yellowbrick full-summary switch) -- keyed by role
        # string ("switch"), each a list of per-element metadata (visible/
        # enabled/id/aria_checked/data_state), read by .nth(i). None (the
        # default, or a role with no entry) means zero matches -- unlike
        # show_full_summary_elements, there is no implicit single match,
        # since "no switch present at all" is a common, legitimate real
        # page state (task section 2's fallback), not an edge case.
        self.role_elements: dict[str, list[dict]] = role_elements or {}
        self._listeners: dict[str, list] = {}

    def goto(self, url, wait_until=None):
        self.urls_visited.append(url)
        self._current_url = url

    @property
    def url(self) -> str | None:
        return self._current_url

    def content(self) -> str:
        if self._current_url in self._expanded_urls and self._current_url in self.expanded_html_by_url:
            return self.expanded_html_by_url[self._current_url]
        return self.html_by_url.get(self._current_url, self.default_html)

    def title(self) -> str:
        return ""

    def wait_for_selector(self, selector, timeout=None):
        self.wait_for_selector_calls.append((selector, timeout))
        if self.raise_on_wait:
            raise TimeoutError(f"fake: selector {selector!r} did not appear")

    def get_by_text(self, text, exact=False):
        return _FakeLocator(self, text)

    def get_by_role(self, role, **kwargs):
        return _FakeRoleLocator(self, role)

    def wait_for_timeout(self, ms):
        self.wait_for_timeout_calls.append(ms)
        # Applies any click's pending expansion -- content() only reflects
        # it from this point on, modeling a real (short) async render delay.
        self._expanded_urls |= self._pending_expand_urls
        self._pending_expand_urls.clear()

    # --- event listeners (Stage 5.9: request capture during a click) ------

    def on(self, event, handler):
        self._listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        handlers = self._listeners.get(event)
        if handlers and handler in handlers:
            handlers.remove(handler)

    def simulate_request(self, method: str, url: str):
        """Test-only: fires a fake Playwright 'request' event so tests can
        verify sanitized request-capture/reporting logic without any real
        network I/O -- never a claim about what the real page actually
        requests.
        """
        request = _FakeRequest(method, url)
        for handler in list(self._listeners.get("request", [])):
            handler(request)


class _FakeRequest:
    def __init__(self, method: str, url: str):
        self.method = method
        self.url = url


class _FakeLocator:
    """Stands in for a Playwright Locator returned by page.get_by_text()."""

    def __init__(self, page: FakePage, text: str, index: int | None = None):
        self._page = page
        self._text = text
        self._index = index

    def _element_config(self) -> dict:
        elements = self._page.show_full_summary_elements
        if elements is None or self._index is None:
            return {}
        if 0 <= self._index < len(elements):
            return elements[self._index]
        return {}

    def count(self) -> int:
        elements = self._page.show_full_summary_elements
        return len(elements) if elements is not None else 1

    def nth(self, index: int) -> "_FakeLocator":
        return _FakeLocator(self._page, self._text, index=index)

    def is_visible(self) -> bool:
        return self._element_config().get("visible", True)

    def is_enabled(self) -> bool:
        return self._element_config().get("enabled", True)

    def get_attribute(self, name: str):
        return self._element_config().get(_ARIA_ATTR_KEY_MAP.get(name, name))

    def evaluate(self, script: str, arg=None):
        config = self._element_config()
        # Order matters: _DOM_CONTEXT_SCRIPT and _RELATED_CLICK_SCRIPT both
        # happen to contain the substrings "outerHTML"/"tagName" too, so
        # their own distinctive markers must be checked first.
        if "getBoundingClientRect" in script:
            return config.get("dom_context") or {
                "ancestors": [],
                "previous_sibling": None,
                "next_sibling": None,
                "container_outer_html": None,
            }
        if "resolveRelatedNode" in script:
            self._page.click_calls.append(f"{self._text}::{arg}")
            if self._page.raise_on_click:
                raise TimeoutError(f"fake: related node {arg!r} did not become clickable")
            self._page._pending_expand_urls.add(self._page._current_url)
            return {"clicked": True}
        if "outerHTML" in script:
            return config.get("outer_html")
        if "tagName" in script:
            return config.get("tag_name")
        return None

    def bounding_box(self):
        return self._element_config().get("bounding_box")

    def click(self, timeout=None):
        self._page.click_calls.append(self._text)
        if self._page.raise_on_click:
            raise TimeoutError(f"fake: {self._text!r} control did not become clickable")
        self._page._pending_expand_urls.add(self._page._current_url)


class _FakeRoleLocator:
    """Stands in for a Playwright Locator returned by page.get_by_role()
    (Stage 5.11 -- the real Yellowbrick full-summary switch is located
    this way, via role="switch"). Reuses the SAME click-to-expand
    simulation as _FakeLocator.click (raise_on_click / pending-expand-url
    mechanics), so tests configure expansion outcomes identically
    regardless of which locator strategy production code used to click.
    """

    def __init__(self, page: FakePage, role: str, index: int | None = None):
        self._page = page
        self._role = role
        self._index = index

    def _element_config(self) -> dict:
        elements = self._page.role_elements.get(self._role)
        if elements is None or self._index is None:
            return {}
        if 0 <= self._index < len(elements):
            return elements[self._index]
        return {}

    def count(self) -> int:
        elements = self._page.role_elements.get(self._role)
        return len(elements) if elements is not None else 0

    def nth(self, index: int) -> "_FakeRoleLocator":
        return _FakeRoleLocator(self._page, self._role, index=index)

    def is_visible(self) -> bool:
        return self._element_config().get("visible", True)

    def is_enabled(self) -> bool:
        return self._element_config().get("enabled", True)

    def get_attribute(self, name: str):
        return self._element_config().get(_ARIA_ATTR_KEY_MAP.get(name, name))

    def click(self, timeout=None):
        self._page.click_calls.append(f"role={self._role}[{self._index}]")
        if self._page.raise_on_click:
            raise TimeoutError(f"fake: role={self._role!r} switch did not become clickable")
        self._page._pending_expand_urls.add(self._page._current_url)


class FakeContext:
    def __init__(self, page: FakePage):
        self._page = page
        self.closed = False
        self.new_page_calls = 0
        self._listeners: dict[str, list] = {}

    def new_page(self):
        self.new_page_calls += 1
        return self._page

    def close(self):
        self.closed = True

    # --- event listeners (Stage 5.9: popup/new-page detection) ------------

    def on(self, event, handler):
        self._listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        handlers = self._listeners.get(event)
        if handlers and handler in handlers:
            handlers.remove(handler)

    def simulate_popup(self, url: str, title: str) -> "_FakePopupPage":
        """Test-only: fires a fake Playwright 'page' (popup) event -- never
        a claim about whether the real page actually opens one.
        """
        popup = _FakePopupPage(url, title)
        for handler in list(self._listeners.get("page", [])):
            handler(popup)
        return popup


class _FakePopupPage:
    def __init__(self, url: str, title: str):
        self.url = url
        self._title = title

    def title(self) -> str:
        return self._title


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
