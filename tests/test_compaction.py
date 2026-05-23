"""Tests for the rewritten _compact_context.

Old behaviour wiped the entire history → fake ack pair, with tool_results
hard-truncated at 4 KB. The bug it caused: model lost sight of its own
tool_calls/tool_results immediately after compaction.

New behaviour:
  - tail of recent messages survives verbatim
  - head is summarized into a single user "briefing" message
  - no fake assistant ack
  - if buffer ≤ keep-tail, returns input unchanged
  - tool_use/tool_result pairs are not split across the boundary
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from cogitum.core.agent import (
    Agent,
    AgentConfig,
    _COMPACTION_KEEP_TAIL,
)
from cogitum.core.events import (
    ChunkKind,
    Message,
    StreamChunk,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)


class _FakeMesh:
    """Minimal mesh stub: returns a fixed summary as TEXT chunks."""

    def __init__(self, summary: str = "FAKE_SUMMARY") -> None:
        self._summary = summary
        self.providers: dict = {}

    def resolve(self, ref: str):  # pragma: no cover — not used
        return []

    def list_resolved(self):  # pragma: no cover
        return []

    async def stream(self, req) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(kind=ChunkKind.TEXT, text=self._summary)
        yield StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn")

    async def aclose(self) -> None:  # pragma: no cover
        return None


class _FakeRegistry:
    def to_openai(self, tags=None):  # pragma: no cover
        return []

    def names(self):  # pragma: no cover
        return []


def _make_agent() -> Agent:
    return Agent(
        mesh=_FakeMesh(),
        registry=_FakeRegistry(),
        config=AgentConfig(model="fake/model"),
    )


def _user(text: str) -> Message:
    return Message(role="user", parts=[TextPart(text=text)])


def _asst(text: str, *, tool_calls: list[ToolCallPart] | None = None) -> Message:
    parts: list = [TextPart(text=text)] if text else []
    if tool_calls:
        parts.extend(tool_calls)
    return Message(role="assistant", parts=parts)


def _tool(call_id: str, content: str) -> Message:
    return Message(
        role="tool",
        parts=[ToolResultPart(tool_call_id=call_id, content=content, is_error=False)],
    )


@pytest.mark.asyncio
async def test_short_buffer_unchanged() -> None:
    """If history ≤ keep-tail, compaction returns input as-is."""
    agent = _make_agent()
    msgs = [_user(f"msg-{i}") for i in range(_COMPACTION_KEEP_TAIL - 2)]
    result = await agent._compact_context(msgs, asyncio.Queue())
    assert result is msgs, "short buffer should be returned unchanged"


@pytest.mark.asyncio
async def test_long_buffer_keeps_tail_verbatim() -> None:
    """Long buffer: tail messages must survive byte-for-byte."""
    agent = _make_agent()
    n_total = _COMPACTION_KEEP_TAIL + 10
    msgs = [_user(f"msg-{i}") for i in range(n_total)]
    result = await agent._compact_context(msgs, asyncio.Queue())

    # Result = 1 briefing + tail (last KEEP_TAIL messages)
    assert len(result) == 1 + _COMPACTION_KEEP_TAIL
    # Briefing is a user message
    assert result[0].role == "user"
    assert "BRIEFING" in result[0].parts[0].text
    assert "FAKE_SUMMARY" in result[0].parts[0].text
    # Tail: last KEEP_TAIL of the original list, byte-for-byte
    expected_tail = msgs[-_COMPACTION_KEEP_TAIL:]
    assert result[1:] == expected_tail


@pytest.mark.asyncio
async def test_tool_pair_not_split() -> None:
    """If naive split lands on a tool-role message, walk back to keep
    the assistant message that produced its tool_calls in the same
    chunk. Otherwise we emit an orphan tool_result and the provider
    rejects it."""
    agent = _make_agent()

    # Build: [head ...] [asst tool_call] [tool result] [tail ...]
    # such that the naive split index lands on the tool-role message.
    head_size = 4
    extra_tail = _COMPACTION_KEEP_TAIL - 1  # so split idx hits the tool msg

    head = [_user(f"head-{i}") for i in range(head_size)]
    boundary_asst = _asst(
        "calling tool",
        tool_calls=[ToolCallPart(id="c1", name="terminal", arguments={})],
    )
    boundary_tool = _tool("c1", "tool stdout")
    rest_tail = [_user(f"tail-{i}") for i in range(extra_tail)]

    msgs = head + [boundary_asst, boundary_tool] + rest_tail
    # Sanity: naive split would land on boundary_tool
    naive_idx = len(msgs) - _COMPACTION_KEEP_TAIL
    assert msgs[naive_idx].role == "tool"

    result = await agent._compact_context(msgs, asyncio.Queue())

    # Tail must start with the assistant tool_call, not the orphan tool_result
    tail = result[1:]
    assert tail[0] is boundary_asst, (
        "compaction split a tool_use/tool_result pair — provider would reject this"
    )
    assert tail[1] is boundary_tool


@pytest.mark.asyncio
async def test_no_fake_assistant_ack() -> None:
    """Old impl appended a fake 'Understood. I have the full context.'
    assistant message. New impl must not — assistant turns are facts
    the model treats as its own, fabricating one corrupts state."""
    agent = _make_agent()
    msgs = [_user(f"msg-{i}") for i in range(_COMPACTION_KEEP_TAIL + 5)]
    result = await agent._compact_context(msgs, asyncio.Queue())
    # No assistant message should be the second item — the briefing
    # is a single user message followed directly by the original tail.
    assert result[0].role == "user"
    # Nothing in the result is a fabricated assistant ack
    for m in result:
        for p in m.parts:
            if isinstance(p, TextPart):
                assert "Understood. I have the full context" not in p.text


@pytest.mark.asyncio
async def test_empty_summary_keeps_original() -> None:
    """If the summarizer returns nothing, fall back to the input
    rather than wiping the user's session."""
    agent = _make_agent()
    agent.mesh = _FakeMesh(summary="")  # empty summary
    msgs = [_user(f"msg-{i}") for i in range(_COMPACTION_KEEP_TAIL + 5)]
    result = await agent._compact_context(msgs, asyncio.Queue())
    assert result is msgs


