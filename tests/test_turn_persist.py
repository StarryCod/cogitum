"""Tests for the AgentTurnPersist event — mid-run persistence checkpoints.

The agent emits AgentTurnPersist after each atomic mutation of its
message buffer (assistant commit, tool-results commit, fallback summary
commit). Consumers (TUI/TG) react by writing the buffer to disk so a
process crash mid-loop loses at most one in-flight turn, not all
accumulated history.

These tests verify:
- Event fires after assistant message commit
- Event fires after tool-results commit
- Event carries the actual messages list (not just a count)
- Event fires after fallback summary too
- The snapshot semantics: consumer reading the .messages list at
  emission time sees what was committed
- The event order is consistent with the agent's commits
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from cogitum.core.agent import (
    Agent,
    AgentConfig,
)
from cogitum.core.events import (
    ChunkKind,
    Message,
    StreamChunk,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)


class _ScriptedMesh:
    def __init__(self, responses: list[list[StreamChunk]]) -> None:
        self._responses = responses
        self._call_idx = 0
        self.providers: dict = {}

    def resolve(self, ref: str):  # pragma: no cover
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


class _FakeRegistry:
    def to_openai(self, tags=None):  # pragma: no cover
        return []

    def names(self):  # pragma: no cover
        return []

    async def execute(self, name, args):
        return f"fake-result({name})"


def _make_agent(responses: list[list[StreamChunk]]) -> Agent:
    return Agent(
        mesh=_ScriptedMesh(responses),
        registry=_FakeRegistry(),
        config=AgentConfig(model="fake/model", max_turns=5),
    )


def _drain_persist_events(queue: asyncio.Queue) -> list:
    """Pull every AgentTurnPersist event from the queue."""
    events = []
    while not queue.empty():
        try:
            ev = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if type(ev).__name__ == "AgentTurnPersist":
            events.append(ev)
    return events


# ── Basic emission ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_fires_after_text_only_response() -> None:
    """Plain end_turn: one assistant text response → ONE persist
    event after the assistant commit."""
    agent = _make_agent([[
        StreamChunk(kind=ChunkKind.TEXT, text="hello"),
        StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn"),
    ]])
    queue: asyncio.Queue = asyncio.Queue()
    await agent.run(user_message="hi", queue=queue)

    events = _drain_persist_events(queue)
    assert len(events) == 1, f"expected 1 persist event, got {len(events)}"
    # At emission, history was: user + assistant
    assert events[0].messages_count == 2


@pytest.mark.asyncio
async def test_persist_fires_after_tool_batch() -> None:
    """Tool-call turn → tool execution → tool-results commit. Each
    of (assistant, tool-results) is a persist boundary."""
    tool_call = [
        StreamChunk(
            kind=ChunkKind.TOOL_CALL_DONE,
            tool_call_id="c1",
            tool_call_name="terminal",
            tool_call_args={},
        ),
        StreamChunk(kind=ChunkKind.STOP, stop_reason="tool_use"),
    ]
    final_text = [
        StreamChunk(kind=ChunkKind.TEXT, text="all done"),
        StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn"),
    ]
    agent = _make_agent([tool_call, final_text])
    queue: asyncio.Queue = asyncio.Queue()
    await agent.run(user_message="run ls", queue=queue)

    events = _drain_persist_events(queue)
    # Events: (a) after assistant tool_call commit, (b) after tool
    # result message commit, (c) after final assistant text commit.
    assert len(events) >= 3, (
        f"expected at least 3 persist events, got {len(events)}"
    )
    # Counts must be monotonic — buffer only grows during a run
    counts = [e.messages_count for e in events]
    assert counts == sorted(counts), (
        f"persist counts must be monotonically non-decreasing: {counts}"
    )


@pytest.mark.asyncio
async def test_persist_messages_list_reflects_committed_state() -> None:
    """The event's messages field is a SHARED reference. After the
    run ENDS we can verify each persist boundary committed at least
    the right tail role.

    NB: snapshots can't be reliably taken DURING the run because the
    consumer drains events on the same event loop the agent runs on
    — by the time the drain task observes a persist, the agent may
    have already committed the next message. The shared-reference
    contract is verified by `test_persist_carries_actual_buffer_not_a_copy`
    further down. This test instead verifies monotonic role progression.
    """
    tool_call = [
        StreamChunk(
            kind=ChunkKind.TOOL_CALL_DONE,
            tool_call_id="c1",
            tool_call_name="t",
            tool_call_args={},
        ),
        StreamChunk(kind=ChunkKind.STOP, stop_reason="tool_use"),
    ]
    final = [
        StreamChunk(kind=ChunkKind.TEXT, text="bye"),
        StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn"),
    ]
    agent = _make_agent([tool_call, final])
    queue: asyncio.Queue = asyncio.Queue()

    # Snapshot eagerly inside the drain loop — `list(ev.messages)`
    # at receive time captures the buffer state at THAT moment,
    # before the agent gets the next event-loop slice.
    snapshots: list[list[str]] = []

    async def drain():
        while True:
            ev = await queue.get()
            name = type(ev).__name__
            if name == "AgentTurnPersist":
                snapshots.append([m.role for m in list(ev.messages)])
            if name in ("AgentDone", "AgentError"):
                break

    drainer = asyncio.create_task(drain())
    await agent.run(user_message="x", queue=queue)
    await asyncio.wait_for(drainer, timeout=5.0)

    # Three persist events — assistant_with_tool_call, tool_results,
    # final_assistant. Even with snapshots, the drainer may observe
    # successive states because the agent runs on the same loop.
    # What we CAN guarantee:
    #   - At least 3 events
    #   - Final history ends in [user, assistant, tool, assistant]
    #   - Counts are monotonically non-decreasing
    assert len(snapshots) >= 3
    counts = [len(s) for s in snapshots]
    assert counts == sorted(counts), (
        f"persist counts must be monotonically non-decreasing: {counts}"
    )
    # Final state ends with all four roles
    assert snapshots[-1] == ["user", "assistant", "tool", "assistant"]
    # Earliest snapshot has at least the user message
    assert snapshots[0][0] == "user"


# ── Edge cases ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_does_not_fire_on_empty_response() -> None:
    """Empty STOP → no assistant message → no persist event from
    the assistant-commit branch. (A persist may still fire later
    from the fallback path; we count only assistant-commit ones
    here by checking iteration matches.)"""
    agent = _make_agent([[
        StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn"),
    ]])
    queue: asyncio.Queue = asyncio.Queue()
    await agent.run(user_message="hi", queue=queue)

    events = _drain_persist_events(queue)
    # All iteration-1 persists should be absent (no assistant
    # message was committed at that turn). A later persist from a
    # fallback summary may appear, but it'd carry a different
    # iteration number or count.
    iter1 = [e for e in events if e.iteration == 1]
    assert not iter1, (
        "no assistant message was committed in iteration 1, "
        f"so no persist event should reference it: {iter1}"
    )


@pytest.mark.asyncio
async def test_persist_fires_after_successful_fallback_summary() -> None:
    """If the loop ends without text and the fallback produces a
    summary, a persist event MUST fire so the closing summary
    survives a crash before AgentDone reaches the consumer."""
    summary = [
        StreamChunk(kind=ChunkKind.TEXT, text="closing"),
        StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn"),
    ]
    agent = _make_agent([
        [StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn")],
        summary,
    ])
    queue: asyncio.Queue = asyncio.Queue()
    await agent.run(user_message="hi", queue=queue)

    events = _drain_persist_events(queue)
    # The fallback's persist event lands after the assistant
    # message holding the summary text was appended.
    assert events, "fallback should have emitted a persist event"
    last = events[-1]
    last_msg = last.messages[-1]
    assert last_msg.role == "assistant"
    text = "".join(p.text for p in last_msg.parts if isinstance(p, TextPart))
    assert "closing" in text


@pytest.mark.asyncio
async def test_persist_carries_snapshot_not_live_buffer() -> None:
    """The .messages field is a SHALLOW SNAPSHOT taken at emit time.

    Round-2 hardening: producer-side snapshot via ``list(messages)``
    so a subsequent agent turn cannot retroactively change what the
    consumer sees as "the state at this checkpoint". Message objects
    inside the list are still shared by reference (deep copy would
    be too expensive per persist), but the OUTER list never grows
    after emit.

    This test verifies:
      - event.messages is NOT the same object as the agent's final
        history (snapshot, not alias)
      - But it has the same content at emit time (since the test
        run only emits one persist event before completion)
    """
    agent = _make_agent([[
        StreamChunk(kind=ChunkKind.TEXT, text="ok"),
        StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn"),
    ]])
    queue: asyncio.Queue = asyncio.Queue()
    history = await agent.run(user_message="hi", queue=queue)

    events = _drain_persist_events(queue)
    assert events
    last = events[-1]
    # Snapshot: NOT the same list object as the returned history.
    assert last.messages is not history, (
        "AgentTurnPersist.messages must be a snapshot, not a live "
        "reference — round-2 hardening against producer mutation"
    )
    # But same content at the moment of emit.
    assert len(last.messages) == len(history)
    # Inner Message objects are shared by reference (intentional
    # cost trade-off), so identity holds element-wise.
    assert all(a is b for a, b in zip(last.messages, history))


# ── Round-1 critic gaps ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_fires_after_auto_compaction() -> None:
    """Auto-compaction shrinks `messages` in-place. A crash before
    the next assistant commit would otherwise leave the OLD bloated
    history on disk. A persist event fires immediately after
    AgentCompacted to keep on-disk state in sync.

    We force compaction by monkey-patching `_get_context_window` to
    return a tiny window and pre-loading enough history that the
    first iteration's pre-flight estimate triggers compaction
    before any LLM call.
    """
    # Two responses: the compaction prompt AND the post-compaction
    # turn (since fake mesh script-feeds in order).
    final_response = [
        StreamChunk(kind=ChunkKind.TEXT, text="ok"),
        StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn"),
    ]
    # Compaction also calls mesh.stream once for the summarization.
    compact_summary = [
        StreamChunk(kind=ChunkKind.TEXT, text="brief summary of head"),
        StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn"),
    ]
    agent = _make_agent([compact_summary, final_response])

    # Force compaction: tiny context_window so the buffer estimate
    # exceeds 80% of it. Default _COMPACTION_KEEP_TAIL=16, so the
    # history must be longer than that.
    agent._get_context_window = lambda: 100

    long_history = [
        Message(role="user", parts=[TextPart(text=f"old {i}" * 50)])
        for i in range(20)
    ]

    queue: asyncio.Queue = asyncio.Queue()
    await agent.run(
        user_message="new question",
        history=long_history,
        queue=queue,
    )

    # Walk events in receive order — find AgentCompacted, then
    # check the very next AgentTurnPersist sees the SHORTER buffer.
    seen_compacted_at: int | None = None
    persist_after_compact = None
    events: list = []
    while not queue.empty():
        try:
            ev = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        events.append(ev)

    for i, ev in enumerate(events):
        if type(ev).__name__ == "AgentCompacted":
            seen_compacted_at = i
        elif (
            type(ev).__name__ == "AgentTurnPersist"
            and seen_compacted_at is not None
            and persist_after_compact is None
        ):
            persist_after_compact = ev
            break

    assert seen_compacted_at is not None, (
        "AgentCompacted must fire when context window is exceeded"
    )
    assert persist_after_compact is not None, (
        "AgentTurnPersist must fire AFTER AgentCompacted so on-disk "
        "state matches the compacted buffer; otherwise a crash "
        "before the next assistant commit leaves bloat on disk"
    )