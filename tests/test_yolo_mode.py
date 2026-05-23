"""Tests for yolo_mode in AgentConfig.

YOLO bypasses the approval queue for medium/danger tools. The agent
runs fully autonomous: terminal commands, file writes, network calls
all execute without prompting the user. Toggled per-session via /yolo.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from cogitum.core.agent import Agent, AgentConfig
from cogitum.core.events import ChunkKind, StreamChunk


class _FakeMesh:
    providers: dict = {}

    def resolve(self, ref):  # pragma: no cover
        return []

    def list_resolved(self):  # pragma: no cover
        return []

    async def stream(self, req) -> AsyncIterator[StreamChunk]:  # pragma: no cover
        yield StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn")

    async def aclose(self) -> None:  # pragma: no cover
        return None


class _FakeRegistry:
    def to_openai(self, tags=None):  # pragma: no cover
        return []

    def names(self):  # pragma: no cover
        return []

    async def execute(self, name, args):
        # Pretend execution succeeds — we only care about the
        # approval-gate path, not the result.
        return f"executed {name}({args})"


def _agent(*, yolo: bool) -> Agent:
    return Agent(
        mesh=_FakeMesh(),
        registry=_FakeRegistry(),
        config=AgentConfig(model="fake/model", yolo_mode=yolo),
    )


def test_yolo_default_is_off() -> None:
    """Sanity: yolo_mode defaults to False so existing flows keep
    asking for approval."""
    cfg = AgentConfig()
    assert cfg.yolo_mode is False


@pytest.mark.asyncio
async def test_yolo_off_blocks_dangerous_tool_until_approval() -> None:
    """With yolo OFF and an approval queue present, a dangerous tool
    must wait for user decision before executing."""
    agent = _agent(yolo=False)
    agent._approval_queue = asyncio.Queue()  # non-None gates the call

    from cogitum.core.events import ToolCallPart

    tc = ToolCallPart(
        id="c1",
        name="terminal",
        arguments={"command": "rm -rf /tmp/foo"},  # classified medium/danger
    )

    # Fire the tool execution; it should hang waiting for approval.
    task = asyncio.create_task(agent._execute_tool(tc, turn=1, queue=asyncio.Queue()))
    await asyncio.sleep(0.05)
    assert not task.done(), "non-yolo run must wait on approval queue"
    # Send approval to unblock and let it finish.
    agent._approval_queue.put_nowait("approve")
    result = await asyncio.wait_for(task, timeout=2.0)
    assert "executed" in result


@pytest.mark.asyncio
async def test_yolo_on_executes_dangerous_tool_immediately() -> None:
    """With yolo ON, the same dangerous tool runs immediately —
    approval queue is not consulted at all."""
    agent = _agent(yolo=True)
    agent._approval_queue = asyncio.Queue()  # present but bypassed

    from cogitum.core.events import ToolCallPart

    tc = ToolCallPart(
        id="c1",
        name="terminal",
        arguments={"command": "rm -rf /tmp/foo"},
    )

    result = await asyncio.wait_for(
        agent._execute_tool(tc, turn=1, queue=asyncio.Queue()),
        timeout=2.0,
    )
    assert "executed" in result
    # Approval queue must remain untouched
    assert agent._approval_queue.empty()