@pytest.mark.asyncio
async def test_compact_now_emits_event_and_returns_tokens() -> None:
    """Manual /compact path must emit AgentCompacted with realistic
    before/after estimates and return the new history tuple.

    NB: this test imports AgentCompacted *inside* the function on
    purpose. ``tests/conftest.py`` wipes every ``cogitum.*`` from
    ``sys.modules`` before each test, so any class object grabbed at
    module top-level becomes a stale reference vs. the freshly
    re-imported module the agent itself uses — isinstance() then
    fails on objects that print as the same class. We check by
    type name instead to dodge the duplicate-class trap entirely.
    """
    agent = _make_agent()

    msgs = [_user("x" * 4000) for _ in range(_COMPACTION_KEEP_TAIL + 8)]

    q: asyncio.Queue = asyncio.Queue()
    new_msgs, before, after = await agent.compact_now(msgs, queue=q)

    # Tokens went down (compaction did something useful).
    assert before > after, (
        f"compact_now should shrink token estimate; before={before} after={after}"
    )
    # Event was emitted with the same numbers. We check via type name
    # rather than isinstance() because tests/conftest.py wipes
    # cogitum.* from sys.modules between tests, so a top-level
    # AgentCompacted import and the agent's own internal class are
    # two different class objects (same fully-qualified name, both
    # alive at once). isinstance() would return False even though
    # they're functionally identical.
    ev = q.get_nowait()
    assert type(ev).__name__ == "AgentCompacted"
    assert ev.manual is True
    assert ev.before_tokens == before
    assert ev.after_tokens == after
    assert ev.messages_before == len(msgs)
    assert ev.messages_after == len(new_msgs)


@pytest.mark.asyncio
async def test_compact_now_no_event_when_queue_none() -> None:
    """If caller passes queue=None, no event is emitted but the
    return value is still valid — used by the TG gateway path that
    doesn't have a TUI queue to deliver events to."""
    agent = _make_agent()
    msgs = [_user(f"msg-{i}") for i in range(_COMPACTION_KEEP_TAIL + 5)]
    new_msgs, before, after = await agent.compact_now(msgs, queue=None)
    assert isinstance(new_msgs, list)
    assert isinstance(before, int)
    assert isinstance(after, int)
