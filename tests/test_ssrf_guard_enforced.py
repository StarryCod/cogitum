"""F39 enforcement: when the browser is in ``ssrf_guard_partial`` state,
every ``browser(...)`` action except ``close`` must short-circuit with an
``ERROR:`` so popups can't route to private/loopback IPs.

The previous code SET the flag on guard-install failure but no production
code READ it — popups would then route without protection. This pins the
new gating contract.
"""
from __future__ import annotations

import asyncio

import pytest


def _bt():
    import cogitum.core.builtin_tools as bt
    return bt


@pytest.fixture(autouse=True)
def _reset_browser_state():
    """Each test starts with a clean module-level state and restores it."""
    bt = _bt()
    saved = bt._BROWSER_STATE
    bt._BROWSER_STATE = {
        "browser": None,
        "page": None,
        "context": None,
        "_pw": None,
    }
    yield
    bt._BROWSER_STATE = saved


# ---------------------------------------------------------------------------
# Gating contract
# ---------------------------------------------------------------------------


def _set_partial(bt) -> None:
    bt._BROWSER_STATE["ssrf_guard_partial"] = True


@pytest.mark.parametrize(
    "action,kwargs",
    [
        ("open", {"url": "https://example.com/"}),
        ("click", {"selector": "a"}),
        ("type", {"selector": "input", "text": "x"}),
        ("text", {}),
        ("extract", {"selector": "body"}),
        ("links", {}),
        ("act", {"text": "document.title"}),
        ("screenshot", {}),
        ("scroll", {}),
        ("back", {}),
        ("forward", {}),
        ("reload", {}),
        ("title", {}),
        ("url", {}),
    ],
)
def test_partial_state_blocks_all_actions_except_close(action, kwargs):
    bt = _bt()
    _set_partial(bt)

    result = asyncio.run(bt.browser(action=action, **kwargs))

    assert isinstance(result, str)
    assert result.startswith("ERROR:"), (action, result)
    assert "partial" in result.lower()
    assert "close" in result.lower()


def test_partial_state_close_still_works():
    """``close`` is the recovery path — it MUST be reachable even in the
    partial-guard state. Otherwise an operator can never recover without
    process restart."""
    bt = _bt()
    _set_partial(bt)
    # Browser already torn down (None handles), so close is a no-op that
    # still must clear the flag so the next open() starts fresh.
    result = asyncio.run(bt.browser(action="close"))

    assert result.startswith("OK"), result
    assert bt._BROWSER_STATE.get("ssrf_guard_partial") is False


def test_clean_state_is_not_blocked():
    """Sanity: without the flag, the gating layer must not refuse calls
    with the partial-state message. We don't drive the real Playwright
    stack here — we just assert the early gate doesn't trip."""
    bt = _bt()
    # Don't set the flag. Whatever happens downstream (Playwright not
    # installed, launch errors, etc.) is fine; what we're pinning is
    # that the partial-state short-circuit does NOT fire.
    try:
        result = asyncio.run(bt.browser(action="title"))
    except Exception:
        # Playwright internals can throw outright in some environments
        # (e.g. import-time AttributeError). The gate ran first by then,
        # so the negative assertion still holds.
        return
    assert isinstance(result, str)
    assert "partial state" not in result.lower()


def test_source_contains_partial_gate():
    """Source-level pin so the gate can't regress quietly."""
    import cogitum.core.builtin_tools as bt
    src = open(bt.__file__).read()
    assert 'state.get("ssrf_guard_partial")' in src
    assert "partial state" in src
