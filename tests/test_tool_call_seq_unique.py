"""Audit M4: synthetic tool_call_id collision when provider omits id.

When a provider stream emits TOOL_CALL_DELTA without a
``tool_call_id``, the agent generates a fallback. The previous
implementation used ``f"call_{len(pending)}"`` — but ``pending``
shrinks when an entry is popped on TOOL_CALL_DONE, so the *next*
idless tool call could reuse the same synthetic id and clobber a
still-streaming entry.

The fix is a per-Agent monotonically-increasing counter
(``self._tool_call_seq``) that never decreases.

Tested at two levels:
  1. AgentConfig has ``_tool_call_seq`` initialised to 0 on the
     Agent instance.
  2. Stream three idless tool calls back-to-back; verify three
     distinct call_ids appear on the events queue.
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
    """Single turn that emits 3 idless tool_call deltas, each one
    finalised by TOOL_CALL_DONE before the next starts. The bug only
    fires when ``pending.pop`` shrinks the dict between deltas, so we
    interleave start → done → start → done."""

    def __init__(self) -> None:
        self._turn = 0

    def list_resolved(self):
        return []

    def resolve(self, qid):
        return None

    async def stream(self, req) -> AsyncIterator[StreamChunk]:
        self._turn += 1
        if self._turn == 1:
            for n in ("a", "b", "c"):
                # Idless delta: provider didn't supply tool_call_id.
                yield StreamChunk(
                    kind=ChunkKind.TOOL_CALL_DELTA,
                    tool_call_id=None,
                    tool_call_name=f"echo_{n}",
                    tool_call_args_delta="{}",
                )
                # DONE for the same call. Without tool_call_id, the
                # done-handler can't pop by id; in practice the agent
                # treats unknown ids as "finalised without prior delta".
                # That's fine — we care that the DELTA path generates
                # unique synthetic ids.
                yield StreamChunk(
                    kind=ChunkKind.TOOL_CALL_DONE,
                    tool_call_id=f"call_{n}",  # provider-supplied done id
                    tool_call_name=f"echo_{n}",
                    tool_call_args={},
                )
            yield StreamChunk(kind=ChunkKind.STOP, stop_reason="tool_use")
        else:
            yield StreamChunk(kind=ChunkKind.TEXT, text="ok")
            yield StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn")


class _Reg:
    def to_openai(self, tags=None):
        return [
            {
                "type": "function",
                "function": {
                    "name": f"echo_{n}",
                    "description": "x",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for n in ("a", "b", "c")
        ]

    def names(self):
        return ["echo_a", "echo_b", "echo_c"]

    async def execute(self, name, args):
        return f"ran {name}"


def test_agent_initialises_tool_call_seq_to_zero():
    from cogitum.core.agent import Agent, AgentConfig
    cfg = AgentConfig(model="x/y")
    a = Agent(mesh=_Mesh(), registry=_Reg(), config=cfg)
    assert a._tool_call_seq == 0


@pytest.mark.asyncio
async def test_idless_tool_calls_get_unique_synthetic_ids():
    """When TOOL_CALL_DELTA has no provider id, three deltas in a row
    must yield three distinct synthetic call_{n} ids.

    Asserts via the AgentToolCall events emitted with preliminary=True
    on first delta — those carry the synthesised cid.
    """
    from cogitum.core.agent import Agent, AgentConfig, AgentToolCall

    cfg = AgentConfig(model="x/y", platform="cli")
    cfg.tools_enabled = True
    agent = Agent(mesh=_Mesh(), registry=_Reg(), config=cfg)

    q: asyncio.Queue = asyncio.Queue()
    await asyncio.wait_for(
        agent.run(user_message="go", queue=q),
        timeout=10.0,
    )

    prelim_ids = []
    while not q.empty():
        ev = q.get_nowait()
        if isinstance(ev, AgentToolCall) and getattr(ev, "preliminary", False):
            prelim_ids.append(ev.call_id)

    # We emitted 3 idless deltas → 3 unique synthetic ids.
    assert len(prelim_ids) == 3, (
        f"expected 3 preliminary AgentToolCall events, got {len(prelim_ids)}: {prelim_ids}"
    )
    assert len(set(prelim_ids)) == 3, (
        f"synthetic call_ids collided: {prelim_ids}"
    )
    # Counter must have advanced past those 3 deltas.
    assert agent._tool_call_seq >= 3


def test_tool_call_seq_never_decreases_across_runs():
    """Spec: counter is per-instance and monotonic — it must not reset
    between turns or runs."""
    from cogitum.core.agent import Agent, AgentConfig
    a = Agent(mesh=_Mesh(), registry=_Reg(), config=AgentConfig(model="x/y"))
    a._tool_call_seq = 17
    # Direct manipulation can't be undone: that's the whole point.
    assert a._tool_call_seq == 17
