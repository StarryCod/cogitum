"""F32: TelegramAPI.send_message auto-splits messages > 4000 chars.

Telegram caps a single sendMessage at 4096 chars. /tools with 200+ MCP
tools, big tracebacks in compact failures, /help in extreme cases —
all could overflow and trip ok=false 'message is too long' →
TelegramSendError → handler dies.

Fix: if len(text) > 4000, split via tg_formatter.split_message and
chain the chunks. reply_markup attaches to the last chunk only.

These tests count send-call rounds against the underlying _send
primitive: a 10000-char message must produce >1 call.
"""
from __future__ import annotations

import pytest


class _TraceAPI:
    """Subclass swap: track every _send_message_single invocation."""

    def __init__(self) -> None:
        from cogitum.gateway.telegram import TelegramAPI
        self._real = TelegramAPI("dummy:token")
        self.calls: list[tuple] = []

        async def _fake_single(*, chat_id, text, parse_mode, reply_to, reply_markup):
            self.calls.append({
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_to": reply_to,
                "reply_markup": reply_markup,
            })
            return {"ok": True, "result": {"message_id": len(self.calls)}}

        # Bind onto the real instance so send_message dispatches into
        # our fake.
        self._real._send_message_single = _fake_single  # type: ignore

    async def send(self, *a, **kw):
        return await self._real.send_message(*a, **kw)


@pytest.mark.asyncio
async def test_send_message_under_limit_single_call():
    api = _TraceAPI()
    await api.send(123, "x" * 100, parse_mode=None)
    assert len(api.calls) == 1, f"short message must NOT split, got {len(api.calls)}"


@pytest.mark.asyncio
async def test_send_message_over_4000_splits():
    api = _TraceAPI()
    await api.send(123, "x" * 10000, parse_mode=None)

    assert len(api.calls) >= 2, (
        f"10000-char message must split into >=2 chunks, got {len(api.calls)}"
    )
    # No chunk exceeds Telegram's hard cap.
    for c in api.calls:
        assert len(c["text"]) <= 4096, (
            f"chunk over Telegram limit: {len(c['text'])} chars"
        )


@pytest.mark.asyncio
async def test_send_message_split_attaches_markup_only_to_last():
    """reply_markup belongs on the LAST chunk so the buttons land
    where the user's eye is."""
    api = _TraceAPI()
    markup = {"inline_keyboard": [[{"text": "go", "callback_data": "x"}]]}
    await api.send(123, "y" * 9000, parse_mode=None, reply_markup=markup)

    assert len(api.calls) >= 2
    # First chunk has no markup; last does.
    assert api.calls[0]["reply_markup"] is None
    assert api.calls[-1]["reply_markup"] == markup


@pytest.mark.asyncio
async def test_send_message_reply_to_only_first():
    """reply_to_message_id belongs on the FIRST chunk only — the
    follow-ups are continuations, not separate replies."""
    api = _TraceAPI()
    await api.send(123, "z" * 9000, parse_mode=None, reply_to=42)

    assert len(api.calls) >= 2
    assert api.calls[0]["reply_to"] == 42
    for c in api.calls[1:]:
        assert c["reply_to"] is None
