"""Tests for yolo_mode in AgentConfig.

YOLO bypasses the approval queue for medium/danger tools. The agent
runs fully autonomous: terminal commands, file writes, network calls
all execute without prompting the user. Toggled per-session via /yolo.
"""

from __future__ import annotations

import asyncio
import inspect
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
    assert agent.submit_approval(tc.id, "approve")
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


# ── Defense-in-depth: race-condition guard in the consumer ───────────


def test_app_yolo_consumer_short_circuits_approval_request_widget() -> None:
    """The TUI consumer's AgentApprovalRequest branch must
    auto-approve when yolo is currently on, even if a stale request
    landed in the queue from a moment when yolo was off.

    Race scenario:
      1. Tool A (medium-danger) starts.
      2. Agent's _execute_tool queues AgentApprovalRequest.
      3. User types /yolo on between emit and drain.
      4. Drain pulls the stale request — under naive code, modal
         opens despite yolo being on. Under the guarded code, the
         consumer auto-puts 'approve' to the agent's queue and
         skips the widget.

    AST-walk check: the consumer's elif branch for
    AgentApprovalRequest must contain a `cfg.yolo_mode` check that
    auto-approves and returns/continues before reaching the modal.
    """
    import ast
    import textwrap
    from cogitum import app as app_module
    src = inspect.getsource(app_module)
    tree = ast.parse(src)

    # Find the AgentApprovalRequest handler block. We look for the
    # string 'AgentApprovalRequest' as part of an isinstance call,
    # and verify that the immediately-following body contains both
    # `yolo_mode` and a `put_nowait("approve")` call.
    found_branch_with_guard = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        # Look for `isinstance(event, AgentApprovalRequest)` shape.
        test = node.test
        if not isinstance(test, ast.Call):
            continue
        if not (
            isinstance(test.func, ast.Name) and test.func.id == "isinstance"
        ):
            continue
        if len(test.args) != 2:
            continue
        klass = test.args[1]
        if not (isinstance(klass, ast.Name) and klass.id == "AgentApprovalRequest"):
            continue
        # We're inside the right branch. Walk its body.
        body_src = ast.unparse(node)
        if "yolo_mode" in body_src and "approve" in body_src:
            found_branch_with_guard = True
            break

    assert found_branch_with_guard, (
        "TUI AgentApprovalRequest handler must check yolo_mode and "
        "auto-approve when yolo is on; otherwise a stale approval "
        "request from before /yolo on draws a modal anyway"
    )


def test_telegram_yolo_consumer_short_circuits_approval_request() -> None:
    """Same defense-in-depth contract on the TG-gateway side."""
    import ast
    from cogitum.gateway import telegram as tg_module
    src = inspect.getsource(tg_module)
    tree = ast.parse(src)

    found_branch_with_guard = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not isinstance(test, ast.Call):
            continue
        if not (
            isinstance(test.func, ast.Name) and test.func.id == "isinstance"
        ):
            continue
        if len(test.args) != 2:
            continue
        klass = test.args[1]
        if not (isinstance(klass, ast.Name) and klass.id == "AgentApprovalRequest"):
            continue
        body_src = ast.unparse(node)
        if "yolo_mode" in body_src and "approve" in body_src:
            found_branch_with_guard = True
            break

    assert found_branch_with_guard, (
        "TG AgentApprovalRequest handler must check yolo_mode and "
        "auto-approve when yolo is on; otherwise the bot draws a "
        "Sanction inline-keyboard despite yolo being on"
    )
