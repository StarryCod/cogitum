"""F1-F4: None-agent guards on /godmode, /yolo, /model (TG) and
/yolo, /godmode (TUI app).

When the bot starts on a fresh install with no providers configured,
``self.agent`` is None — touching ``self.agent.cfg`` raised
AttributeError and crashed the update handler. The fix adds an early
"No active model. Run /setup first." reply that mirrors the existing
/compact branch.

These tests pin the user-facing message and prove no AttributeError
escapes the handler. We drive _handle_command directly with the same
__new__ + manual-attrs stub pattern used in test_tg_acl.py.
"""
from __future__ import annotations

import collections
from unittest.mock import MagicMock

import pytest


# ── stubs ────────────────────────────────────────────────────────────


class _FakeAPI:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))

    async def answer_callback(self, cb_id, text=""):  # pragma: no cover
        pass

    async def edit_message(self, *a, **kw):  # pragma: no cover
        pass


class _FakeConfig:
    def __init__(self) -> None:
        self.allowed_user_id = 1
        self.allowed_chat_ids: list[int] = []

    def can_respond(self, *, user_id, chat_id):
        return user_id == self.allowed_user_id


class _Session:
    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id
        self.history: list = []
        self.session_id: str | None = None
        self.agent_task = None

    @property
    def is_busy(self) -> bool:
        return False


def _make_bot_no_agent():
    from cogitum.gateway.telegram import CogitumBot

    bot = CogitumBot.__new__(CogitumBot)
    bot.config = _FakeConfig()  # type: ignore
    bot.api = _FakeAPI()  # type: ignore
    bot.agent = None  # ← the whole point
    bot.sessions = {}
    bot.mesh = MagicMock()
    bot.mesh.resolve = MagicMock(return_value=[])
    bot._pre_godmode_system = None
    bot._approval_token_to_call_id = collections.OrderedDict()
    bot._approval_token_max = 64
    bot._seen_callbacks = collections.OrderedDict()
    bot._seen_callbacks_max = 256
    return bot


def _msg(chat_id: int, user_id: int) -> dict:
    return {"chat": {"id": chat_id}, "from": {"id": user_id}, "message_id": 1}


# ── TG: /godmode, /yolo, /model with agent=None ────────────────────


@pytest.mark.parametrize("cmd", ["/godmode", "/godmode on", "/yolo", "/yolo on", "/model", "/model anthropic/claude"])
@pytest.mark.asyncio
async def test_tg_command_no_agent_replies_friendly(cmd):
    """Each operator command that touches Agent.cfg must emit a clean
    'No active model. Run /setup first.' instead of crashing with
    AttributeError on self.agent.cfg.X.
    """
    bot = _make_bot_no_agent()
    session = _Session(chat_id=1)

    # Must NOT raise — that's the whole bug.
    await bot._handle_command(cmd, session, _msg(1, 1))

    sent_text = "\n".join(t for _, t, _ in bot.api.sent)
    assert "No active model" in sent_text, (
        f"expected 'No active model' guard for {cmd!r}, got {bot.api.sent!r}"
    )
    assert "Run /setup first" in sent_text


# ── TUI: app.py /yolo, /godmode with _agent=None ──────────────────


class _Feed:
    def __init__(self) -> None:
        self.systems: list[tuple[str, str]] = []
        self.errors: list[str] = []

    def append_system(self, text, tag=""):
        self.systems.append((text, tag))

    def append_error(self, text, meta=""):
        self.errors.append(text)

    def append_user(self, *a, **kw):  # pragma: no cover
        pass


@pytest.mark.parametrize("cmd", ["/yolo", "/yolo on", "/godmode", "/godmode on"])
def test_tui_command_no_agent_replies_friendly(cmd):
    """app.py mirror — _handle_command on a fresh CogitumApp before any
    model has been picked. Same guard, same message, no AttributeError.
    """
    from cogitum.app import CogitumApp

    app = CogitumApp.__new__(CogitumApp)
    app._agent = None  # type: ignore
    app.mesh = None
    feed = _Feed()

    # Must NOT raise.
    app._handle_command(cmd, feed)

    msgs = " ".join(feed.errors)
    assert "No active model" in msgs, (
        f"expected guard for {cmd!r}, errors={feed.errors!r}"
    )
