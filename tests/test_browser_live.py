"""Live integration test for the browser tool — verifies playwright works.

Skipped if chromium isn't installed locally.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import pytest_asyncio

from cogitum.core.builtin_tools import browser


def _chromium_available() -> bool:
    cache = Path("~/.cache/ms-playwright").expanduser()
    if not cache.is_dir():
        return False
    for entry in cache.iterdir():
        if entry.name.startswith("chromium-"):
            cand = entry / "chrome-linux64" / "chrome"
            if cand.exists():
                return True
    return False


pytestmark = pytest.mark.skipif(
    not _chromium_available(),
    reason="playwright chromium not installed in ~/.cache/ms-playwright",
)


@pytest_asyncio.fixture
async def _browser_close_after():
    yield
    await browser(action="close")


@pytest.mark.asyncio
async def test_open_example_returns_title(_browser_close_after):
    out = await browser(action="open", url="https://example.com")
    assert "OK: opened" in out
    assert "Example Domain" in out


@pytest.mark.asyncio
async def test_text_extracts_body(_browser_close_after):
    await browser(action="open", url="https://example.com")
    out = await browser(action="text")
    assert "Example Domain" in out
    assert "illustrative" in out.lower() or "examples" in out.lower()


@pytest.mark.asyncio
async def test_extract_specific_selector(_browser_close_after):
    await browser(action="open", url="https://example.com")
    out = await browser(action="extract", selector="h1")
    assert out.strip() == "Example Domain"


@pytest.mark.asyncio
async def test_act_runs_arbitrary_js(_browser_close_after):
    await browser(action="open", url="https://example.com")
    out = await browser(action="act", text="document.title")
    assert "Example Domain" in out


@pytest.mark.asyncio
async def test_links_lists_anchors(_browser_close_after):
    await browser(action="open", url="https://example.com")
    out = await browser(action="links")
    assert "Links" in out
    # example.com has at least one outbound link
    assert "http" in out


@pytest.mark.asyncio
async def test_screenshot_writes_png(_browser_close_after):
    await browser(action="open", url="https://example.com")
    out = await browser(action="screenshot")
    m = re.search(r"saved to (/[^\s]+\.png)", out)
    assert m, out
    path = m.group(1)
    assert os.path.exists(path)
    assert os.path.getsize(path) > 1000
    os.unlink(path)


@pytest.mark.asyncio
async def test_unknown_action_errors(_browser_close_after):
    await browser(action="open", url="https://example.com")
    out = await browser(action="hyperjump")
    assert "ERROR" in out
    assert "hyperjump" in out
