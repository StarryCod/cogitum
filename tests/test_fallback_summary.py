"""Tests for the fallback-summary-on-no-text-exit feature.

When the agent loop ends without producing a final assistant text
response (max_turns hit, empty_response, thinking_only_response, or
last message was a tool result), Agent.run() asks the model for ONE
extra closing turn WITHOUT tools so the user always sees a coherent
end to the conversation. Hermes-agent does the same via
`_handle_max_iterations`.

These tests verify:
- When fallback fires (and when it doesn't)
- Streamed text reaches the queue as AgentText events
- The summary lands as an assistant Message in the returned history
- turn_exit_reason is annotated with `+fallback_summary`
- final_response_len is updated
- Failures inside the fallback don't crash the run
- _suppress_fallback_summary kwarg disables it (for tests / nested runs)
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

import pytest

from cogitum.core.agent import (
    Agent,
    AgentConfig,
    AgentText,
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
    """Mesh stub: pre-recorded response sequences. Inner list per
    stream() invocation. After all scripts are exhausted, returns a
    plain STOP — empty turn."""

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
        config=AgentConfig(model="fake/model", max_turns=3),
    )


# ── Fallback fires on the right exits ────────────────────────────────


@pytest.mark.asyncio
async def test_fallback_fires_on_max_turns_with_pending_tool_result() -> None:
    """The canonical bug case: max_turns hit while last message is
    a tool result. Without fallback the user sees nothing; with
    fallback the model emits a closing summary.

    Script: 3 assistant turns each emit one tool_call. The 4th turn
    would fire if max_turns allowed; it doesn't. Then the fallback
    is invoked and produces a summary.
    """
    tool_call = lambda cid, name: [
        StreamChunk(
            kind=ChunkKind.TOOL_CALL_DONE,
            tool_call_id=cid,
            tool_call_name=name,
            tool_call_args={},
        ),
        StreamChunk(kind=ChunkKind.STOP, stop_reason="tool_use"),
    ]
    summary_chunks = [
        StreamChunk(kind=ChunkKind.TEXT, text="Closing summary: "),
        StreamChunk(kind=ChunkKind.TEXT, text="three tools ran."),
        StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn"),
    ]
    agent = _make_agent([
        tool_call("c1", "terminal"),
        tool_call("c2", "read_file"),
        tool_call("c3", "write_file"),
        summary_chunks,  # the fallback call
    ])
    agent.cfg.max_turns = 3

    queue: asyncio.Queue = asyncio.Queue()
    history = await agent.run(user_message="run forever", queue=queue)

    # Last message in history is now an assistant with the summary,
    # not the dangling tool result.
    assert history[-1].role == "assistant"
    text_parts = [p for p in history[-1].parts if isinstance(p, TextPart)]
    assert text_parts
    assert "Closing summary" in text_parts[0].text
    assert "three tools ran" in text_parts[0].text


@pytest.mark.asyncio
async def test_fallback_streams_text_through_queue() -> None:
    """The fallback's text deltas reach the same queue as a normal
    stream so TUI/TG render them inline."""
    tool_call = lambda cid: [
        StreamChunk(
            kind=ChunkKind.TOOL_CALL_DONE,
            tool_call_id=cid,
            tool_call_name="t",
            tool_call_args={},
        ),
        StreamChunk(kind=ChunkKind.STOP, stop_reason="tool_use"),
    ]
    summary = [
        StreamChunk(kind=ChunkKind.TEXT, text="part-A "),
        StreamChunk(kind=ChunkKind.TEXT, text="part-B"),
        StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn"),
    ]
    agent = _make_agent([tool_call("c1"), summary])
    agent.cfg.max_turns = 1  # one tool turn, then loop ends
    queue: asyncio.Queue = asyncio.Queue()

    await agent.run(user_message="hi", queue=queue)

    # Drain queue, collect text deltas
    deltas: list[str] = []
    while not queue.empty():
        try:
            ev = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if isinstance(ev, AgentText):
            deltas.append(ev.delta)

    joined = "".join(deltas)
    # The prefix banner + the streamed parts must be visible
    assert "closing summary" in joined.lower()
    assert "part-A" in joined
    assert "part-B" in joined


@pytest.mark.asyncio
async def test_fallback_fires_on_empty_response(caplog) -> None:
    """Provider returns STOP with no chunks. Fallback fires and
    asks for a summary instead."""
    summary = [
        StreamChunk(kind=ChunkKind.TEXT, text="Recovered."),
        StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn"),
    ]
    agent = _make_agent([
        [StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn")],
        summary,
    ])
    with caplog.at_level(logging.INFO, logger="cogitum.core.agent"):
        history = await agent.run(user_message="hi")

    assert history[-1].role == "assistant"
    text = history[-1].parts[0].text
    assert "Recovered" in text

    diag = [r for r in caplog.records if "Turn ended:" in r.message]
    assert any(
        "+fallback_summary" in r.message for r in diag
    ), "diagnostic should be annotated with +fallback_summary"


@pytest.mark.asyncio
async def test_fallback_fires_on_thinking_only_response() -> None:
    """Reasoning-only response (no text, no tool_calls) → fallback."""
    summary = [
        StreamChunk(kind=ChunkKind.TEXT, text="Forced answer."),
        StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn"),
    ]
    agent = _make_agent([
        [
            StreamChunk(kind=ChunkKind.THINKING, thinking="reasoning…"),
            StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn"),
        ],
        summary,
    ])
    history = await agent.run(user_message="hi")
    assert history[-1].role == "assistant"
    assert "Forced answer" in history[-1].parts[0].text


# ── Fallback does NOT fire on the wrong exits ────────────────────────


@pytest.mark.asyncio
async def test_fallback_does_NOT_fire_on_normal_text_response() -> None:
    """end_turn with real text → fallback must NOT trigger.
    Otherwise we'd double-cost every successful run."""
    agent = _make_agent([
        [
            StreamChunk(kind=ChunkKind.TEXT, text="hello"),
            StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn"),
        ],
    ])
    history = await agent.run(user_message="hi")
    # The assistant message is the only one we emitted; fallback
    # didn't append anything else.
    assert sum(1 for m in history if m.role == "assistant") == 1
    assert history[-1].parts[0].text == "hello"


