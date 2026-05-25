"""Regression test for P0-2 (audit_tools_history.md).

When ``_compact_context`` summarises the head of the conversation, it
flattens every Message into a text dump for the summariser model. The
old implementation rendered each ``ToolResultPart`` as a bare line
``[tool_result]: <body>`` — losing the structural pairing with the
``ToolCallPart`` that produced it. The summariser then saw
``[tool_result]: 42`` without any way to know which tool was called or
with which arguments.

The fix walks the head once, builds a ``tool_call_id → (name, args)``
index, and renders every result with that context inline so the
pairing survives the flatten.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from cogitum.core.agent import Agent, AgentConfig, _COMPACTION_KEEP_TAIL
from cogitum.core.events import (
    ChunkKind,
    Message,
    StreamChunk,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)


class _CapturingMesh:
    """Mesh stub that records the compaction prompt the agent built."""

    def __init__(self) -> None:
        self.captured_prompt: str | None = None
        self.providers: dict = {}

    def resolve(self, ref: str):  # pragma: no cover
        return []

    def list_resolved(self):  # pragma: no cover
        return []

    async def stream(self, req) -> AsyncIterator[StreamChunk]:
        # Pull the user-prompt text out of the StreamRequest the agent
        # passed in — that's exactly the rendered head dump we want
        # to assert on.
        assert req.messages, "compaction must build a non-empty prompt"
        first_msg = req.messages[0]
        assert first_msg.role == "user"
        assert first_msg.parts and isinstance(first_msg.parts[0], TextPart)
        self.captured_prompt = first_msg.parts[0].text
        yield StreamChunk(kind=ChunkKind.TEXT, text="STUB_SUMMARY")
        yield StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn")

    async def aclose(self) -> None:  # pragma: no cover
        return None


class _Registry:
    def to_openai(self, tags=None):  # pragma: no cover
        return []

    def names(self):  # pragma: no cover
        return []


def _user(text: str) -> Message:
    return Message(role="user", parts=[TextPart(text=text)])


def _asst_call(call_id: str, name: str, args: dict) -> Message:
    return Message(
        role="assistant",
        parts=[ToolCallPart(id=call_id, name=name, arguments=args)],
    )


def _tool(call_id: str, content: str) -> Message:
    return Message(
        role="tool",
        parts=[ToolResultPart(tool_call_id=call_id, content=content, is_error=False)],
    )


@pytest.mark.asyncio
async def test_compaction_renders_tool_call_alongside_result() -> None:
    """The compaction prompt must include the originating tool name and
    args next to every tool_result so the summariser can attribute the
    output to a specific call."""
    mesh = _CapturingMesh()
    agent = Agent(
        mesh=mesh,
        registry=_Registry(),
        config=AgentConfig(model="fake/model"),
    )

    # Build a head with tool call/result pairs followed by enough
    # filler tail messages that compaction triggers (>KEEP_TAIL items).
    msgs: list[Message] = [
        _user("start working"),
        _asst_call("call-001", "read_file", {"path": "/etc/hosts"}),
        _tool("call-001", "127.0.0.1 localhost\n::1 localhost\n"),
        _asst_call("call-002", "terminal", {"command": "uname -a"}),
        _tool("call-002", "Linux box 6.1.0 x86_64 GNU/Linux"),
    ]
    # Filler so naive split lands inside the user-only filler region
    # and the head above is included verbatim.
    msgs.extend(_user(f"filler-{i}") for i in range(_COMPACTION_KEEP_TAIL + 4))

    result = await agent._compact_context(msgs, asyncio.Queue())
    assert result is not msgs, "compaction must run on this buffer size"
    assert mesh.captured_prompt is not None

    prompt = mesh.captured_prompt

    # Both tool_call ids appear and are paired with their tool name.
    assert "call-001" in prompt
    assert "call-002" in prompt
    # The originating tool name is rendered next to the result, not
    # only on the tool_call line, so the summariser sees the pairing
    # even if it skims.
    assert "tool_result for read_file" in prompt, (
        "tool_result must include the tool name that produced it; got:\n"
        + prompt
    )
    assert "tool_result for terminal" in prompt, prompt
    # And the body is preserved.
    assert "127.0.0.1 localhost" in prompt
    assert "Linux box 6.1.0" in prompt
    # The args preview is part of the result header so the summariser
    # can tell apart two calls of the same tool.
    assert "/etc/hosts" in prompt
    assert "uname -a" in prompt


@pytest.mark.asyncio
async def test_compaction_pairs_distinguish_repeated_tool_calls() -> None:
    """Two read_file calls in a row must be distinguishable in the
    summary by their args — not merged into a generic
    ``[tool_result]: ...`` blob."""
    mesh = _CapturingMesh()
    agent = Agent(
        mesh=mesh,
        registry=_Registry(),
        config=AgentConfig(model="fake/model"),
    )

    msgs: list[Message] = [
        _user("scan two files"),
        _asst_call("c1", "read_file", {"path": "/tmp/a.txt"}),
        _tool("c1", "alpha-content"),
        _asst_call("c2", "read_file", {"path": "/tmp/b.txt"}),
        _tool("c2", "beta-content"),
    ]
    msgs.extend(_user(f"filler-{i}") for i in range(_COMPACTION_KEEP_TAIL + 4))

    await agent._compact_context(msgs, asyncio.Queue())
    prompt = mesh.captured_prompt or ""

    # Each result line independently mentions the path it came from.
    a_idx = prompt.find("alpha-content")
    b_idx = prompt.find("beta-content")
    assert a_idx > 0 and b_idx > 0

    # Find the result-header preceding each body.
    a_header = prompt.rfind("tool_result for", 0, a_idx)
    b_header = prompt.rfind("tool_result for", 0, b_idx)
    assert a_header >= 0 and b_header >= 0
    a_segment = prompt[a_header:a_idx]
    b_segment = prompt[b_header:b_idx]
    assert "/tmp/a.txt" in a_segment, a_segment
    assert "/tmp/b.txt" in b_segment, b_segment


@pytest.mark.asyncio
async def test_compaction_orphan_result_keeps_id() -> None:
    """If a tool_result lands without its tool_call (orphan after
    earlier compaction or a sanitiser stub) we still emit something
    the summariser can anchor on — namely the tool_call_id."""
    mesh = _CapturingMesh()
    agent = Agent(
        mesh=mesh,
        registry=_Registry(),
        config=AgentConfig(model="fake/model"),
    )

    # Orphan tool_result — no preceding tool_call with matching id.
    msgs: list[Message] = [
        _user("kickoff"),
        _tool("orphan-xyz", "stale-data"),
    ]
    msgs.extend(_user(f"filler-{i}") for i in range(_COMPACTION_KEEP_TAIL + 4))

    await agent._compact_context(msgs, asyncio.Queue())
    prompt = mesh.captured_prompt or ""
    assert "orphan-xyz" in prompt, (
        "orphan tool_result must still surface its id so the summary "
        "can reference it"
    )
    assert "stale-data" in prompt
