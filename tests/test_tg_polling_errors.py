"""Tests for Tier 3 bug #3: 401 BUSY-LOOP on invalid Telegram bot token.

Covers:
- TelegramAPI.call attaches _http_status to returned dict.
- get_updates raises TelegramAuthError on 401 (revoked token).
- get_updates raises TelegramRateLimitError(N) on 429 with retry_after.
- get_updates returns [] on 503 with the existing warning behaviour.
- _poll_loop exits cleanly on TelegramAuthError (no infinite loop).
- _poll_loop honours retry_after on TelegramRateLimitError.
- start() returns early without polling if getMe returns 401.

Tests use MagicMock-stubbed httpx.AsyncClient — no real network, no
spinning event loop time.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from cogitum.gateway.telegram import (
    CogitumBot,
    TelegramAPI,
    TelegramAuthError,
    TelegramRateLimitError,
)
from cogitum.gateway.tg_config import TelegramConfig


# ── Helpers ─────────────────────────────────────────────────────────────────


def _mock_response(status_code: int, body: dict) -> MagicMock:
    """Build a MagicMock that quacks like an httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=body)
    return resp


def _stub_api_client(api: TelegramAPI, response: MagicMock) -> AsyncMock:
    """Replace api._client with one that returns `response` from .post()."""
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client.is_closed = False
    api._client = client
    return client


@pytest.fixture
def isolated_offset(tmp_path, monkeypatch):
    fake = tmp_path / "tg_offset"
    monkeypatch.setattr(CogitumBot, "_offset_path", staticmethod(lambda: fake))
    return fake


def _make_bot() -> CogitumBot:
    cfg = TelegramConfig(bot_token="fake-token", allowed_user_id=1)
    return CogitumBot(cfg)


# ── (a) call() attaches _http_status ────────────────────────────────────────


@pytest.mark.asyncio
async def test_call_attaches_http_status_on_success():
    api = TelegramAPI("fake")
    _stub_api_client(api, _mock_response(200, {"ok": True, "result": []}))
    data = await api.call("getUpdates")
    assert data["_http_status"] == 200
    assert data["ok"] is True


@pytest.mark.asyncio
async def test_call_attaches_http_status_on_401():
    """call() now raises TelegramAuthError on 401 — see H1 quality fix.

    Previously call() returned the dict with _http_status=401 and let
    get_updates do the raise. After the fix, every method raises so
    send_message / edit_message etc. surface a token revoke loudly.
    """
    api = TelegramAPI("fake")
    _stub_api_client(api, _mock_response(401, {"ok": False, "description": "Unauthorized"}))
    with pytest.raises(TelegramAuthError):
        await api.call("getUpdates")


# ── (b) get_updates raises TelegramAuthError on 401/404 ─────────────────────


@pytest.mark.asyncio
async def test_get_updates_raises_auth_error_on_401():
    api = TelegramAPI("fake")
    _stub_api_client(api, _mock_response(401, {"ok": False, "description": "Unauthorized"}))
    with pytest.raises(TelegramAuthError):
        await api.get_updates()


@pytest.mark.asyncio
async def test_get_updates_raises_auth_error_on_404():
    api = TelegramAPI("fake")
    _stub_api_client(api, _mock_response(404, {"ok": False, "description": "Not Found"}))
    with pytest.raises(TelegramAuthError):
        await api.get_updates()


# ── (c) get_updates raises TelegramRateLimitError on 429 ────────────────────


@pytest.mark.asyncio
async def test_get_updates_raises_rate_limit_with_retry_after():
    api = TelegramAPI("fake")
    body = {
        "ok": False,
        "error_code": 429,
        "description": "Too Many Requests: retry after 17",
        "parameters": {"retry_after": 17},
    }
    _stub_api_client(api, _mock_response(429, body))
    with pytest.raises(TelegramRateLimitError) as exc:
        await api.get_updates()
    assert exc.value.retry_after == 17


@pytest.mark.asyncio
async def test_get_updates_rate_limit_default_retry_after_5():
    api = TelegramAPI("fake")
    # Body missing parameters.retry_after — fall back to 5.
    body = {"ok": False, "error_code": 429, "description": "Too Many Requests"}
    _stub_api_client(api, _mock_response(429, body))
    with pytest.raises(TelegramRateLimitError) as exc:
        await api.get_updates()
    assert exc.value.retry_after == 5