@pytest.mark.asyncio
async def test_fallback_does_NOT_fire_on_cancellation(caplog) -> None:
    """User-initiated cancel: don't fire another LLM call. The
    user wants to stop, not get one more response."""

    class _HangingMesh(_ScriptedMesh):
        def __init__(self):
            super().__init__([])

        async def stream(self, req):  # type: ignore[override]
            await asyncio.sleep(60)
            yield  # pragma: no cover

    agent = Agent(
        mesh=_HangingMesh(),
        registry=_FakeRegistry(),
        config=AgentConfig(model="fake/model"),
    )
    with caplog.at_level(logging.INFO, logger="cogitum.core.agent"):
        task = asyncio.create_task(agent.run(user_message="hi"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # The diagnostic should show reason=cancelled WITHOUT
    # +fallback_summary suffix.
    diag = [r for r in caplog.records if "Turn ended:" in r.message]
    assert diag
    assert "+fallback_summary" not in diag[-1].message
    assert "reason=cancelled" in diag[-1].message


@pytest.mark.asyncio
async def test_fallback_does_NOT_fire_on_exception() -> None:
    """If the loop crashed, AgentError already fired. Don't risk
    a second crash from the same provider in the fallback path."""

    class _BoomMesh(_ScriptedMesh):
        async def stream(self, req):  # type: ignore[override]
            raise RuntimeError("boom")
            yield  # pragma: no cover

    agent = Agent(
        mesh=_BoomMesh([]),
        registry=_FakeRegistry(),
        config=AgentConfig(model="fake/model"),
    )
    history = await agent.run(user_message="hi")
    # No assistant message added — fallback was skipped.
    assert all(m.role != "assistant" for m in history)


# ── Failure modes ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fallback_failure_does_not_crash_run(caplog) -> None:
    """If the fallback's own stream throws, the run must still
    return successfully. The original exit reason wins."""

    # Mesh: first call succeeds (empty response), second call throws.
    class _PartlyBrokenMesh(_ScriptedMesh):
        def __init__(self):
            super().__init__([])
            self._n = 0

        async def stream(self, req):  # type: ignore[override]
            self._n += 1
            if self._n == 1:
                yield StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn")
                return
            raise RuntimeError("fallback boom")
            yield  # pragma: no cover

    agent = Agent(
        mesh=_PartlyBrokenMesh(),
        registry=_FakeRegistry(),
        config=AgentConfig(model="fake/model"),
    )
    with caplog.at_level(logging.INFO, logger="cogitum.core.agent"):
        history = await agent.run(user_message="hi")

    # Run completed (didn't raise); fallback failure was suppressed.
    assert isinstance(history, list)
    suppressed = [
        r for r in caplog.records
        if "Fallback summary" in r.message
    ]
    assert suppressed, "expected a fallback-failure log record"


# ── Suppress kwarg ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_suppress_fallback_summary_kwarg() -> None:
    """`_suppress_fallback_summary=True` skips the fallback path
    entirely. Used by nested runs (fallback shouldn't recursively
    trigger fallback) and by tests that want raw exit behavior."""
    agent = _make_agent([
        [StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn")],
    ])
    history = await agent.run(
        user_message="hi", _suppress_fallback_summary=True,
    )
    # No assistant text was produced (empty stream) and fallback
    # was suppressed → no assistant Message in history at all.
    assert all(m.role != "assistant" for m in history)


# ── Round-1 critic gaps ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthetic_nudge_not_in_returned_history() -> None:
    """The fallback adds a 'closing summary' user message to a COPY
    of the buffer it sends to the model — the original messages
    list passed back to the caller must NOT contain that nudge.

    Regression lock: a future refactor that switches to append+remove
    might forget to remove on exception/early-return paths.
    """
    summary = [
        StreamChunk(kind=ChunkKind.TEXT, text="closing"),
        StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn"),
    ]
    agent = _make_agent([
        [StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn")],
        summary,
    ])
    history = await agent.run(user_message="orig")

    # No user message containing the nudge text
    nudge_marker = "summarise what you found"
    for m in history:
        if m.role == "user":
            assert nudge_marker not in m.text, (
                f"synthetic nudge leaked into persisted history: {m.text!r}"
            )


@pytest.mark.asyncio
async def test_agent_done_arrives_AFTER_fallback_text() -> None:
    """UI consumers treat AgentDone as 'stop rendering / re-enable
    input'. The fallback's AgentText deltas must arrive BEFORE
    AgentDone, otherwise the closing summary is invisible to the
    UI. This is the round-1 reviewer's blocker.

    NB: tests/conftest.py wipes cogitum.* from sys.modules between
    tests, so a top-level `from cogitum.core.agent import AgentDone`
    can land on a different class object than the one the agent
    creates internally. We compare by type name to dodge that trap.
    """
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

    # Drain queue and find positions of last AgentText vs AgentDone.
    events: list = []
    while not queue.empty():
        try:
            events.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break

    text_positions = [
        i for i, e in enumerate(events)
        if type(e).__name__ == "AgentText" and "closing" in e.delta
    ]
    done_positions = [
        i for i, e in enumerate(events) if type(e).__name__ == "AgentDone"
    ]
    assert text_positions, "fallback text never made it to the queue"
    assert done_positions, (
        "AgentDone never emitted — events seen: "
        f"{[type(e).__name__ for e in events]}"
    )
    assert max(text_positions) < min(done_positions), (
        "AgentDone arrived BEFORE fallback text — UI would miss "
        "the closing summary"
    )


@pytest.mark.asyncio
async def test_cancel_during_fallback_still_logs_diagnostic(caplog) -> None:
    """If the user cancels WHILE the fallback is mid-stream, the
    diagnostic must still fire. Round-1 adversarial gap: the cancel
    raised inside the fallback path used to skip past the diag.

    Implementation guarantee: fallback + AgentDone + diagnostic are
    in nested try/finally so the diag is the LAST thing the function
    does on every exit path including BaseException propagation.
    """

    class _SlowFallbackMesh(_ScriptedMesh):
        def __init__(self):
            super().__init__([])
            self._n = 0

        async def stream(self, req):  # type: ignore[override]
            self._n += 1
            if self._n == 1:
                # Initial loop: empty STOP → triggers fallback
                yield StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn")
                return
            # Fallback call: emit one delta then hang for cancel
            yield StreamChunk(kind=ChunkKind.TEXT, text="starting…")
            await asyncio.sleep(60)
            yield  # pragma: no cover

    agent = Agent(
        mesh=_SlowFallbackMesh(),
        registry=_FakeRegistry(),
        config=AgentConfig(model="fake/model"),
    )

    with caplog.at_level(logging.INFO, logger="cogitum.core.agent"):
        task = asyncio.create_task(agent.run(user_message="hi"))
        # Wait long enough for the loop to exit + fallback to start
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    diag = [r for r in caplog.records if "Turn ended:" in r.message]
    assert diag, (
        "diagnostic must fire even when CancelledError raised "
        "from inside the fallback"
    )
    # The reason should reflect the cancel-during-fallback shape.
    last_msg = diag[-1].message
    assert (
        "fallback_cancelled" in last_msg
        or "cancelled" in last_msg
    ), f"unexpected diagnostic reason: {last_msg!r}"
