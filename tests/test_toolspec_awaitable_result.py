"""C1 from /tmp/audit_v5_tools.md.

A *sync* function whose return value is itself an awaitable (functools.partial
over async, decorator that hands back a coroutine, etc.) used to be passed
through `run_in_executor` and the resulting coroutine object was stringified
straight into the tool_result. The model saw ``<coroutine object foo at 0x...>``
and never the actual output — the original "tool output stops reaching the
model" bug.

Fix: post-call ``inspect.isawaitable(result) -> await``.
"""
from __future__ import annotations

import asyncio
import functools

import pytest

from cogitum.core.tools import ToolSpec


# ----------------------------- helpers --------------------------------- #


async def _async_real_work(**kwargs):
    await asyncio.sleep(0)
    return f"real:{kwargs.get('x', '?')}"


def _sync_returns_coroutine(**kwargs):
    """A sync wrapper that *returns* a coroutine. Classic decorator pattern."""
    return _async_real_work(**kwargs)


def _sync_returns_partial(**kwargs):
    """functools.partial over an async fn — common in plugin glue."""
    p = functools.partial(_async_real_work, **kwargs)
    return p()


def _sync_returns_value(**kwargs):
    return f"sync:{kwargs.get('x', '?')}"


async def _async_normal(**kwargs):
    return f"native_async:{kwargs.get('x', '?')}"


def _make_spec(fn, name="t"):
    return ToolSpec(
        name=name,
        description="test",
        parameters={"type": "object", "properties": {}, "required": []},
        fn=fn,
    )


# ----------------------------- tests ----------------------------------- #


@pytest.mark.asyncio
async def test_sync_fn_returning_coroutine_is_awaited() -> None:
    spec = _make_spec(_sync_returns_coroutine)
    result = await spec.call(x=42)
    # Before the fix: result == "<coroutine object _async_real_work at 0x...>"
    assert result == "real:42", f"got {result!r}"


@pytest.mark.asyncio
async def test_sync_fn_returning_partial_coroutine_is_awaited() -> None:
    spec = _make_spec(_sync_returns_partial)
    result = await spec.call(x="abc")
    assert result == "real:abc"


@pytest.mark.asyncio
async def test_native_async_still_works() -> None:
    spec = _make_spec(_async_normal)
    result = await spec.call(x="hi")
    assert result == "native_async:hi"


@pytest.mark.asyncio
async def test_pure_sync_still_works() -> None:
    spec = _make_spec(_sync_returns_value)
    result = await spec.call(x="ok")
    assert result == "sync:ok"


@pytest.mark.asyncio
async def test_result_is_never_a_coroutine_object() -> None:
    """Regression sentinel: under no path does .call return an unawaited coro."""
    for fn in (_sync_returns_coroutine, _sync_returns_partial, _async_normal, _sync_returns_value):
        spec = _make_spec(fn)
        result = await spec.call()
        assert not asyncio.iscoroutine(result), (
            f"{fn.__name__} returned an unawaited coroutine: {result!r}"
        )