# ── (d) get_updates returns [] on 503, warning logged ───────────────────────


@pytest.mark.asyncio
async def test_get_updates_returns_empty_on_503(caplog):
    """503 (TG infra hiccup) — non-fatal, return [] and let backoff retry.

    Tightened (M5/F1): previously this test would still pass if the
    401/404/429 raise paths were reverted, because it only exercises
    the 503 leg. Coverage for the auth/rate-limit raise paths is
    elsewhere (test_get_updates_raises_auth_error_on_401, etc.) — what
    this test pins specifically is "503 must NOT raise".
    """
    api = TelegramAPI("fake")
    _stub_api_client(api, _mock_response(503, {"ok": False, "description": "service unavailable"}))
    with caplog.at_level(logging.WARNING, logger="cogitum.gateway.telegram"):
        # Explicitly no pytest.raises — 503 must return cleanly.
        result = await api.get_updates()
    # Strict identity: not just falsy, exactly the empty-list contract.
    assert result == []
    assert isinstance(result, list)
    # The .call() warning should be present.
    assert any("TG API error" in r.message for r in caplog.records)


# ── (e) _poll_loop exits cleanly on TelegramAuthError ───────────────────────


@pytest.mark.asyncio
async def test_poll_loop_exits_on_auth_error(isolated_offset, caplog):
    bot = _make_bot()
    bot._running = True
    bot.api.get_updates = AsyncMock(side_effect=TelegramAuthError("bad token"))

    with caplog.at_level(logging.CRITICAL, logger="cogitum.gateway.telegram"):
        # Wrap in wait_for to guarantee no infinite loop — fail loudly
        # if the loop somehow doesn't exit within 2s.
        import asyncio
        await asyncio.wait_for(bot._poll_loop(), timeout=2.0)

    assert bot._running is False
    # get_updates was called exactly once before bailing.
    assert bot.api.get_updates.await_count == 1
    assert any(
        "invalid or revoked" in r.message.lower()
        or "invalid or revoked" in str(r.args).lower()
        for r in caplog.records
    )


# ── (f) _poll_loop honours retry_after on rate limit ────────────────────────


@pytest.mark.asyncio
async def test_poll_loop_sleeps_retry_after_on_rate_limit(
    isolated_offset, monkeypatch
):
    """First call raises RateLimit(7); second call exits the loop."""
    bot = _make_bot()
    bot._running = True

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        # Stop the loop after the first sleep so the test ends.
        bot._running = False

    import cogitum.gateway.telegram as tg_mod
    monkeypatch.setattr(tg_mod.asyncio, "sleep", fake_sleep)

    bot.api.get_updates = AsyncMock(side_effect=TelegramRateLimitError(7))

    import asyncio
    await asyncio.wait_for(bot._poll_loop(), timeout=2.0)

    assert sleeps == [7]
    # Backoff state should NOT have been mutated (rate limit uses TG's hint).
    assert bot._poll_backoff == 1.0


# ── (g) start() returns early on bad token (getMe → 401) ────────────────────


