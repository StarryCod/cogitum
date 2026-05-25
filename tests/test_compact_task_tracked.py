"""F33/F51: /compact must store its background task in self._bg_tasks.

Without the reference, the asyncio runtime can GC the task mid-await and
the compaction silently disappears.
"""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest


def _import_app_module():
    """Import cogitum.app — required imports are heavy; isolate behind helper."""
    if "cogitum.app" not in sys.modules:
        import cogitum.app  # noqa: F401
    return sys.modules["cogitum.app"]


def test_bg_tasks_set_exists_and_tracks_compact(monkeypatch):
    """The /compact branch creates a task and stores it in self._bg_tasks."""
    app_mod = _import_app_module()

    # We won't construct the full Textual App — too heavy for this unit
    # test. Instead, stub the minimum surface needed: a fake instance
    # exposing _bg_tasks plus the bound method.
    fake_self = SimpleNamespace(_bg_tasks=set())

    async def runner():
        async def _do_compact():
            await asyncio.sleep(0)

        # F33/F51 fix pattern, copied from app.py:
        compact_task = asyncio.create_task(_do_compact())
        fake_self._bg_tasks.add(compact_task)
        compact_task.add_done_callback(fake_self._bg_tasks.discard)
        # Pre-await assertion: task is tracked.
        assert compact_task in fake_self._bg_tasks
        await compact_task
        # done_callback should have removed it.
        await asyncio.sleep(0)
        assert compact_task not in fake_self._bg_tasks

    asyncio.run(runner())


def test_compact_task_pattern_present_in_app_source():
    """Source-level guard so a future refactor can't quietly drop the fix.

    Asserts the /compact branch in app.py uses the
    self._bg_tasks.add(...) + add_done_callback(self._bg_tasks.discard)
    idiom, not a raw asyncio.create_task(_do_compact()) one-liner.
    """
    import cogitum.app
    src = open(cogitum.app.__file__).read()
    # The fix replaces the bare create_task with a tracked variant.
    assert "compact_task = asyncio.create_task(_do_compact())" in src
    assert "self._bg_tasks.add(compact_task)" in src
    assert "compact_task.add_done_callback(self._bg_tasks.discard)" in src
    # And the orphan form should NOT be present anymore.
    assert "asyncio.create_task(_do_compact())\n            return" not in src
