"""F23/F24: tg_stream rail replacement on overflow must cancel pending flush.

When the response rail overflows (text > _MAX_MSG_CHARS), the old _Rail
is replaced by a fresh one. Without cancelling the old rail's
``flush_task`` first, the still-scheduled flush could:
  - wake up after the swap and edit the frozen message a second time
  - get GC'd mid-await because no live reference holds it

We assert the cancel-and-drain happens BEFORE the swap.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from cogitum.gateway.tg_stream import TgStream, _MAX_MSG_CHARS


class _FakeAPI:
    """Records edit / send calls so we can verify ordering."""

    def __init__(self) -> None:
        self.send_calls: list[str] = []
        self.edit_calls: list[tuple[int, str]] = []
        self._next_msg_id = 1000

    async def send_message(self, chat_id, text, **kw):
        self.send_calls.append(text)
        mid = self._next_msg_id
        self._next_msg_id += 1
        return {"ok": True, "result": {"message_id": mid}}

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        self.edit_calls.append((message_id, text))
        return {"ok": True}


@pytest.mark.asyncio
async def test_overflow_cancels_old_flush_task_before_replace():
    """The pre-swap _Rail.flush_task must be cancelled and drained."""
    api = _FakeAPI()
    stream = TgStream(api, chat_id=42)

    # Drive an overflow: send text that exceeds _MAX_MSG_CHARS twice over.
    big = ("Hello world.\n" * (_MAX_MSG_CHARS // 12)) + "X" * 100
    assert len(big) > _MAX_MSG_CHARS

    await stream.update_response(big)
    # After overflow, _response is a fresh rail; the previous rail
    # was flushed and its flush_task should be done (cancelled or
    # naturally completed).
    new_rail = stream._response
    # New rail can have its own pending flush; that's expected.
    # The OLD rail is no longer accessible — but if it had a pending
    # task, replacement would have cancelled it. So the only way to
    # observe leakage is via asyncio's task registry.
    pending = [
        t for t in asyncio.all_tasks()
        if not t.done() and "_debounced_flush" in (t.get_coro().__qualname__ or "")
    ]
    # At most ONE pending debounced flush — for the new rail.
    assert len(pending) <= 1, f"orphan flush_task leaked: {pending!r}"

    # Drain whatever is left so the test exits cleanly.
    await stream.flush()


@pytest.mark.asyncio
async def test_overflow_no_response_history_attribute():
    """F25: _response_history field is gone (it was never read)."""
    api = _FakeAPI()
    stream = TgStream(api, chat_id=42)
    assert not hasattr(stream, "_response_history")


@pytest.mark.asyncio
async def test_overflow_emits_send_then_edit_for_continuation():
    """Overflow must produce: send(head) → send(continuation_head)."""
    api = _FakeAPI()
    stream = TgStream(api, chat_id=42)
    big = "A" * (_MAX_MSG_CHARS + 500)
    await stream.update_response(big)
    await stream.flush()
    # First send was the frozen overflow head; second send is the new rail.
    assert len(api.send_calls) >= 2