@pytest.mark.asyncio
async def test_start_returns_early_on_getme_401(isolated_offset, caplog):
    bot = _make_bot()
    bot.api.call = AsyncMock(
        return_value={
            "ok": False,
            "description": "Unauthorized",
            "_http_status": 401,
        }
    )

    with caplog.at_level(logging.CRITICAL, logger="cogitum.gateway.telegram"):
        await bot.start()

    # Only getMe should have been called — no polling, no mesh load.
    assert bot.api.call.await_count == 1
    assert bot.api.call.await_args.args[0] == "getMe"
    assert bot._running is False
    # No polling task spawned.
    assert bot._poll_task is None
    assert any(
        "invalid or revoked" in r.message.lower()
        or "invalid or revoked" in str(r.args).lower()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_start_returns_early_when_ok_false_unauthorized(
    isolated_offset, caplog
):
    """Even if status is 200, ok=False with 'unauthorized' description bails."""
    bot = _make_bot()
    bot.api.call = AsyncMock(
        return_value={
            "ok": False,
            "description": "Unauthorized: bot token wrong",
            "_http_status": 200,
        }
    )

    with caplog.at_level(logging.CRITICAL, logger="cogitum.gateway.telegram"):
        await bot.start()

    assert bot.api.call.await_count == 1
    assert bot._poll_task is None


# ── (h) getMe bails on ANY ok=false (Token expired et al.) ──────────────────


@pytest.mark.asyncio
async def test_getme_bails_on_token_expired(isolated_offset, caplog):
    """getMe → 200 + ok=false + description='Token expired' must bail.

    Adversarial finding 4: substring filter on 'unauthorized' missed
    'Token expired', 'TOKEN_INVALID', 'Forbidden' etc. Any ok=false
    response is unrecoverable for a bot token.
    """
    bot = _make_bot()
    bot.api.call = AsyncMock(
        return_value={
            "ok": False,
            "description": "Token expired",
            "_http_status": 200,
        }
    )

    with caplog.at_level(logging.CRITICAL, logger="cogitum.gateway.telegram"):
        await bot.start()

    assert bot.api.call.await_count == 1, "only getMe should be called"
    assert bot._poll_task is None
    assert any(
        "invalid or revoked" in r.message.lower()
        or "invalid or revoked" in str(r.args).lower()
        for r in caplog.records
    )


# ── (i) 429 retry_after fragility / cap ─────────────────────────────────────


def test_rate_limit_error_null_retry_after_defaults_to_5():
    """retry_after=None (JSON null) → fall back to 5s, не TypeError."""
    e = TelegramRateLimitError(None)
    assert e.retry_after == 5


def test_rate_limit_error_string_retry_after_coerces():
    """TG sometimes sends '30' as string."""
    e = TelegramRateLimitError("30")
    assert e.retry_after == 30


def test_rate_limit_error_caps_at_300():
    """86400 (24h) hint клампится в 5min — иначе шлюз заморожен на сутки."""
    e = TelegramRateLimitError(99999)
    assert e.retry_after == 300


def test_rate_limit_error_floor_at_1():
    """retry_after=0 не должен превращаться в busy-loop."""
    e = TelegramRateLimitError(0)
    assert e.retry_after == 1


def test_rate_limit_error_garbage_input_defaults_to_5():
    e = TelegramRateLimitError("abc")
    assert e.retry_after == 5
    e2 = TelegramRateLimitError([])
    assert e2.retry_after == 5


@pytest.mark.asyncio
async def test_get_updates_rate_limit_null_retry_after():
    """parameters.retry_after=null shouldn't TypeError; fall back to 5s."""
    api = TelegramAPI("fake")
    body = {
        "ok": False,
        "error_code": 429,
        "description": "Too Many Requests",
        "parameters": {"retry_after": None},
    }
    _stub_api_client(api, _mock_response(429, body))
    with pytest.raises(TelegramRateLimitError) as exc:
        await api.get_updates()
    assert exc.value.retry_after == 5


@pytest.mark.asyncio
async def test_get_updates_rate_limit_caps_runaway_value():
    """TG-supplied 86400 (24h) clamps to 300s."""
    api = TelegramAPI("fake")
    body = {
        "ok": False,
        "error_code": 429,
        "description": "Too Many Requests",
        "parameters": {"retry_after": 86400},
    }
    _stub_api_client(api, _mock_response(429, body))
    with pytest.raises(TelegramRateLimitError) as exc:
        await api.get_updates()
    assert exc.value.retry_after == 300


# ── (j) _scrub_token redacts bot URL ────────────────────────────────────────


def test_scrub_token_redacts_full_bot_url():
    """httpx exception strings include the full bot URL — must be scrubbed."""
    msg = (
        "RemoteProtocolError: "
        "https://api.telegram.org/bot1234567890:AAFakeAAA-Bbb_CcDd/getUpdates"
        " timed out"
    )
    scrubbed = TelegramAPI._scrub_token(msg)
    assert "1234567890" not in scrubbed
    assert "AAFakeAAA" not in scrubbed
    assert "/bot<REDACTED>/" in scrubbed


def test_scrub_token_handles_empty_input():
    assert TelegramAPI._scrub_token("") == ""
    assert TelegramAPI._scrub_token(None) is None  # type: ignore[arg-type]


def test_scrub_token_passes_through_clean_strings():
    msg = "ConnectionError: name resolution failed for proxy"
    assert TelegramAPI._scrub_token(msg) == msg


# ── (k) TelegramAuthError raised from call() (not just get_updates) ─────────


@pytest.mark.asyncio
async def test_call_raises_auth_error_for_send_message():
    """send_message → call() → TG returns 401 must raise TelegramAuthError.

    Quality H1: previously only get_updates raised; now every method
    surfaces a mid-conversation token revoke.
    """
    api = TelegramAPI("fake")
    _stub_api_client(api, _mock_response(401, {"ok": False, "description": "Unauthorized"}))
    with pytest.raises(TelegramAuthError):
        await api.send_message(123, "hello")


@pytest.mark.asyncio
async def test_call_raises_auth_error_for_answer_callback():
    api = TelegramAPI("fake")
    _stub_api_client(api, _mock_response(404, {"ok": False, "description": "Not Found"}))
    with pytest.raises(TelegramAuthError):
        await api.answer_callback("cb-1", "hi")


# ── (l) _poll_loop generic-error exponential backoff cap ───────────────────


@pytest.mark.asyncio
async def test_poll_loop_backoff_caps_at_30s(isolated_offset, monkeypatch):
    """Generic-exception arm: backoff doubles, capped at 30s.

    Set _poll_backoff to 29 before the failure; assert sleep == 29s
    then the backoff caps at 30 (not 58).
    """
    bot = _make_bot()
    bot._running = True
    bot._poll_backoff = 29.0

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        # Stop after first sleep so test exits.
        bot._running = False

    import cogitum.gateway.telegram as tg_mod
    monkeypatch.setattr(tg_mod.asyncio, "sleep", fake_sleep)

    bot.api.get_updates = AsyncMock(side_effect=RuntimeError("boom"))

    import asyncio
    await asyncio.wait_for(bot._poll_loop(), timeout=2.0)

    assert sleeps == [29.0]
    # Backoff doubled then capped: min(58, 30) == 30.
    assert bot._poll_backoff == 30.0



# ---------------------------------------------------------------------------
# R2 hardening: traceback scrubbing + callback dedup propagation
# ---------------------------------------------------------------------------

def test_token_scrub_filter_redacts_traceback():
    """logging.exception(exc_info=True) renders the traceback into
    record.exc_text. httpx errors include the full /bot<TOKEN>/ URL in
    their str(); the filter must redact it before any handler sees it.
    """
    import logging
    from cogitum.gateway.telegram import _TokenScrubFilter, _TG_TOKEN_RE

    SECRET = "1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    err_text = (
        f"ConnectError: HTTPSConnectionPool(host='api.telegram.org', port=443): "
        f"https://api.telegram.org/bot{SECRET}/getUpdates"
    )

    record = logging.LogRecord(
        name="cogitum.gateway.telegram",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="poll error",
        args=(),
        exc_info=None,
    )
    record.exc_text = err_text

    filt = _TokenScrubFilter()
    assert filt.filter(record) is True
    assert SECRET not in record.exc_text
    assert "/bot<REDACTED>/" in record.exc_text


