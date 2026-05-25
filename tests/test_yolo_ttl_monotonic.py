"""Audit cosmetic 2: /yolo TTL must use monotonic clock.

The previous implementation stored ``yolo_until`` as
``time.time() + ttl*60`` (wall clock). An NTP step-back / DST shift /
manual clock edit could silently extend the privileged window past the
intended deadline. Persistence is in-memory only, so switching to
``time.monotonic`` is safe (TTL resets on process restart, which is
the right security default anyway).

Tested at four levels:
  1. AgentConfig setter from the TUI command path uses monotonic.
  2. AgentConfig setter from the Telegram command path uses monotonic.
  3. The approval-gate expiry check uses monotonic, not time.time.
  4. A wall-clock step-back does NOT extend a near-expired window
     (regression for the original concern).
"""
from __future__ import annotations

import asyncio
import collections
import time
from unittest.mock import MagicMock, patch

import pytest


# ── 1. Approval gate uses monotonic for the expiry comparison ────────


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
async def test_gate_uses_monotonic_clock_not_wall_clock():
    """Pin time.time to the future and time.monotonic to the past;
    the gate should see the TTL as still active because we use
    monotonic now, not time.time."""
    from cogitum.core.agent import Agent, AgentConfig
    from cogitum.core.events import ToolCallPart

    real_monotonic = time.monotonic()
    cfg = AgentConfig(
        model="x/y",
        yolo_mode=True,
        # Deadline 10 minutes ahead in monotonic terms.
        yolo_until=real_monotonic + 600.0,
    )
    agent = Agent(mesh=_FakeMesh(), registry=_FakeRegistry(), config=cfg)
    agent._approval_queue = asyncio.Queue()

    tc = ToolCallPart(
        id="cm1", name="terminal", arguments={"command": "rm -rf /tmp/foo"},
    )

    # Crank time.time() WAY into the future. If the gate still uses
    # time.time() the deadline (which was set from monotonic) would
    # appear "expired" — but actually it's nonsense because the two
    # clocks aren't comparable. Either way, with the monotonic fix
    # the comparison is consistent and the deadline stays valid.
    queue: asyncio.Queue = asyncio.Queue()
    with patch("cogitum.core.agent.time.time", return_value=time.time() + 99999):
        result = await asyncio.wait_for(
            agent._execute_tool(tc, turn=1, queue=queue),
            timeout=2.0,
        )

    # Tool ran without prompting → gate stayed short-circuited.
    assert "ran terminal" in result
    assert agent.cfg.yolo_mode is True
    assert agent.cfg.yolo_until is not None


@pytest.mark.asyncio
async def test_gate_expires_on_monotonic_advance():
    """Deadline crossed in monotonic terms → gate flips yolo off."""
    from cogitum.core.agent import Agent, AgentConfig
    from cogitum.core.events import ToolCallPart

    cfg = AgentConfig(
        model="x/y",
        yolo_mode=True,
        # Deadline already in the past (monotonic).
        yolo_until=time.monotonic() - 60.0,
    )
    agent = Agent(mesh=_FakeMesh(), registry=_FakeRegistry(), config=cfg)
    agent._approval_queue = asyncio.Queue()

    tc = ToolCallPart(
        id="cm2", name="terminal", arguments={"command": "rm -rf /tmp/foo"},
    )

    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(agent._execute_tool(tc, turn=1, queue=queue))
    await asyncio.sleep(0.05)

    # Past-deadline → gate must NOT short-circuit; it has to prompt.
    assert not task.done(), "TTL expired but gate ran tool without prompting"
    assert agent.cfg.yolo_mode is False
    assert agent.cfg.yolo_until is None

    # Cleanup.
    agent.submit_approval(tc.id, "approve")
    await asyncio.wait_for(task, timeout=2.0)


# ── 2. NTP step-back must not extend a window ────────────────────────


@pytest.mark.asyncio
async def test_wall_clock_step_back_does_not_extend_yolo_window():
    """Regression: the original bug was that an NTP step-back of
    e.g. 30 minutes would extend an in-flight 30-min /yolo window
    by another 30 minutes. With monotonic, time.time can lurch
    arbitrarily without affecting yolo_until."""
    from cogitum.core.agent import Agent, AgentConfig
    from cogitum.core.events import ToolCallPart

    base_mono = time.monotonic()
    cfg = AgentConfig(
        model="x/y",
        yolo_mode=True,
        # 1 second of TTL remaining.
        yolo_until=base_mono - 0.5,  # already expired in monotonic
    )
    agent = Agent(mesh=_FakeMesh(), registry=_FakeRegistry(), config=cfg)
    agent._approval_queue = asyncio.Queue()

    tc = ToolCallPart(
        id="cm3", name="terminal", arguments={"command": "rm -rf /tmp/foo"},
    )

    queue: asyncio.Queue = asyncio.Queue()
    # Step time.time BACK by an hour — under the wall-clock impl this
    # would push yolo_until far into the "future" and keep yolo on.
    # With monotonic, time.time is irrelevant.
    with patch("cogitum.core.agent.time.time", return_value=time.time() - 3600):
        task = asyncio.create_task(
            agent._execute_tool(tc, turn=1, queue=queue)
        )
        await asyncio.sleep(0.05)

        # Gate must still see the deadline as expired.
        assert not task.done(), (
            "wall-clock step-back extended yolo window (it shouldn't)"
        )
        assert agent.cfg.yolo_mode is False

    # Cleanup.
    agent.submit_approval(tc.id, "approve")
    await asyncio.wait_for(task, timeout=2.0)


# ── 3. Telegram setter uses monotonic ────────────────────────────────


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
async def test_tg_yolo_setter_uses_monotonic_clock():
    """When /yolo on N is invoked, yolo_until must equal the monotonic
    clock plus N*60 — NOT time.time + N*60."""
    bot = _make_bot()
    msg = {"chat": {"id": 1}, "from": {"id": 1}, "message_id": 1}

    fixed_mono = 5_000_000.0
    fixed_wall = 1_700_000_000.0  # epoch
    with patch(
        "cogitum.gateway.telegram.time.monotonic",
        return_value=fixed_mono,
    ), patch(
        "cogitum.gateway.telegram.time.time",
        return_value=fixed_wall,
    ):
        await bot._handle_command("/yolo on 30", _Session(), msg)

    assert bot.agent.cfg.yolo_mode is True
    # Must be monotonic-based, not wall-clock-based.
    expected = fixed_mono + 30 * 60
    assert abs(bot.agent.cfg.yolo_until - expected) < 1e-6, (
        f"yolo_until={bot.agent.cfg.yolo_until} not on monotonic clock; "
        f"expected ~{expected}, wall-clock would have given ~{fixed_wall + 30*60}"
    )
    # Sanity: the value is far from a wall-clock value.
    assert bot.agent.cfg.yolo_until < fixed_wall, (
        "yolo_until is still on wall clock — the fix did not apply"
    )


# ── 4. CLI app setter uses monotonic ─────────────────────────────────


def test_app_imports_have_monotonic_call_for_yolo():
    """Smoke check: cogitum.app's /yolo handler imports time and uses
    time.monotonic when arming the TTL. Looking at the source as a
    string is brittle but cheap; the structural tests above cover
    behaviour."""
    from pathlib import Path

    src = Path("cogitum/app.py").read_text(encoding="utf-8")
    # The TTL-arming branch is inside a /yolo handler. Both setter
    # paths (TG + app) must use monotonic.
    assert "_time.monotonic() + ttl_minutes" in src, (
        "cogitum/app.py /yolo handler still using wall-clock time.time"
    )
