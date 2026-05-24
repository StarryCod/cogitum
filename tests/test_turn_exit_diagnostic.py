"""Tests for the turn-exit diagnostic in Agent.run().

Production goal: every Agent.run() must log WHY the loop ended,
and emit a WARNING when the last message is `role: tool` (the
"agent stopped mid-work, didn't comment on tool results" pattern
users report as "model didn't see tool feedback").

These are end-to-end tests against Agent.run() with a fake mesh,
asserting the structured log line shape and level.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

import pytest

from cogitum.core.agent import Agent, AgentConfig
from cogitum.core.events import (
    ChunkKind,
    Message,
    StreamChunk,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    Usage,
)


# ── Fake mesh with scriptable response sequences ─────────────────────


class _ScriptedMesh:
    """Mesh stub that yields a pre-recorded list of stream chunks per
    call. ``responses`` is a list of lists — each inner list is what
    one ``stream()`` invocation will yield in order.
    """

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


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_diag_logs_end_turn_for_normal_text_response(caplog) -> None:
    """Normal completion: text response, no tool_calls. Log INFO with
    reason=end_turn and a non-zero response_len."""
    agent = _make_agent([[
        StreamChunk(kind=ChunkKind.TEXT, text="hello world"),
        StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn"),
    ]])
    with caplog.at_level(logging.INFO, logger="cogitum.core.agent"):
        await agent.run(user_message="hi")
    diag = [r for r in caplog.records if "Turn ended:" in r.message]
    assert diag, "no Turn-ended diagnostic recorded"
    msg = diag[-1].message
    assert "reason=end_turn" in msg
    assert "response_len=" in msg
    # response_len should be > 0 (we sent "hello world" = 11 chars)
    assert "response_len=0" not in msg
    # Normal completion → INFO, not WARNING
    assert diag[-1].levelno == logging.INFO


@pytest.mark.asyncio
async def test_diag_logs_warning_when_last_msg_is_tool(caplog) -> None:
    """The 'just stops' scenario: agent emits tool_calls, tools run,
    but loop ends before the model could comment on the results.
    This is the bug pattern users hit. Must be a WARNING, must
    include last_tool name."""
    # Script: turn 1 emits a tool_call. Turn 2 hits max_turns BEFORE
    # the model gets to respond — emit ANOTHER tool_call which means
    # max_turns terminates with last message = tool.
    tool_call_chunks = lambda cid, name: [
        StreamChunk(
            kind=ChunkKind.TOOL_CALL_DONE,
            tool_call_id=cid,
            tool_call_name=name,
            tool_call_args={"x": 1},
        ),
        StreamChunk(kind=ChunkKind.STOP, stop_reason="tool_use"),
    ]
    agent = _make_agent([
        tool_call_chunks("c1", "terminal"),
        tool_call_chunks("c2", "read_file"),
        tool_call_chunks("c3", "write_file"),
    ])
    agent.cfg.max_turns = 3  # hit cap on turn 4
    with caplog.at_level(logging.WARNING, logger="cogitum.core.agent"):
        await agent.run(user_message="run forever")

    diag = [
        r for r in caplog.records
        if "Turn ended:" in r.message and r.name == "cogitum.core.agent"
    ]
    assert diag, "no Turn-ended diagnostic recorded"
    last = diag[-1]
    assert last.levelno == logging.WARNING, (
        f"expected WARNING for last_msg_role=tool, got {logging.getLevelName(last.levelno)}"
    )
    assert "last_msg_role=tool" in last.message
    # Strict: the last tool name must resolve to one we actually called.
    # The agent emitted c1=terminal, c2=read_file, c3=write_file before
    # max_turns hit, so the most-recent assistant tool_calls is c3.
    assert "last_tool=write_file" in last.message, (
        f"expected last_tool=write_file (the most recent call), got: {last.message}"
    )
    assert "reason=max_turns_reached" in last.message
    # tool_turns should reflect the 3 assistant tool-call batches.
    assert "tool_turns=3" in last.message


@pytest.mark.asyncio
async def test_diag_logs_empty_response_when_provider_returns_nothing(caplog) -> None:
    """Provider STOP with no TEXT/THINKING/TOOL_CALL chunks.
    Reason should be 'empty_response' so operators can spot
    silently-failing providers."""
    agent = _make_agent([[
        StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn"),
    ]])
    with caplog.at_level(logging.INFO, logger="cogitum.core.agent"):
        await agent.run(user_message="hi")
    diag = [r for r in caplog.records if "Turn ended:" in r.message]
    assert diag
    assert "reason=empty_response" in diag[-1].message


@pytest.mark.asyncio
async def test_diag_logs_thinking_only_response(caplog) -> None:
    """Reasoning emitted, no visible text, no tool_calls. Distinct
    reason so we can see how often the model 'thinks but never
    answers' on each provider."""
    agent = _make_agent([[
        StreamChunk(kind=ChunkKind.THINKING, thinking="reasoning…"),
        StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn"),
    ]])
    with caplog.at_level(logging.INFO, logger="cogitum.core.agent"):
        await agent.run(user_message="hi")
    diag = [r for r in caplog.records if "Turn ended:" in r.message]
    assert diag
    assert "reason=thinking_only_response" in diag[-1].message


