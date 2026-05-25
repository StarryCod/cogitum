"""F38: /yolo on <ttl_minutes> auto-disables after expiry.

Operator turns yolo on for a 30-min sprint, forgets to /yolo off, and
the bot keeps auto-approving every tool forever. The fix: optional
TTL argument, stored as ``cfg.yolo_until`` (monotonic seconds, NOT
wall clock — NTP step-back must not extend a privileged window).
The approval gate in agent.py checks
``time.monotonic() > yolo_until`` and lazily flips ``yolo_mode`` back
to False the first time the deadline passes.

Tested at three levels:
  1. AgentConfig dataclass has the new field with a None default.
  2. Approval gate in _execute_tool flips yolo_mode off after TTL.
  3. TG /yolo on <minutes> command sets yolo_until.
"""
from __future__ import annotations

import asyncio
import collections
import time
from typing import AsyncIterator
from unittest.mock import MagicMock

import pytest


# ── 1. AgentConfig field ─────────────────────────────────────────────


def test_agent_config_has_yolo_until_default_none():
    from cogitum.core.agent import AgentConfig
    cfg = AgentConfig()
    assert hasattr(cfg, "yolo_until")
    assert cfg.yolo_until is None


# ── 2. Approval gate auto-disables yolo on TTL expiry ────────────────


class _FakeMesh:
    providers: dict = {}

    async def stream(self, req):  # pragma: no cover
        from cogitum.core.events import ChunkKind, StreamChunk
        yield StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn")

    async def aclose(self):  # pragma: no cover
        return None


class _FakeRegistry:
    def to_openai(self, tags=None):
        return []

    def names(self):
        return []

    async def execute(self, name, args):
        return f"ran {name}"


@pytest.mark.asyncio
async def test_yolo_ttl_expiry_flips_mode_off_in_gate():
    """yolo_until in the past + a danger tool → gate flips yolo off
    AND queues an approval request (just like normal yolo=off)."""
    from cogitum.core.agent import Agent, AgentConfig
    from cogitum.core.events import ToolCallPart

    cfg = AgentConfig(model="x/y", yolo_mode=True, yolo_until=time.monotonic() - 60.0)
    agent = Agent(mesh=_FakeMesh(), registry=_FakeRegistry(), config=cfg)
    agent._approval_queue = asyncio.Queue()

    tc = ToolCallPart(
        id="c1", name="terminal", arguments={"command": "rm -rf /tmp/foo"},
    )

    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(agent._execute_tool(tc, turn=1, queue=queue))
    await asyncio.sleep(0.05)

    # Gate must NOT short-circuit — yolo expired.
    assert not task.done(), (
        "TTL expired but gate still ran tool without prompting"
    )
    # And yolo_mode is now False (lazy expiry).
    assert agent.cfg.yolo_mode is False
    assert agent.cfg.yolo_until is None

    # Clean up.
    agent.submit_approval(tc.id, "approve")
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_yolo_ttl_active_still_skips_gate():
    """yolo_until in the FUTURE → gate stays short-circuited, tool
    runs without approval."""
    from cogitum.core.agent import Agent, AgentConfig
    from cogitum.core.events import ToolCallPart

    cfg = AgentConfig(
        model="x/y", yolo_mode=True, yolo_until=time.monotonic() + 600.0,
    )
    agent = Agent(mesh=_FakeMesh(), registry=_FakeRegistry(), config=cfg)
    agent._approval_queue = asyncio.Queue()

    tc = ToolCallPart(
        id="c2", name="terminal", arguments={"command": "rm -rf /tmp/foo"},
    )
    queue: asyncio.Queue = asyncio.Queue()
    result = await asyncio.wait_for(
        agent._execute_tool(tc, turn=1, queue=queue), timeout=2.0,
    )
    assert "ran terminal" in result
    assert agent.cfg.yolo_mode is True  # still active
    assert agent.cfg.yolo_until is not None  # still set


# ── 3. TG /yolo on <minutes> sets yolo_until ─────────────────────────


class _FakeAPI:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))


class _FakeCfg:
    def __init__(self) -> None:
        self.system = "BASE"
        self.model = "x/y"
        self.yolo_mode = False
        self.yolo_until = None


class _FakeAgent:
    def __init__(self) -> None:
        self.cfg = _FakeCfg()


class _FakeConfig:
    def __init__(self) -> None:
        self.allowed_user_id = 1
        self.allowed_chat_ids: list[int] = []

    def can_respond(self, *, user_id, chat_id):
        return True


class _Session:
    def __init__(self) -> None:
        self.chat_id = 1
        self.history: list = []
        self.session_id = None
        self.agent_task = None

    @property
    def is_busy(self):
        return False


def _make_bot():
    from cogitum.gateway.telegram import CogitumBot
    bot = CogitumBot.__new__(CogitumBot)
    bot.config = _FakeConfig()  # type: ignore
    bot.api = _FakeAPI()  # type: ignore
    bot.agent = _FakeAgent()  # type: ignore
    bot.sessions = {}
    bot.mesh = MagicMock()
    bot._pre_godmode_system = None
    bot._approval_token_to_call_id = collections.OrderedDict()
    bot._approval_token_max = 64
    bot._seen_callbacks = collections.OrderedDict()
    bot._seen_callbacks_max = 256
    return bot


@pytest.mark.asyncio
async def test_tg_yolo_on_with_ttl_sets_yolo_until():
    bot = _make_bot()
    msg = {"chat": {"id": 1}, "from": {"id": 1}, "message_id": 1}
    before = time.monotonic()

    await bot._handle_command("/yolo on 30", _Session(), msg)

    assert bot.agent.cfg.yolo_mode is True
    assert bot.agent.cfg.yolo_until is not None
    # 30 min ahead, ±10s tolerance for slow CI.
    expected = before + 30 * 60
    assert abs(bot.agent.cfg.yolo_until - expected) < 10.0


@pytest.mark.asyncio
async def test_tg_yolo_off_clears_yolo_until():
    bot = _make_bot()
    bot.agent.cfg.yolo_mode = True
    bot.agent.cfg.yolo_until = time.monotonic() + 600.0

    msg = {"chat": {"id": 1}, "from": {"id": 1}, "message_id": 1}
    await bot._handle_command("/yolo off", _Session(), msg)

    assert bot.agent.cfg.yolo_mode is False
    assert bot.agent.cfg.yolo_until is None


@pytest.mark.asyncio
async def test_tg_yolo_on_without_ttl_clears_old_until():
    """Re-arming yolo without a TTL should NOT inherit a stale deadline."""
    bot = _make_bot()
    # Pretend a previous /yolo on 30 left an hour of headroom.
    bot.agent.cfg.yolo_mode = False
    bot.agent.cfg.yolo_until = time.monotonic() + 3600.0

    msg = {"chat": {"id": 1}, "from": {"id": 1}, "message_id": 1}
    await bot._handle_command("/yolo on", _Session(), msg)

    assert bot.agent.cfg.yolo_mode is True
    assert bot.agent.cfg.yolo_until is None


@pytest.mark.asyncio
async def test_tg_yolo_on_with_bad_ttl_rejects():
    bot = _make_bot()
    msg = {"chat": {"id": 1}, "from": {"id": 1}, "message_id": 1}

    await bot._handle_command("/yolo on abc", _Session(), msg)

    assert bot.agent.cfg.yolo_mode is False, "bad TTL must NOT enable yolo"
    text = "\n".join(t for _, t, _ in bot.api.sent)
    assert "usage" in text.lower()