def test_token_scrub_filter_scrubs_msg_and_args():
    import logging
    from cogitum.gateway.telegram import _TokenScrubFilter

    SECRET = "1234567890:ABCDEF"
    record = logging.LogRecord(
        name="cogitum.gateway.telegram",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="connect failed: %s",
        args=(f"https://api.telegram.org/bot{SECRET}/sendMessage",),
        exc_info=None,
    )
    _TokenScrubFilter().filter(record)
    assert SECRET not in str(record.args)
    assert "/bot<REDACTED>/" in record.args[0]


@pytest.mark.asyncio
async def test_callback_dedup_propagates_auth_error():
    """When the duplicate-ack path catches an exception it must let
    TelegramAuthError through to _spawn_handler so the gateway can
    shut down. Generic exceptions still get swallowed at debug."""
    import collections
    from unittest.mock import AsyncMock, MagicMock
    from cogitum.gateway.telegram import CogitumBot, TelegramAuthError

    bot = CogitumBot.__new__(CogitumBot)
    bot.config = MagicMock()
    bot.config.can_respond = MagicMock(return_value=True)
    bot.api = MagicMock()
    bot.api.answer_callback = AsyncMock(
        side_effect=TelegramAuthError("revoked")
    )
    bot.sessions = {}
    bot._seen_callbacks = collections.OrderedDict()
    bot._seen_callbacks_max = 256
    # Prime the dedup ring so the next call hits the duplicate branch.
    bot._is_duplicate_callback("cb-1")

    cb = {
        "id": "cb-1",
        "data": "approve:abc12345",
        "from": {"id": 1},
        "message": {"chat": {"id": 1}, "message_id": 1},
    }

    with pytest.raises(TelegramAuthError):
        await bot._handle_callback(cb)