@pytest.mark.asyncio
async def test_diag_logs_exception_path(caplog) -> None:
    """If the loop throws, the diagnostic still fires with the
    exception class as the reason."""

    class _BoomMesh(_ScriptedMesh):
        async def stream(self, req):  # type: ignore[override]
            raise RuntimeError("boom")
            yield  # pragma: no cover  (make it an async-gen)

    agent = Agent(
        mesh=_BoomMesh([]),
        registry=_FakeRegistry(),
        config=AgentConfig(model="fake/model"),
    )
    with caplog.at_level(logging.INFO, logger="cogitum.core.agent"):
        await agent.run(user_message="hi")
    diag = [r for r in caplog.records if "Turn ended:" in r.message]
    assert diag
    assert "reason=exception:RuntimeError" in diag[-1].message


@pytest.mark.asyncio
async def test_diag_logs_on_cancellation(caplog) -> None:
    """CancelledError must NOT skip the diagnostic. The bug it
    surfaces (Esc mid-tool: tool result hangs unanswered) is exactly
    the case where the operator most needs the log line.

    Implementation puts the diag block in a `finally:` so the
    re-raised CancelledError unwinds through it before propagating.
    """

    # Mesh that emits a STOP, then on the second call hangs forever
    # so the test can cancel mid-stream.
    class _HangingMesh(_ScriptedMesh):
        def __init__(self):
            super().__init__([])

        async def stream(self, req):  # type: ignore[override]
            await asyncio.sleep(60)  # cancellation target
            yield  # pragma: no cover

    agent = Agent(
        mesh=_HangingMesh(),
        registry=_FakeRegistry(),
        config=AgentConfig(model="fake/model"),
    )

    with caplog.at_level(logging.INFO, logger="cogitum.core.agent"):
        task = asyncio.create_task(agent.run(user_message="hi"))
        await asyncio.sleep(0.05)  # let the agent reach the await
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    diag = [r for r in caplog.records if "Turn ended:" in r.message]
    assert diag, (
        "no Turn-ended diagnostic emitted on cancellation — "
        "the finally block must run before CancelledError propagates"
    )
    assert "reason=cancelled" in diag[-1].message


@pytest.mark.asyncio
async def test_diag_does_not_crash_run_when_message_is_malformed(caplog) -> None:
    """The diagnostic walks the message list. If a malformed Message
    slips in (e.g. a future Message subclass with a buggy property),
    the diag must NOT poison the agent's return value or mask the
    real exit. Wrapped in its own try/except for that reason."""

    class _PoisonMessage:
        """Looks enough like a Message to land in the buffer but
        explodes when the diagnostic touches it."""
        @property
        def role(self):
            raise RuntimeError("poisoned message accessed")

        @property
        def tool_calls(self):
            raise RuntimeError("poisoned tool_calls accessed")

        parts = []

    agent = _make_agent([[
        StreamChunk(kind=ChunkKind.TEXT, text="hello"),
        StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn"),
    ]])
    # Drive a normal run to completion, then jam a poisoned Message
    # at the end and call the diagnostic helper through a synthetic
    # second run. Easier: monkey-patch Agent.run to inject one.
    # Simpler still: directly assert the diag block's resilience by
    # giving the agent a starting history that ends with poison.
    poisoned_history = [_PoisonMessage()]  # type: ignore[list-item]
    with caplog.at_level(logging.INFO, logger="cogitum.core.agent"):
        # The agent's first action will append the user_message; the
        # poisoned message is no longer last by the time we exit.
        # To put poison at the end we need a different approach: drop
        # in an empty mesh + zero-iteration cap so loop exits before
        # appending anything new.
        agent.cfg.max_turns = -1  # while loop exits immediately
        result = await agent.run(
            user_message="hi", history=poisoned_history
        )

    # The run completed (didn't raise) and returned a list. A
    # suppressed-diagnostic line should be in the log.
    assert isinstance(result, list)
    suppressed = [
        r for r in caplog.records
        if "Turn-exit diagnostic failed" in r.message
    ]
    assert suppressed, (
        "expected a 'Turn-exit diagnostic failed (suppressed)' record "
        "but none was logged"
    )


@pytest.mark.asyncio
async def test_diag_logs_when_registry_to_openai_throws(caplog) -> None:
    """Pre-loop setup (registry.to_openai) is now INSIDE the try so
    a failure there still produces a Turn-ended diagnostic. Round-2
    adversarial gap: previously these crashes happened above the try
    block and the operator never saw why the run ended."""

    class _BrokenRegistry:
        def to_openai(self, tags=None):
            raise RuntimeError("registry exploded during to_openai")

        def names(self):
            return []

        async def execute(self, name, args):  # pragma: no cover
            return ""

    agent = Agent(
        mesh=_ScriptedMesh([]),
        registry=_BrokenRegistry(),
        config=AgentConfig(model="fake/model"),
    )
    with caplog.at_level(logging.INFO, logger="cogitum.core.agent"):
        await agent.run(user_message="hi")
    diag = [r for r in caplog.records if "Turn ended:" in r.message]
    assert diag, (
        "registry.to_openai failure must still produce a diagnostic"
    )
    assert "reason=exception:RuntimeError" in diag[-1].message
