"""Audit M1: errors from delegated subagents must surface to the parent.

The tool-flow audit found that DELEGATE_WORKERS / DELEGATE_EXPERTS
sentinels were dispatched correctly but the result classification
hardcoded ``is_error = False`` regardless of payload content. A
crashed swarm came back as a success, so the parent model couldn't
trigger retries / escalations.

The fix mirrors LEGION_RUN: detect ``ERROR:`` / ``REJECTED:`` /
``Internal error`` prefixes and set ``is_error=True`` on the
ToolResultPart and the streamed AgentToolResult.
"""
from __future__ import annotations

import asyncio
import pytest
from typing import AsyncIterator

from cogitum.core.events import (
    ChunkKind,
    StreamChunk,
)


class _Mesh:
    """Two-turn mock: turn 1 emits a single tool_call, turn 2 stops."""

    def __init__(self, tool_name: str) -> None:
        self._tool_name = tool_name
        self._turn = 0

    def list_resolved(self):
        return []

    def resolve(self, qid):
        return None

    async def stream(self, req) -> AsyncIterator[StreamChunk]:
        self._turn += 1
        if self._turn == 1:
            yield StreamChunk(
                kind=ChunkKind.TOOL_CALL_DONE,
                tool_call_id="dc-1",
                tool_call_name=self._tool_name,
                tool_call_args={},
            )
            yield StreamChunk(kind=ChunkKind.STOP, stop_reason="tool_use")
        else:
            yield StreamChunk(kind=ChunkKind.TEXT, text="done")
            yield StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn")


class _Reg:
    """Minimal registry: one tool that returns a sentinel string."""

    def __init__(self, tool_name: str, sentinel: str) -> None:
        self._tool_name = tool_name
        self._sentinel = sentinel

    def to_openai(self, tags=None):
        return [{
            "type": "function",
            "function": {
                "name": self._tool_name,
                "description": "x",
                "parameters": {"type": "object", "properties": {}},
            },
        }]

    def names(self):
        return [self._tool_name]

    async def execute(self, name, args):
        return self._sentinel


def _make_agent(tool_name: str, sentinel: str):
    from cogitum.core.agent import Agent, AgentConfig
    cfg = AgentConfig(model="mock", platform="cli")
    cfg.tools_enabled = True
    return Agent(
        mesh=_Mesh(tool_name),
        registry=_Reg(tool_name, sentinel),
        config=cfg,
    )


def _drain(q: asyncio.Queue) -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


@pytest.mark.asyncio
async def test_delegate_workers_error_prefix_propagates():
    from cogitum.core.agent import AgentToolResult

    agent = _make_agent("delegate_workers", "DELEGATE_WORKERS:{\"tasks\":[]}")

    async def fake_run(payload_json: str) -> str:
        return "ERROR: worker pool exploded"

    agent._run_delegate_workers = fake_run  # type: ignore[assignment]

    q: asyncio.Queue = asyncio.Queue()
    await asyncio.wait_for(
        agent.run(user_message="go", queue=q),
        timeout=10.0,
    )

    finals = [
        e for e in _drain(q)
        if isinstance(e, AgentToolResult) and e.tool_name == "delegate_workers"
    ]
    assert finals, "no AgentToolResult emitted for delegate_workers"
    assert finals[-1].error is True, (
        "ERROR: prefix from delegate_workers must surface as error=True; "
        f"got error={[r.error for r in finals]}"
    )
    assert "ERROR:" in finals[-1].result


@pytest.mark.asyncio
async def test_delegate_experts_rejected_prefix_propagates():
    from cogitum.core.agent import AgentToolResult

    agent = _make_agent("delegate_experts", "DELEGATE_EXPERTS:{\"tasks\":[]}")

    async def fake_run(payload_json: str) -> str:
        return "REJECTED: budget exceeded"

    agent._run_delegate_experts = fake_run  # type: ignore[assignment]

    q: asyncio.Queue = asyncio.Queue()
    await asyncio.wait_for(
        agent.run(user_message="go", queue=q),
        timeout=10.0,
    )

    finals = [
        e for e in _drain(q)
        if isinstance(e, AgentToolResult) and e.tool_name == "delegate_experts"
    ]
    assert finals and finals[-1].error is True
    assert "REJECTED:" in finals[-1].result


@pytest.mark.asyncio
async def test_delegate_internal_error_prefix_propagates():
    from cogitum.core.agent import AgentToolResult

    agent = _make_agent("delegate_workers", "DELEGATE_WORKERS:{\"tasks\":[]}")

    async def fake_run(payload_json: str) -> str:
        return "Internal error: subagent OOM"

    agent._run_delegate_workers = fake_run  # type: ignore[assignment]

    q: asyncio.Queue = asyncio.Queue()
    await asyncio.wait_for(
        agent.run(user_message="go", queue=q),
        timeout=10.0,
    )

    finals = [
        e for e in _drain(q)
        if isinstance(e, AgentToolResult) and e.tool_name == "delegate_workers"
    ]
    assert finals and finals[-1].error is True


