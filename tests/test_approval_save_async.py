"""Async I/O hygiene: ``_save_approval_tokens`` must not block the loop.

The disk write + chmod inside the persist path is fast on local SSD
but stutters under network filesystems (NFS, fuse, encrypted volumes).
We expose two methods now:

  - ``_save_approval_tokens_sync`` — the blocking implementation.
  - ``_save_approval_tokens``      — async wrapper that offloads to a
    worker thread via ``asyncio.to_thread``.

These tests pin both shapes so a future refactor can't silently bring
the blocking call back into the event loop.

Also enforces (regex) that ``cogitum/app.py`` and the rest of the
package use ``asyncio.create_task`` instead of the old
``asyncio.ensure_future`` for fire-and-forget coroutines.
"""
from __future__ import annotations

import asyncio
import collections
import inspect
import json
import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
COGITUM_DIR = REPO_ROOT / "cogitum"


# ── _save_approval_tokens shape ──────────────────────────────────────


def _make_bot(tmp_path):
    from cogitum.gateway.telegram import CogitumBot

    bot = CogitumBot.__new__(CogitumBot)
    bot._approval_token_to_call_id = collections.OrderedDict()
    bot._approval_token_max = 64
    bot._approval_persist_path = tmp_path / "tg_approvals.json"
    return bot


def test_sync_method_exists_and_is_blocking():
    """The blocking variant must exist and must NOT be a coroutine."""
    from cogitum.gateway.telegram import CogitumBot

    fn = CogitumBot._save_approval_tokens_sync
    assert not inspect.iscoroutinefunction(fn), (
        "_save_approval_tokens_sync must be a plain (blocking) method"
    )


def test_async_wrapper_is_coroutine():
    """The public ``_save_approval_tokens`` must be an async wrapper."""
    from cogitum.gateway.telegram import CogitumBot

    fn = CogitumBot._save_approval_tokens
    assert inspect.iscoroutinefunction(fn), (
        "_save_approval_tokens must be a coroutine function so callers "
        "can await it without blocking the event loop"
    )


@pytest.mark.asyncio
async def test_async_save_writes_file(tmp_path):
    """End-to-end: awaiting the wrapper must produce the same JSON as
    the sync path."""
    bot = _make_bot(tmp_path)
    bot._approval_token_to_call_id["a1"] = "call_a"
    bot._approval_token_to_call_id["b2"] = "call_b"

    await bot._save_approval_tokens()

    raw = bot._approval_persist_path.read_text(encoding="utf-8")
    assert json.loads(raw) == {"a1": "call_a", "b2": "call_b"}


@pytest.mark.asyncio
async def test_async_save_offloads_to_thread(tmp_path, monkeypatch):
    """The wrapper MUST go through ``asyncio.to_thread`` — that's the
    whole point of the split. We assert by spying on ``to_thread``.
    """
    bot = _make_bot(tmp_path)
    bot._approval_token_to_call_id["tok"] = "call_x"

    real_to_thread = asyncio.to_thread
    calls: list[tuple] = []

    async def _spy(func, /, *args, **kwargs):
        calls.append((func, args, kwargs))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _spy)

    await bot._save_approval_tokens()

    assert calls, "asyncio.to_thread was not invoked by _save_approval_tokens"
    fn = calls[0][0]
    # Bound method whose underlying func is _save_approval_tokens_sync.
    assert getattr(fn, "__func__", fn).__name__ == "_save_approval_tokens_sync"


@pytest.mark.asyncio
async def test_async_save_swallows_io_error(tmp_path):
    """Errors inside the threaded sync impl must not propagate — the
    caller (callback handler) must keep going."""
    bot = _make_bot(tmp_path)
    bot._approval_token_to_call_id["x"] = "y"
    bot._approval_persist_path = tmp_path / "no" / "such" / "dir" / "f.json"

    # Must NOT raise.
    await bot._save_approval_tokens()


# ── ensure_future → create_task migration ────────────────────────────


_ENSURE_FUTURE = re.compile(r"\basyncio\.ensure_future\b")


def _iter_python_sources():
    for p in COGITUM_DIR.rglob("*.py"):
        yield p


def test_app_py_has_no_ensure_future():
    """``cogitum/app.py`` must not call ``asyncio.ensure_future`` —
    every fire-and-forget there is a coroutine, so ``create_task`` is
    the correct (and non-deprecated) call."""
    src = (COGITUM_DIR / "app.py").read_text(encoding="utf-8")
    assert not _ENSURE_FUTURE.search(src), (
        "cogitum/app.py still references asyncio.ensure_future; "
        "use asyncio.create_task instead"
    )


def test_cogitum_tree_has_no_ensure_future():
    """No .py file under cogitum/ should reference asyncio.ensure_future.

    Allowed exception: documentation strings and SKILL.md files (those
    aren't .py and are excluded by the rglob pattern). If a future
    Future-construction site genuinely needs ensure_future, document
    it in the audit and add an explicit allowlist here.
    """
    offenders: list[str] = []
    for path in _iter_python_sources():
        text = path.read_text(encoding="utf-8")
        if _ENSURE_FUTURE.search(text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        "asyncio.ensure_future found in: "
        + ", ".join(offenders)
        + " — replace with asyncio.create_task"
    )


# ── get_event_loop sanity (audit item 4) ─────────────────────────────


def test_tools_py_uses_get_running_loop():
    """``core/tools.py`` runs inside an active loop — it must use
    ``asyncio.get_running_loop()``, not the deprecated
    ``asyncio.get_event_loop()`` which fabricates a fresh loop in
    Python 3.12+ and emits a DeprecationWarning."""
    src = (COGITUM_DIR / "core" / "tools.py").read_text(encoding="utf-8")
    assert "asyncio.get_event_loop()" not in src, (
        "core/tools.py still uses asyncio.get_event_loop(); "
        "switch to asyncio.get_running_loop() inside async contexts"
    )
    assert "asyncio.get_running_loop()" in src
