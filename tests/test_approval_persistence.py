"""F27+F76: approval persistence + spawn handler error reply.

F27: ``_approval_token_to_call_id`` is the bot-level dict that
maps callback-data tokens (8 hex chars) back to the agent's tool
call_id. It used to live in memory only — a bot restart left every
pending approval button as a clickable zombie that the user couldn't
distinguish from a fresh prompt, getting only "▲ No pending approval".

The fix: persist the map atomically on every insert/pop. On startup
the bot restores the file. When a callback comes in for a stale
token (it WAS in the file but the agent has no live future), we edit
the message to "[stale — bot restarted, ignore]" so the user knows
that clicking won't help.

F76: when _spawn_handler catches an unhandled Exception in update
processing, it must best-effort-reply "✕ Internal error — check logs."
to the affected chat instead of staying silent.
"""
from __future__ import annotations

import collections
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── F27: persistence helpers ─────────────────────────────────────────


def _make_bot(tmp_path):
    """Construct a CogitumBot via __new__ and pin the persistence
    path to tmp_path so we don't write to ~/.local/share/cogitum."""
    from cogitum.gateway.telegram import CogitumBot

    bot = CogitumBot.__new__(CogitumBot)
    bot._approval_token_to_call_id = collections.OrderedDict()
    bot._approval_token_max = 64
    bot._approval_persist_path = tmp_path / "tg_approvals.json"
    return bot


def test_save_approval_tokens_writes_json(tmp_path):
    bot = _make_bot(tmp_path)
    bot._approval_token_to_call_id["abcd1234"] = "call_xyz"
    bot._approval_token_to_call_id["beefcafe"] = "call_qrs"

    bot._save_approval_tokens_sync()

    raw = bot._approval_persist_path.read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data == {"abcd1234": "call_xyz", "beefcafe": "call_qrs"}


def test_save_approval_tokens_swallows_io_error(tmp_path, caplog):
    """A persist failure (e.g. disk full / permission) MUST NOT propagate
    — the chat handler that just inserted a token would otherwise crash
    on a flaky disk."""
    bot = _make_bot(tmp_path)
    bot._approval_token_to_call_id["x"] = "y"
    # Point at a directory that doesn't exist as a parent — atomic_write
    # raises OSError under that.
    bot._approval_persist_path = tmp_path / "no" / "such" / "dir" / "f.json"
    # Must NOT raise.
    bot._save_approval_tokens_sync()


def test_restore_approval_tokens_loads_existing(tmp_path):
    bot = _make_bot(tmp_path)
    bot._approval_persist_path.write_text(
        json.dumps({"tok1": "call1", "tok2": "call2"}),
        encoding="utf-8",
    )
    # Reset map and reload.
    bot._approval_token_to_call_id = collections.OrderedDict()
    bot._restore_approval_tokens()
    assert dict(bot._approval_token_to_call_id) == {"tok1": "call1", "tok2": "call2"}


def test_restore_approval_tokens_missing_file_silent(tmp_path):
    bot = _make_bot(tmp_path)
    # File doesn't exist — should be a no-op, no exception.
    bot._restore_approval_tokens()
    assert len(bot._approval_token_to_call_id) == 0


def test_restore_approval_tokens_corrupt_file_silent(tmp_path):
    bot = _make_bot(tmp_path)
    bot._approval_persist_path.write_text("{ broken json", encoding="utf-8")
    # Must NOT raise; map stays empty.
    bot._restore_approval_tokens()
    assert len(bot._approval_token_to_call_id) == 0


# ── F27: stale callback after restart ────────────────────────────────


class _RecordAPI:
    def __init__(self) -> None:
        self.sent: list[tuple] = []
        self.answered: list[tuple] = []
        self.edited: list[tuple] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))

    async def answer_callback(self, cb_id, text=""):
        self.answered.append((cb_id, text))

    async def edit_message(self, chat_id, message_id, text, **kw):
        self.edited.append((chat_id, message_id, text))


class _OperatorConfig:
    allowed_user_id = 1
    allowed_chat_ids: list[int] = []

    def can_respond(self, *, user_id, chat_id):
        return user_id == self.allowed_user_id


def _make_full_bot(tmp_path):
    """Like _make_bot but also wires the attrs _handle_callback needs."""
    from cogitum.gateway.telegram import CogitumBot

    bot = CogitumBot.__new__(CogitumBot)
    bot.config = _OperatorConfig()  # type: ignore
    bot.api = _RecordAPI()  # type: ignore
    bot.sessions = {}
    bot.agent = MagicMock()
    bot.agent.submit_approval = MagicMock(return_value=False)  # ← stale
    bot._approval_token_to_call_id = collections.OrderedDict()
    bot._approval_token_max = 64
    bot._approval_persist_path = tmp_path / "tg_approvals.json"
    bot._seen_callbacks = collections.OrderedDict()
    bot._seen_callbacks_max = 256
    return bot


@pytest.mark.asyncio
async def test_callback_for_persisted_stale_token_edits_message(tmp_path):
    """User clicks a button after restart → token resolves to a call_id
    we restored from disk, but the agent has no live future for it
    (submit_approval returns False). The message MUST be edited to
    '[stale — bot restarted, ignore]' so the user understands.
    """
    bot = _make_full_bot(tmp_path)
    # Simulate a restart: token map already has the entry from disk.
    bot._approval_token_to_call_id["tok-restart"] = "call_old"

    callback = {
        "id": "cb-1",
        "data": "approve:tok-restart",
        "from": {"id": 1},
        "message": {"chat": {"id": 1}, "message_id": 99},
    }
    await bot._handle_callback(callback)

    edited_text = "\n".join(t for _, _, t in bot.api.edited)
    assert "stale" in edited_text.lower(), (
        f"expected stale-edit, got {bot.api.edited!r}"
    )
    # And answer_callback fired with a stale-toast.
    assert any("stale" in t.lower() or "restart" in t.lower()
               for _, t in bot.api.answered), (
        f"no stale toast on answer_callback: {bot.api.answered!r}"
    )


# ── F76: spawn handler crash → best-effort error reply ───────────────


@pytest.mark.asyncio
async def test_spawn_handler_replies_on_crash(tmp_path):
    """An uncaught Exception inside _handle_update must trigger a
    user-visible 'Internal error — check logs.' reply, not silence."""
    import asyncio

    from cogitum.gateway.telegram import CogitumBot

    bot = CogitumBot.__new__(CogitumBot)
    bot.config = _OperatorConfig()  # type: ignore
    bot.api = _RecordAPI()  # type: ignore
    bot._global_sem = asyncio.Semaphore(32)
    bot._chat_sems = {}
    bot._chat_sem_users = {}
    bot._per_chat_sem_size = 4
    bot._poll_task = None
    bot._running = True

    async def _crash(update):
        raise RuntimeError("boom")

    bot._handle_update = _crash  # type: ignore
    bot._chat_id_of = lambda u: 7  # type: ignore

    await bot._spawn_handler({"update_id": 1, "message": {"chat": {"id": 7}}})

    text = "\n".join(t for _, t, _ in bot.api.sent)
    assert "Internal error" in text, (
        f"expected user-visible error reply, got sent={bot.api.sent!r}"
    )