@pytest.mark.asyncio
async def test_delegate_workers_success_stays_non_error():
    """Sanity: a non-error payload must still come back error=False."""
    from cogitum.core.agent import AgentToolResult

    agent = _make_agent("delegate_workers", "DELEGATE_WORKERS:{\"tasks\":[]}")

    async def fake_run(payload_json: str) -> str:
        return "[alpha] all good\n[beta] also good"

    agent._run_delegate_workers = fake_run  # type: ignore[assignment]

    q: asyncio.Queue = asyncio.Queue()
    await asyncio.wait_for(
        agent.run(user_message="go", queue=q),
        timeout=10.0,
    )

    finals = [
        e for e in _drain(q)
        if isinstance(e, AgentToolResult) and e.tool_name == "delegate_workers"
    ]
    assert finals and finals[-1].error is False


# ── Broader prefix detection (F40) ──────────────────────────────────────
# The ``_result_indicates_error`` helper is the single source of truth
# for "did this delegate result come back as a failure". The set of
# patterns it recognises was widened in F40 to include the swarm
# formatter's "0/N" header and a leaked traceback. These tests pin the
# helper directly AND exercise it through the run loop.


def test_helper_detects_workers_zero_completed():
    from cogitum.core.agent import _result_indicates_error
    assert _result_indicates_error(
        "Workers completed (0/5 success):\n\n[✗] alpha (1.0s):\n  ERROR: x"
    ) is True


def test_helper_detects_experts_zero_completed():
    from cogitum.core.agent import _result_indicates_error
    assert _result_indicates_error(
        "Experts completed (0/3 reviewers):\n…"
    ) is True


def test_helper_detects_traceback_leak():
    from cogitum.core.agent import _result_indicates_error
    leak = (
        "Traceback (most recent call last):\n"
        "  File 'x.py', line 1, in <module>\n"
        "RuntimeError: nope"
    )
    assert _result_indicates_error(leak) is True


def test_helper_detects_raised_exception_substring():
    from cogitum.core.agent import _result_indicates_error
    assert _result_indicates_error(
        "[✗] task-3 (12.4s):\n  task raised an exception: ValueError"
    ) is True


def test_helper_keeps_existing_prefixes():
    from cogitum.core.agent import _result_indicates_error
    assert _result_indicates_error("ERROR: foo") is True
    assert _result_indicates_error("REJECTED: budget") is True
    assert _result_indicates_error("Internal error: x") is True


def test_helper_passes_clean_success():
    from cogitum.core.agent import _result_indicates_error
    assert _result_indicates_error(
        "Workers completed (5/5 success):\n\n[✓] alpha"
    ) is False
    assert _result_indicates_error("[alpha] all good") is False
    assert _result_indicates_error("") is False
    assert _result_indicates_error(None) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_delegate_workers_zero_completed_propagates():
    """End-to-end: a "Workers completed (0/N …)" header from the
    delegate must surface as error=True at the run loop."""
    from cogitum.core.agent import AgentToolResult

    agent = _make_agent("delegate_workers", "DELEGATE_WORKERS:{\"tasks\":[]}")

    async def fake_run(payload_json: str) -> str:
        return (
            "Workers completed (0/3 success):\n\n"
            "[✗] alpha (1.0s):\n  ERROR: provider down\n"
        )

    agent._run_delegate_workers = fake_run  # type: ignore[assignment]

    q: asyncio.Queue = asyncio.Queue()
    await asyncio.wait_for(
        agent.run(user_message="go", queue=q),
        timeout=10.0,
    )

    finals = [
        e for e in _drain(q)
        if isinstance(e, AgentToolResult) and e.tool_name == "delegate_workers"
    ]
    assert finals and finals[-1].error is True, (
        "Workers completed (0/N) header must mark the result as error"
    )


@pytest.mark.asyncio
async def test_delegate_experts_traceback_leak_propagates():
    """A leaked traceback string from the experts branch must be flagged."""
    from cogitum.core.agent import AgentToolResult

    agent = _make_agent("delegate_experts", "DELEGATE_EXPERTS:{\"tasks\":[]}")

    async def fake_run(payload_json: str) -> str:
        return (
            "Traceback (most recent call last):\n"
            "  File 'x.py', line 1, in <module>\n"
            "RuntimeError: leaked"
        )

    agent._run_delegate_experts = fake_run  # type: ignore[assignment]

    q: asyncio.Queue = asyncio.Queue()
    await asyncio.wait_for(
        agent.run(user_message="go", queue=q),
        timeout=10.0,
    )

    finals = [
        e for e in _drain(q)
        if isinstance(e, AgentToolResult) and e.tool_name == "delegate_experts"
    ]
    assert finals and finals[-1].error is True


@pytest.mark.asyncio
async def test_legion_run_uses_same_classifier():
    """LEGION_RUN must use the shared classifier too — a "Workers
    completed (0/N)" payload from a legion run was previously treated
    as success because legion's branch only checked startswith('ERROR:')."""
    from cogitum.core.agent import AgentToolResult

    agent = _make_agent("legion_run", "LEGION_RUN:{\"tasks\":[]}")

    async def fake_run(payload_json: str) -> str:
        return "Workers completed (0/2 success):\n\n[✗] x"

    agent._run_legion = fake_run  # type: ignore[assignment]

    q: asyncio.Queue = asyncio.Queue()
    await asyncio.wait_for(
        agent.run(user_message="go", queue=q),
        timeout=10.0,
    )

    finals = [
        e for e in _drain(q)
        if isinstance(e, AgentToolResult) and e.tool_name == "legion_run"
    ]
    assert finals and finals[-1].error is True
