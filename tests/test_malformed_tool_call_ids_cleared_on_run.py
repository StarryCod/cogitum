"""R3 fix (audit gap #10): ``self._malformed_tool_call_ids`` must be
cleared at the top of ``Agent.run()``.

Background. The dict is written when a streamed tool-call's JSON
arguments fail to parse (entry: ``call_id -> "ERROR: invalid JSON …"``).
``_execute_tool`` then pops the entry and short-circuits, returning
that error string to the model instead of running the tool.

Failure mode (pre-fix). If the agent loop is cancelled (user /stop,
``CancelledError`` from outer task, mid-stream exception) BEFORE
``_execute_tool`` got a chance to consume the entry, the parse-err
record stays in the dict for the lifetime of the Agent.

A subsequent ``run()`` against a provider that emits sequential ids
(vLLM-style ``call_0, call_1, ...``) can collide with the stale id and
have its FIRST legitimate tool call short-circuited with a misleading
"invalid JSON" diagnostic. Hard to reproduce in production, but
deterministically reproducible in a unit test.

Fix: ``self._malformed_tool_call_ids.clear()`` at the top of
``Agent.run()`` (right after capturing ``self._main_loop``).

These tests assert:
  1. The clear happens at run() start (basic invariant).
  2. A stale entry from a cancelled/leaked previous run does NOT
     block a real tool call on the next run.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from cogitum.core.agent import Agent, AgentConfig
from cogitum.core.events import (
    ChunkKind,
    Message,
    StreamChunk,
    ToolCallPart,
)


# ─────────────────────────────────────────────────────────────────────────
# Test scaffolding (mirrors tests/test_fallback_summary.py shapes)
# ─────────────────────────────────────────────────────────────────────────


class _ScriptedMesh:
    """Pre-recorded streaming responses; one inner list per stream call."""

    def __init__(self, responses: list[list[StreamChunk]]) -> None:
        self._responses = responses
        self._call_idx = 0
        self.providers: dict = {}

    def resolve(self, ref):  # pragma: no cover
        return []

    def list_resolved(self):  # pragma: no cover
        return []

    async def stream(self, req) -> AsyncIterator[StreamChunk]:
        if self._call_idx >= len(self._responses):
            yield StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn")
            return
        chunks = self._responses[self._call_idx]
        self._call_idx += 1
        for c in chunks:
            yield c

    async def aclose(self) -> None:  # pragma: no cover
        return None


class _RecordingRegistry:
    """Records every execute() call so tests can assert the tool ran."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def to_openai(self, tags=None):  # pragma: no cover
        return [{
            "type": "function",
            "function": {"name": "echo", "description": "echo", "parameters": {}},
        }]

    def names(self):  # pragma: no cover
        return ["echo"]

    async def execute(self, name: str, args: dict) -> str:
        self.calls.append((name, args))
        return f"executed {name}"


def _make_agent(responses: list[list[StreamChunk]]) -> tuple[Agent, _RecordingRegistry]:
    reg = _RecordingRegistry()
    agent = Agent(
        mesh=_ScriptedMesh(responses),
        registry=reg,
        config=AgentConfig(model="fake/model", max_turns=2, tools_enabled=True),
    )
    return agent, reg


# ─────────────────────────────────────────────────────────────────────────
# Test 1 — clear() invariant at run() start
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_start_clears_stale_malformed_ids():
    """A stale entry written before run() is wiped at the top of run()."""
    # Stream emits a single STOP — no tool calls, just an empty turn.
    agent, _ = _make_agent([
        [StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn")],
    ])

    # Seed a stale entry from a hypothetical previous cancelled run.
    agent._malformed_tool_call_ids["call_0"] = "ERROR: stale leftover from prior run"
    assert agent._malformed_tool_call_ids == {"call_0": "ERROR: stale leftover from prior run"}

    await agent.run(user_message="hi")

    # The clear at the top of run() wiped it — even though no tool
    # call ever consumed it.
    assert agent._malformed_tool_call_ids == {}


# ─────────────────────────────────────────────────────────────────────────
# Test 2 — stale id does NOT block a real tool call in the next run
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_malformed_id_does_not_block_fresh_tool_call():
    """End-to-end: a stale entry for ``call_0`` from a leaked prior
    run must NOT short-circuit a brand-new, well-formed ``call_0``
    tool call in the next run.

    Reproduces the vLLM-style sequential-id collision scenario.
    """
    # Run #2 stream: the model emits one tool call (id=call_0, valid
    # JSON args), tool runs, then a closing text turn.
    tool_call_chunks = [
        StreamChunk(
            kind=ChunkKind.TOOL_CALL_DONE,
            tool_call_id="call_0",
            tool_call_name="echo",
            tool_call_args={"msg": "hello"},
        ),
        StreamChunk(kind=ChunkKind.STOP, stop_reason="tool_use"),
    ]
    closing_text = [
        StreamChunk(kind=ChunkKind.TEXT, text="done"),
        StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn"),
    ]
    agent, registry = _make_agent([tool_call_chunks, closing_text])

    # Simulate the bug pre-condition: a previous run wrote a parse-err
    # for the SAME id (call_0) and was cancelled before _execute_tool
    # got to pop it. The dict is the only observable Agent-level state
    # that crosses runs.
    agent._malformed_tool_call_ids["call_0"] = (
        "ERROR: invalid JSON in tool arguments: stale | preview: {q:"
    )

    history = await agent.run(user_message="please run echo")

    # Debug aid: print the history shape on failure
    history_shape = [(m.role, [type(p).__name__ for p in m.parts]) for m in history]

    # Critical assertions:
    # 1. The stale entry was cleared at run() start, so it never
    #    short-circuited the real tool call.
    assert "call_0" not in agent._malformed_tool_call_ids

    # 2. The real tool actually executed (registry recorded the call).
    #    THIS is the headline bug-fix assertion. If the stale entry
    #    had blocked, registry.calls would be empty.
    assert registry.calls == [("echo", {"msg": "hello"})], (
        f"expected the fresh call_0 to execute, got registry.calls={registry.calls}; "
        f"history={history_shape}"
    )


# ─────────────────────────────────────────────────────────────────────────
# Test 3 — a malformed call WITHIN a run still works (regression guard)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_malformed_id_within_same_run_still_short_circuits():
    """Confirm we didn't break the in-run malformed-id flow: an entry
    written DURING the current run must still short-circuit
    _execute_tool.

    The clear at run() start fires BEFORE any in-run write, so an entry
    written during streaming survives until _execute_tool consumes it.
    """
    agent, registry = _make_agent([
        [StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn")],
    ])

    # Start a no-op run() to trigger the clear.
    await agent.run(user_message="warmup")
    assert agent._malformed_tool_call_ids == {}

    # Now simulate the streamer setting a parse-err MID-RUN by writing
    # to the dict and immediately calling _execute_tool, which is what
    # happens inside the real loop after a JSONDecodeError.
    agent._malformed_tool_call_ids["call_42"] = "ERROR: invalid JSON in tool arguments: foo"
    tc = ToolCallPart(id="call_42", name="echo", arguments={})
    result = await agent._execute_tool(tc, turn=1)

    assert result == "ERROR: invalid JSON in tool arguments: foo"
    # Tool was NOT executed — short-circuit fired correctly.
    assert registry.calls == []
    # Entry was popped after consumption.
    assert "call_42" not in agent._malformed_tool_call_ids
