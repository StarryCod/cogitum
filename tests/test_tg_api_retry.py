"""F54+F60: TelegramAPI.call retries transient httpx errors and
surfaces a friendly fatal on HTTP 409 (another bot polling).

A flaky VPN drop in the middle of a turn used to take down the whole
agent run — caller saw "✕ Error: ConnectError" with no recovery. The
fix: 3-attempt retry loop with 1s/2s/4s backoff for ReadTimeout,
ConnectError and RemoteProtocolError. Anything else propagates.

For HTTP 409 on getUpdates (typical: parallel `cog tg run` with daemon
already polling) we log a clear hint and raise TelegramAuthError so
the polling loop's auth-error branch shuts down loudly instead of
spinning in backoff.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


@pytest.mark.asyncio
async def test_call_retries_on_read_timeout_and_succeeds():
    """Two ReadTimeouts then a 200 → call returns ok."""
    from cogitum.gateway.telegram import TelegramAPI

    api = TelegramAPI("dummy:token")

    # Pre-populate _client so _ensure_client doesn't try to construct
    # a real httpx client.
    api._client = MagicMock()
    api._client.is_closed = False

    fake_post = AsyncMock(side_effect=[
        httpx.ReadTimeout("vpn blip 1"),
        httpx.ReadTimeout("vpn blip 2"),
        _FakeResponse(200, {"ok": True, "result": {}}),
    ])
    api._client.post = fake_post

    with patch("cogitum.gateway.telegram.asyncio.sleep", AsyncMock()):
        data = await api.call("sendMessage", chat_id=1, text="x")

    assert data["ok"] is True
    assert fake_post.call_count == 3, (
        f"expected 3 attempts, got {fake_post.call_count}"
    )


@pytest.mark.asyncio
async def test_call_retries_exhausted_raises_last():
    """3 consecutive ConnectErrors → last one bubbles up."""
    from cogitum.gateway.telegram import TelegramAPI

    api = TelegramAPI("dummy:token")
    api._client = MagicMock()
    api._client.is_closed = False
    api._client.post = AsyncMock(side_effect=httpx.ConnectError("dead"))

    with patch("cogitum.gateway.telegram.asyncio.sleep", AsyncMock()):
        with pytest.raises(httpx.ConnectError):
            await api.call("sendMessage", chat_id=1, text="x")

    assert api._client.post.call_count == 3


@pytest.mark.asyncio
async def test_call_does_not_retry_other_errors():
    """A non-network exception (e.g. PoolTimeout-shaped Exception) is
    NOT retried — propagates on first attempt."""
    from cogitum.gateway.telegram import TelegramAPI

    class _Custom(Exception):
        pass

    api = TelegramAPI("dummy:token")
    api._client = MagicMock()
    api._client.is_closed = False
    api._client.post = AsyncMock(side_effect=_Custom("nope"))

    with patch("cogitum.gateway.telegram.asyncio.sleep", AsyncMock()):
        with pytest.raises(_Custom):
            await api.call("sendMessage", chat_id=1, text="x")

    assert api._client.post.call_count == 1


@pytest.mark.asyncio
async def test_get_updates_409_raises_friendly_auth_error(caplog):
    """409 on getUpdates → TelegramAuthError + critical log mentioning
    `cog tg stop`.  This is the canonical "another instance is polling"
    diagnostic and the user MUST see the hint."""
    import logging
    from cogitum.gateway.telegram import TelegramAPI, TelegramAuthError

    api = TelegramAPI("dummy:token")
    api._client = MagicMock()
    api._client.is_closed = False
    api._client.post = AsyncMock(return_value=_FakeResponse(
        409, {"ok": False, "description": "Conflict: terminated by other getUpdates"},
    ))

    with caplog.at_level(logging.CRITICAL, logger="cogitum.gateway.telegram"):
        with pytest.raises(TelegramAuthError):
            await api.call("getUpdates", offset=0, timeout=30)

    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "cog tg stop" in text, f"missing operator hint, log={text!r}"
