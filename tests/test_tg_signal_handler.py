"""F11: Ctrl+C on Windows must dispatch bot.stop() via
loop.call_soon_threadsafe, not asyncio.create_task() inside a
signal.signal() handler.

Background: signal.signal handlers fire on the main thread, but on
Windows the asyncio loop's invariants don't allow create_task() from
that context — RuntimeError "no running event loop", and bot.stop()
never executes, leaving the gateway in zombie state.

The fix replaces ``asyncio.create_task(...)`` inside the SIGINT lambda
with ``loop.call_soon_threadsafe(lambda: ... create_task(bot.stop()))``.

We test by inspecting the source of run_bot: the Windows branch must
mention ``call_soon_threadsafe`` (the canonical signal-safe pattern).
A more invasive integration test would need a Windows host.
"""
from __future__ import annotations

import inspect


def test_run_bot_uses_call_soon_threadsafe_for_windows_sigint():
    """run_bot's Windows signal handler MUST funnel the stop request
    through loop.call_soon_threadsafe, not directly create_task."""
    from cogitum.gateway import telegram as tg

    src = inspect.getsource(tg.run_bot)
    # Both old (broken) and new (correct) patterns mention create_task,
    # so we check the threadsafe primitive specifically.
    assert "call_soon_threadsafe" in src, (
        "run_bot must use loop.call_soon_threadsafe to schedule "
        "bot.stop() from a signal handler on Windows"
    )
    # Sanity: we still want the win32 branch present.
    assert 'sys.platform == "win32"' in src
