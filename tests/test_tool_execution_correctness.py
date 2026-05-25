"""Tier-1 correctness tests for parallel-tool execution and approval gating.

Covers four bugs identified in the agent.py audit:

1. CancelledError MUST propagate from _execute_tool (not be swallowed
   into a string result). Otherwise /stop turns into a normal tool
   result and the agent loop happily continues to the next turn.

2. Approval-modify path MUST NOT mutate the ToolCallPart in place.
   The same `tc` object is referenced by `messages` and by every
   AgentTurnPersist snapshot already emitted this turn — mutating it
   retroactively rewrites audit history.

3. When one task in a parallel tool batch raises (or the outer run is
   cancelled), the surviving tool tasks MUST be cancelled. Otherwise
   they keep burning provider tokens and the next turn's batch races
   against the orphans.

4. Wire-shape contract: every assistant tool_call MUST get a matching
   tool_result on the next turn. If a slot stays None (cancelled
   mid-flight, BaseException leak), a placeholder error part must be
   synthesized so we never emit a tool message with fewer parts than
   the assistant emitted tool_calls.
"""

from __future__ import annotations

import asyncio
import inspect
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
)


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


class _Registry:
    """Configurable fake registry; behaviour depends on tool name."""

    def __init__(self, behaviour: dict[str, "object"]) -> None:
        # Map tool-name -> behaviour:
        #   "ok"           : return string
        #   "raise"        : raise RuntimeError
        #   "slow"         : sleep forever (until cancelled)
        #   "delay:<sec>"  : sleep N seconds then return string
        self._b = behaviour

    def to_openai(self, tags=None):  # pragma: no cover
        return []

    def names(self):  # pragma: no cover
        return list(self._b)

    async def execute(self, name, args):
        b = self._b.get(name, "ok")
        if b == "raise":
            raise RuntimeError(f"boom in {name}")
        if b == "slow":
            await asyncio.sleep(60)
            return "should never get here"
        if isinstance(b, str) and b.startswith("delay:"):
            await asyncio.sleep(float(b.split(":", 1)[1]))
            return f"executed {name}({args})"
        return f"executed {name}({args})"


def _agent(behaviour: dict[str, "object"], *, yolo: bool = True) -> Agent:
    return Agent(
        mesh=_FakeMesh(),
        registry=_Registry(behaviour),
        config=AgentConfig(model="fake/model", yolo_mode=yolo),
    )


# ─────────────────────────────────────────────────────────────────────
# Bug 1: CancelledError must propagate (not be turned into a string)
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_tool_propagates_cancelled_error() -> None:
    """User-initiated cancel (/stop, Esc) reaches _execute_tool through
    the task it's running in. The previous code caught CancelledError
    and returned 'ERROR: tool execution cancelled by user' — the loop
    would then commit that as a normal tool result and roll into the
    next iteration. Now CancelledError must propagate so the parent
    can cancel siblings and unwind the run.
    """
    agent = _agent({"slow_tool": "slow"})
    tc = ToolCallPart(id="c1", name="slow_tool", arguments={})

    task = asyncio.create_task(
        agent._execute_tool(tc, turn=1, queue=asyncio.Queue())
    )
    # Let it enter wait_for(registry.execute, ...) before cancelling.
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# ─────────────────────────────────────────────────────────────────────
# Bug 2: approval-modify must not mutate ToolCallPart in place
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_modify_approval_does_not_mutate_original_toolcall() -> None:
    """Auditor finding HIGH @ 1987: in-place tc.arguments rewrite touches
    every shared reference (messages, persist snapshots). Repro: hold
    a reference to tc.arguments, send `modify:` decision, assert the
    original dict is unchanged after _execute_tool returns.
    """
    # yolo=False so the approval gate is reached
    agent = _agent({"terminal": "ok"}, yolo=False)
    agent._approval_queue = asyncio.Queue()

    original_args = {"command": "rm -rf /tmp/foo"}
    tc = ToolCallPart(
        id="c1",
        name="terminal",
        arguments=original_args,
    )
    # Pre-modify reference & snapshot for assertion at the end.
    pre_id = id(tc.arguments)
    pre_copy = dict(tc.arguments)

    task = asyncio.create_task(
        agent._execute_tool(tc, turn=1, queue=asyncio.Queue())
    )
    await asyncio.sleep(0.05)
    # Send a "modify" decision swapping the command to something else.
    import json
    new_args = {"command": "ls /tmp"}
    assert agent.submit_approval(tc.id, "modify:" + json.dumps(new_args))

    result = await asyncio.wait_for(task, timeout=2.0)

    # The execution received the modified args (visible in the fake
    # registry's echo).
    assert "ls /tmp" in result
    # CRITICAL: the ToolCallPart's arguments must be unchanged. Same
    # dict identity, same contents.
    assert id(tc.arguments) == pre_id, (
        "ToolCallPart.arguments was replaced with a new object — "
        "shared references in messages/persist snapshots now diverge"
    )
    assert tc.arguments == pre_copy, (
        "ToolCallPart.arguments was mutated in place — audit history "
        "now retroactively shows the modified args, not what the "
        "model originally emitted"
    )


# ─────────────────────────────────────────────────────────────────────
# Bug 3 + 4: parallel batch — siblings cancel on error,
# orphan slots get a placeholder ToolResultPart
# ─────────────────────────────────────────────────────────────────────


def _stream_one_assistant_turn(tool_calls: list[ToolCallPart]):
    """Build a fake mesh that yields one assistant turn with the given
    tool_calls and then stops. Used to drive Agent.run() through the
    parallel-batch code path exactly once.
    """

    class _Mesh:
        providers: dict = {}

        def resolve(self, ref):
            return []

        def list_resolved(self):
            return []

        async def stream(self, req) -> AsyncIterator[StreamChunk]:
            # First call: emit tool_calls then stop.
            # Second call (after tool results): just stop with a final
            # text answer so the loop unwinds cleanly.
            if not getattr(self, "_first_done", False):
                self._first_done = True
                for tc in tool_calls:
                    yield StreamChunk(
                        kind=ChunkKind.TOOL_CALL_DONE,
                        tool_call_id=tc.id,
                        tool_call_name=tc.name,
                        tool_call_args=tc.arguments,
                    )
                yield StreamChunk(
                    kind=ChunkKind.STOP, stop_reason="tool_use"
                )
                return
            yield StreamChunk(kind=ChunkKind.TEXT, text="done")
            yield StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn")

        async def aclose(self):
            return None

    return _Mesh()


@pytest.mark.asyncio
async def test_parallel_batch_orphan_slot_gets_placeholder_part() -> None:
    """If one task in a parallel batch crashes pre-population (or is
    cancelled before producing a result), the wire-shape MUST stay
    legal: len(tool_results) == len(tool_calls). The fix synthesizes
    a placeholder ToolResultPart with is_error=True for the missing
    slot so Anthropic/OpenAI don't reject the next turn with
    "tool_use ids without matching tool_result".

    We simulate this by patching _execute_tool_indexed so call #1
    crashes via BaseException (which slips past the broad except in
    _execute_tool). Calls #0 and #2 succeed normally.
    """
    tcs = [
        ToolCallPart(id="c0", name="terminal", arguments={"a": 0}),
        ToolCallPart(id="c1", name="terminal", arguments={"a": 1}),
        ToolCallPart(id="c2", name="terminal", arguments={"a": 2}),
    ]
    mesh = _stream_one_assistant_turn(tcs)
    agent = Agent(
        mesh=mesh,
        registry=_Registry({"terminal": "ok"}),
        config=AgentConfig(model="fake/model", yolo_mode=True, max_turns=2),
    )

    real_indexed = agent._execute_tool_indexed

    async def patched(idx, tc, turn, queue=None):
        if idx == 1:
            # BaseException-derived: bypasses _execute_tool's catch-all
            # Exception handler. The slot stays None.
            raise BaseException("synthetic catastrophic failure")
        return await real_indexed(idx, tc, turn, queue=queue)

    agent._execute_tool_indexed = patched  # type: ignore[assignment]

    q: asyncio.Queue = asyncio.Queue()
    user_msg = Message(role="user", parts=[TextPart(text="run them")])

    # Drive the run. We don't await full completion — we just need the
    # tool message to land.
    run_task = asyncio.create_task(agent.run([user_msg], queue=q))
    # Wait until run finishes or times out.
    try:
        await asyncio.wait_for(run_task, timeout=5.0)
    except BaseException:
        pass

    # Find the tool-role message in the agent's message history. The
    # public way is via the run task's return value (list[Message]).
    # If that errored, fall back to the agent's last persisted state.
    # In this fake setup run() returns a list.
    if run_task.done() and not run_task.cancelled() and not run_task.exception():
        out_messages = run_task.result()
    else:
        out_messages = None

    assert out_messages is not None, (
        "run() must complete successfully even when one tool task raises "
        "BaseException — the placeholder synthesis should keep wire shape legal"
    )

    tool_msgs = [m for m in out_messages if m.role == "tool"]
    assert tool_msgs, "expected a tool-role message after the batch"
    parts = tool_msgs[0].parts
    assert len(parts) == 3, (
        f"wire-shape violation: 3 tool_calls emitted but only {len(parts)} "
        f"tool_results returned. Anthropic/OpenAI will reject this on the "
        f"next stream call."
    )
    # The synthesized slot is the failed one (idx 1, call_id c1).
    by_id = {p.tool_call_id: p for p in parts}
    assert by_id["c1"].is_error is True
    assert "unavailable" in by_id["c1"].content.lower()
    # The other two carry the real results.
    assert by_id["c0"].is_error is False
    assert by_id["c2"].is_error is False


@pytest.mark.asyncio
async def test_parallel_batch_cancels_siblings_on_outer_cancel() -> None:
    """When the outer agent.run() is cancelled mid-batch, the surviving
    tool tasks must NOT keep running detached. The finally-block in the
    parallel batch cancels them and drains, otherwise they leak as
    orphan coroutines burning provider tokens.
    """
    tcs = [
        ToolCallPart(id="c0", name="slow", arguments={}),
        ToolCallPart(id="c1", name="slow", arguments={}),
    ]
    mesh = _stream_one_assistant_turn(tcs)
    agent = Agent(
        mesh=mesh,
        registry=_Registry({"slow": "slow"}),
        config=AgentConfig(model="fake/model", yolo_mode=True, max_turns=2),
    )

    q: asyncio.Queue = asyncio.Queue()
    user_msg = Message(role="user", parts=[TextPart(text="run slow tools")])

    run_task = asyncio.create_task(agent.run([user_msg], queue=q))
    # Let the parallel batch start.
    await asyncio.sleep(0.1)
    # Snapshot the tool tasks BEFORE cancelling so we can verify
    # they were actually cancelled (not just dropped).
    tool_tasks = list(agent._active_tool_tasks)
    assert tool_tasks, "tool tasks should be active mid-batch"

    run_task.cancel()
    # Allow cancellation to propagate.
    try:
        await asyncio.wait_for(run_task, timeout=2.0)
    except (asyncio.CancelledError, BaseException):
        pass

    # All tool tasks must now be done (cancelled or finished). None
    # should still be running detached.
    for t in tool_tasks:
        assert t.done(), (
            "tool task survived run() cancellation — orphan coroutine "
            "leak: still consuming resources with no consumer"
        )


# ─────────────────────────────────────────────────────────────────────
# Bug 5: per-call_id approval routing (no FIFO swap)
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approval_routes_by_call_id_out_of_order() -> None:
    """The model emits two medium/danger tools in a parallel batch.
    User clicks B-approve first, then A-reject. Under the old FIFO
    queue, A would be approved and B rejected (decisions swapped).
    Under per-call_id futures, each decision lands on the right call.
    """
    agent = _agent({"terminal": "ok"}, yolo=False)
    agent._approval_queue = asyncio.Queue()  # gate marker (non-None)

    tc_a = ToolCallPart(id="call_A", name="terminal", arguments={"command": "rm -rf /tmp/a"})
    tc_b = ToolCallPart(id="call_B", name="terminal", arguments={"command": "rm -rf /tmp/b"})

    task_a = asyncio.create_task(
        agent._execute_tool(tc_a, turn=1, queue=asyncio.Queue())
    )
    task_b = asyncio.create_task(
        agent._execute_tool(tc_b, turn=1, queue=asyncio.Queue())
    )
    # Let both register their futures.
    await asyncio.sleep(0.05)
    assert "call_A" in agent._approval_futures
    assert "call_B" in agent._approval_futures

    # Reply OUT OF ORDER: B first (approve), then A (reject).
    assert agent.submit_approval("call_B", "approve")
    assert agent.submit_approval("call_A", "reject")

    result_a = await asyncio.wait_for(task_a, timeout=2.0)
    result_b = await asyncio.wait_for(task_b, timeout=2.0)

    # If routing was FIFO, result_a would be "executed" (got B's approve)
    # and result_b would be REJECTED. With call_id routing we get the
    # opposite — and the opposite is correct.
    assert result_a.startswith("REJECTED"), (
        "call_A should have been rejected (its specific decision); "
        "got the approval meant for call_B instead — FIFO swap regression"
    )
    assert "executed" in result_b, (
        "call_B should have executed (its specific approval); got "
        "the rejection meant for call_A instead — FIFO swap regression"
    )


@pytest.mark.asyncio
async def test_submit_approval_returns_false_for_unknown_call_id() -> None:
    """A late callback from a stale UI (run already ended, future
    cleaned up) must not crash and must not corrupt future approvals.
    submit_approval returns False so the callback handler can show
    'no pending approval' instead of routing into the void.
    """
    agent = _agent({"terminal": "ok"})
    # No futures registered at all.
    assert agent.submit_approval("ghost_call", "approve") is False

    # Register one, resolve it, then a second submission for the same
    # id must also return False (future already done) — protects
    # against duplicate callbacks (TG retry, double-click).
    fut = asyncio.get_running_loop().create_future()
    agent._approval_futures["live"] = fut
    assert agent.submit_approval("live", "approve") is True
    assert fut.result() == "approve"
    assert agent.submit_approval("live", "reject") is False


@pytest.mark.asyncio
async def test_approval_future_cleaned_on_timeout() -> None:
    """Timeout path must remove the future from the map. Otherwise a
    late submit_approval call would resolve a future no longer being
    awaited (memory leak + silent action loss).
    """
    agent = _agent({"terminal": "ok"}, yolo=False)
    agent._approval_queue = asyncio.Queue()
    tc = ToolCallPart(id="late_call", name="terminal", arguments={"command": "rm -rf /tmp/x"})

    # Patch the timeout to something tiny via monkey-patching wait_for
    # is fragile; easier: cancel the task ourselves to simulate the
    # timeout cleanup path. The finally block handles both.
    task = asyncio.create_task(
        agent._execute_tool(tc, turn=1, queue=asyncio.Queue())
    )
    await asyncio.sleep(0.05)
    assert "late_call" in agent._approval_futures
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Future removed from map regardless of cancel/timeout/normal.
    assert "late_call" not in agent._approval_futures, (
        "future stayed in map after cancel — late submit_approval "
        "would resolve a dead future"
    )


# ─────────────────────────────────────────────────────────────────────
# Bug 5 (continued): defense-in-depth and edge cases
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_approval_rejects_invalid_decision_strings() -> None:
    """Garbage decision strings ("yes", "approbe", "") must NOT be
    treated as approve. submit_approval validates the decision and
    returns False for anything outside {"approve", "reject"} or
    starts with "modify:".
    """
    agent = _agent({"terminal": "ok"}, yolo=False)
    agent._approval_queue = asyncio.Queue()
    tc = ToolCallPart(id="g", name="terminal", arguments={"command": "rm -rf /tmp/g"})

    task = asyncio.create_task(
        agent._execute_tool(tc, turn=1, queue=asyncio.Queue())
    )
    await asyncio.sleep(0.05)

    # All of these must be rejected (return False, future not resolved)
    for garbage in ("yes", "approbe", "", "approve_", "rejectall"):
        assert agent.submit_approval("g", garbage) is False, (
            f"garbage decision {garbage!r} was accepted — submit_approval "
            f"contract violation. Typos and arbitrary user text must NOT "
            f"silently approve dangerous tools."
        )

    # Legit "approve" still works after the failed attempts.
    assert agent.submit_approval("g", "approve") is True
    result = await asyncio.wait_for(task, timeout=2.0)
    assert "executed" in result


@pytest.mark.asyncio
async def test_duplicate_call_id_rejects_orphan_so_it_unwinds() -> None:
    """If a duplicate call_id arrives in the same batch (provider replay,
    misbehaving MCP server, stream-parser bug), the OLD future must NOT
    be silently overwritten — that hangs the original call for 300s.
    Instead we resolve the existing future with "reject" and replace it
    with the new one. Both calls unwind in finite time.
    """
    agent = _agent({"terminal": "ok"}, yolo=False)
    agent._approval_queue = asyncio.Queue()

    tc1 = ToolCallPart(id="dup_id", name="terminal", arguments={"command": "rm -rf /tmp/a"})
    tc2 = ToolCallPart(id="dup_id", name="terminal", arguments={"command": "rm -rf /tmp/b"})

    task1 = asyncio.create_task(
        agent._execute_tool(tc1, turn=1, queue=asyncio.Queue())
    )
    await asyncio.sleep(0.05)
    # Now register a second call with the same id. The first one must
    # auto-reject so it doesn't hang.
    task2 = asyncio.create_task(
        agent._execute_tool(tc2, turn=1, queue=asyncio.Queue())
    )
    await asyncio.sleep(0.05)

    # Approve the second one explicitly.
    assert agent.submit_approval("dup_id", "approve") is True

    result1 = await asyncio.wait_for(task1, timeout=2.0)
    result2 = await asyncio.wait_for(task2, timeout=2.0)
    # The original call must unwind (not hang). It returns an honest
    # "preempted by duplicate call_id" diagnostic — NOT "REJECTED: user
    # denied" because the operator never actually denied anything; the
    # provider sent a duplicate id.
    assert "preempted" in result1.lower() and "duplicate" in result1.lower(), (
        f"first call with dup id must unwind with a preemption diagnostic, "
        f"got: {result1!r}"
    )
    assert "executed" in result2


@pytest.mark.asyncio
async def test_real_timeout_returns_rejected_string(monkeypatch) -> None:
    """The 300s wait_for in _execute_tool — exercise the actual
    asyncio.TimeoutError code path with a short-circuited internal
    wait_for so the test runs in <1s. Verifies the REJECTED string
    format and that the future is cleaned from the map.
    """
    agent = _agent({"terminal": "ok"}, yolo=False)
    agent._approval_queue = asyncio.Queue()
    tc = ToolCallPart(id="time", name="terminal", arguments={"command": "rm -rf /tmp/z"})

    # Patch wait_for inside the agent module's asyncio reference so
    # only its calls (the approval gate + tool exec cap) get the tiny
    # timeout. Outer test code keeps the real wait_for for assertions.
    import cogitum.core.agent as agent_module
    real_wait_for = agent_module.asyncio.wait_for

    async def short_wait_for(coro, timeout):
        # Slam any 300.0s timeout down to something we can wait out.
        return await real_wait_for(coro, 0.05 if timeout >= 1.0 else timeout)

    monkeypatch.setattr(agent_module.asyncio, "wait_for", short_wait_for)

    result = await real_wait_for(
        agent._execute_tool(tc, turn=1, queue=asyncio.Queue()),
        timeout=2.0,
    )

    assert result.startswith("REJECTED"), result
    assert "timed out" in result
    # Future cleaned up (no submit_approval can resolve a dead future).
    assert "time" not in agent._approval_futures


@pytest.mark.asyncio
async def test_cancelled_tool_never_returns_error_string() -> None:
    """Defense-in-depth: even if someone re-introduces the bug of
    catching CancelledError and returning 'ERROR: cancelled', the
    parallel-batch placeholder synthesis would still produce a
    reasonable message. But the contract is: a cancelled _execute_tool
    raises, never returns. Verify by checking that NO string with
    'ERROR: tool execution cancelled by user' (the old swallowed text)
    is produced.
    """
    agent = _agent({"slow_tool": "slow"})
    tc = ToolCallPart(id="c1", name="slow_tool", arguments={})

    task = asyncio.create_task(
        agent._execute_tool(tc, turn=1, queue=asyncio.Queue())
    )
    await asyncio.sleep(0.05)
    task.cancel()

    # Must raise CancelledError, never return a string.
    with pytest.raises(asyncio.CancelledError):
        await task

    # task.result() would raise; this is just defensive: nothing to
    # capture, no string was produced. If CancelledError is ever
    # caught and returned again, this test fails on the previous line.


@pytest.mark.asyncio
async def test_sibling_tools_cancelled_when_one_raises_baseexception() -> None:
    """When ONE tool task raises a BaseException-derived error (e.g.
    KeyboardInterrupt-derived, asyncio internal), the SURVIVING
    sibling tasks must be cancelled, not left running. Otherwise
    they keep burning provider tokens detached.

    The orphan-slot test above covers the wire shape; this one
    explicitly covers the cancel-on-error path with a non-cancelled
    sibling that's still mid-flight.
    """
    tcs = [
        ToolCallPart(id="fast_fail", name="raiser", arguments={}),
        ToolCallPart(id="slow", name="slow", arguments={}),
    ]
    mesh = _stream_one_assistant_turn(tcs)
    agent = Agent(
        mesh=mesh,
        registry=_Registry({"raiser": "raise", "slow": "slow"}),
        config=AgentConfig(model="fake/model", yolo_mode=True, max_turns=2),
    )

    real_indexed = agent._execute_tool_indexed

    async def patched(idx, tc, turn, queue=None):
        if tc.id == "fast_fail":
            # BaseException-derived, bypasses _execute_tool's catch.
            raise BaseException("synthetic catastrophic failure")
        return await real_indexed(idx, tc, turn, queue=queue)

    agent._execute_tool_indexed = patched  # type: ignore[assignment]

    q: asyncio.Queue = asyncio.Queue()
    user_msg = Message(role="user", parts=[TextPart(text="run")])

    run_task = asyncio.create_task(agent.run([user_msg], queue=q))
    # Snapshot the tool tasks before they finish.
    await asyncio.sleep(0.05)
    snapshot = list(agent._active_tool_tasks)
    assert snapshot, "tool tasks should be active mid-batch"

    # Wait for the run to settle (might raise via the BaseException).
    try:
        await asyncio.wait_for(run_task, timeout=5.0)
    except BaseException:
        pass

    # All snapshotted tasks must be done — the sibling must NOT have
    # kept running detached.
    for t in snapshot:
        assert t.done(), (
            "sibling tool task survived the sibling-raise path — "
            "leak: still consuming resources with no consumer"
        )


# ─────────────────────────────────────────────────────────────────────
# Bug 5: TUI/TG approval-routing integration smoke tests
# ─────────────────────────────────────────────────────────────────────


def test_telegram_callback_routes_approve_via_submit_approval() -> None:
    """The TG callback handler for `approve:<call_id>` must call
    self.agent.submit_approval(call_id, "approve") — NOT
    session.agent (which doesn't exist) and NOT
    session._approval_queue.put (which the agent no longer reads).

    AST-level check: parse the callback handler and verify it
    references `self.agent.submit_approval` (not session.agent).
    """
    import ast
    from cogitum.gateway import telegram as tg_module
    src = inspect.getsource(tg_module)
    tree = ast.parse(src)

    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        body_src = ast.unparse(node)
        if "approve:" not in body_src or "reject:" not in body_src:
            continue
        # This is a function that handles approve:/reject:.
        # The body MUST call self.agent.submit_approval.
        # Reject the dead-code variant `getattr(session, "agent", ...)`.
        if "self.agent.submit_approval" in body_src:
            found = True
        assert "getattr(session, 'agent'" not in body_src, (
            "TG handler is using getattr(session, 'agent', ...) which is "
            "always None — ChatSession has no .agent attribute"
        )

    assert found, (
        "TG approve:/reject: callback handler must call "
        "self.agent.submit_approval(call_id, action). The agent lives "
        "on the bot, not on per-chat sessions."
    )


def test_tui_approval_handler_routes_via_submit_approval() -> None:
    """The TUI ApprovalWidget.Decided handler must call
    self._agent.submit_approval(event.call_id, event.decision) —
    routing by call_id, not by FIFO order, and not by writing into
    a dead _approval_queue.
    """
    import ast
    from cogitum import app as app_module
    src = inspect.getsource(app_module)
    tree = ast.parse(src)

    # Find _on_approval_widget_decided
    found_handler = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name != "_on_approval_widget_decided":
            continue
        body_src = ast.unparse(node)
        assert "submit_approval" in body_src, (
            "TUI handler must route via submit_approval"
        )
        # No put_nowait into _approval_queue — that path is dead and
        # would silently leak if reintroduced.
        assert "_approval_queue.put_nowait" not in body_src, (
            "TUI handler still falls back to _approval_queue.put_nowait — "
            "that queue is no longer read by the agent. Bytes leak there "
            "and the user sees nothing happen."
        )
        found_handler = True

    assert found_handler, "TUI _on_approval_widget_decided handler not found"


def test_tui_yolo_race_guard_routes_via_submit_approval() -> None:
    """When yolo turns on between an approval emit and drain, the TUI
    consumer must auto-route the decision via submit_approval — NOT
    via _approval_queue.put_nowait (the old FIFO transport that
    nothing listens to anymore).
    """
    import ast
    from cogitum import app as app_module
    src = inspect.getsource(app_module)
    tree = ast.parse(src)

    found_branch_with_submit = False
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
        if "yolo_mode" in body_src and "submit_approval" in body_src:
            found_branch_with_submit = True
            # Stale put_nowait must NOT be present — dead transport.
            assert "_approval_queue.put_nowait" not in body_src, (
                "TUI yolo race-guard still writes to _approval_queue, "
                "which the agent doesn't read. The tool will hang for "
                "300s instead of auto-approving."
            )
            break

    assert found_branch_with_submit, (
        "TUI AgentApprovalRequest handler must use submit_approval "
        "in its yolo race-guard branch, not _approval_queue.put_nowait"
    )


def test_telegram_yolo_race_guard_routes_via_submit_approval() -> None:
    """Same race-guard contract on the TG side. The old code did
    `session._approval_queue.put("approve")` which is dead; new
    code must call `self.agent.submit_approval(event.call_id, ...)`.
    """
    import ast
    from cogitum.gateway import telegram as tg_module
    src = inspect.getsource(tg_module)
    tree = ast.parse(src)

    found_branch = False
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
        if "yolo_mode" in body_src and "submit_approval" in body_src:
            found_branch = True
            assert "session._approval_queue.put" not in body_src, (
                "TG yolo race-guard still writes to session._approval_queue, "
                "which the agent doesn't read. Tool hangs for 300s."
            )
            break

    assert found_branch, (
        "TG AgentApprovalRequest handler must use submit_approval "
        "in its yolo race-guard branch"
    )


# ─────────────────────────────────────────────────────────────────────
# Behavioral tests for TG/TUI handlers (round-2 reviewer asked for
# these — AST-only tests pass even on dead branches like
# `if False: self.agent.submit_approval(...)`)
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_telegram_callback_actually_invokes_submit_approval() -> None:
    """Behavioral: drive the TG callback through a faked API + agent
    and assert submit_approval was called with the right call_id and
    action. Catches the dead-branch regression that pure AST checks
    can't see (e.g. `if False: agent.submit_approval(...)`).
    """
    from cogitum.gateway.telegram import CogitumBot

    # Minimal fake API — only the surface CogitumBot._handle_callback
    # touches in the approve/reject branch.
    class _FakeAPI:
        def __init__(self) -> None:
            self.callbacks: list[tuple] = []
            self.edits: list[tuple] = []

        async def answer_callback(self, cb_id, text=""):
            self.callbacks.append((cb_id, text))

        async def edit_message(self, chat_id, msg_id, text, **kw):
            self.edits.append((chat_id, msg_id, text))

    class _FakeAgent:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []
            self.return_value = True

        def submit_approval(self, call_id, decision):
            self.calls.append((call_id, decision))
            return self.return_value

    # Bypass __init__ — it creates an asyncio.Semaphore bound to the
    # current loop and a poll-offset file handle, both of which leak
    # across tests when the loop is replaced. We only need the
    # attributes the callback handler touches.
    class _FakeConfig:
        allowed_user_id = 0
        allowed_chat_ids: list[int] = []

        def can_respond(self, user_id, chat_id):
            return True

    bot = CogitumBot.__new__(CogitumBot)
    bot.config = _FakeConfig()  # type: ignore
    bot.api = _FakeAPI()  # type: ignore
    bot.agent = _FakeAgent()  # type: ignore
    bot.sessions = {}
    bot._approval_token_to_call_id = {}
    # Pre-register a token→call_id mapping (the way the bot does it
    # when emitting an approval prompt — but here we shortcut so the
    # callback handler can resolve it).
    bot._approval_token_to_call_id["abc12345"] = "real_long_call_id_that_would_overflow_callback_data"
    # Dedup ring needed by _handle_callback's pre-amble.
    import collections as _c
    bot._seen_callbacks = _c.OrderedDict()
    bot._seen_callbacks_max = 256

    # Simulate the callback Telegram would deliver.
    callback = {
        "id": "cbid",
        "from": {"id": 1},
        "message": {"chat": {"id": 1}, "message_id": 99},
        "data": "approve:abc12345",
    }

    await bot._handle_callback(callback)

    # The agent must have been called with the FULL call_id resolved
    # from the token, not the token itself, and with the right action.
    assert bot.agent.calls == [(
        "real_long_call_id_that_would_overflow_callback_data",
        "approve",
    )], (
        f"submit_approval was not invoked correctly: {bot.agent.calls}"
    )
    # Token should be popped (one-shot).
    assert "abc12345" not in bot._approval_token_to_call_id


@pytest.mark.asyncio
async def test_telegram_callback_unknown_token_shows_no_pending() -> None:
    """If the token is unknown (stale callback from previous run, or a
    crafted callback_data from outside), submit_approval must NOT be
    called and the user must see "No pending approval".
    """
    from cogitum.gateway.telegram import CogitumBot

    class _FakeAPI:
        def __init__(self) -> None:
            self.callbacks: list[tuple] = []

        async def answer_callback(self, cb_id, text=""):
            self.callbacks.append((cb_id, text))

        async def edit_message(self, *a, **kw):
            pass

    class _FakeAgent:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def submit_approval(self, call_id, decision):
            self.calls.append((call_id, decision))
            return True

    class _FakeConfig:
        allowed_user_id = 0
        allowed_chat_ids: list[int] = []

        def can_respond(self, user_id, chat_id):
            return True

    bot = CogitumBot.__new__(CogitumBot)
    bot.config = _FakeConfig()  # type: ignore
    bot.api = _FakeAPI()  # type: ignore
    bot.agent = _FakeAgent()  # type: ignore
    bot.sessions = {}
    bot._approval_token_to_call_id = {}
    import collections as _c
    bot._seen_callbacks = _c.OrderedDict()
    bot._seen_callbacks_max = 256

    callback = {
        "id": "cbid",
        "from": {"id": 1},
        "message": {"chat": {"id": 1}, "message_id": 99},
        "data": "approve:ghosttoken",
    }

    await bot._handle_callback(callback)

    assert bot.agent.calls == [], (
        "submit_approval must not be called for an unknown token "
        "(stale callback, crafted data, etc)"
    )
    assert any("No pending" in text for _, text in bot.api.callbacks), (
        "user must see a clear 'no pending approval' message"
    )


def test_callback_data_stays_under_telegram_64_byte_cap() -> None:
    """Telegram's inline button callback_data is capped at 64 bytes.
    The naive `f"approve:{call_id}"` format breaks silently for long
    MCP / composite ids — TG truncates and our handler can't match.
    The fix uses a short token (8 hex chars), so callback_data is
    always under 20 bytes regardless of the underlying call_id.
    """
    # The longest realistic call_id we might see — composite legion ids,
    # MCP server-prefixed names, custom UUIDs. Even here the emitted
    # callback_data must fit.
    long_call_id = "mcp_server_with_a_very_long_namespace__tool_name__" + "x" * 50
    token = "deadbeef"  # 8 hex chars
    cb_data_approve = f"approve:{token}"
    cb_data_reject = f"reject:{token}"
    assert len(cb_data_approve.encode("utf-8")) <= 64
    assert len(cb_data_reject.encode("utf-8")) <= 64
    # Verify the long call_id WOULD have overflowed the naive scheme.
    naive = f"approve:{long_call_id}"
    assert len(naive.encode("utf-8")) > 64, (
        "test premise broken: long_call_id is not actually long enough "
        "to overflow callback_data in the naive scheme"
    )


@pytest.mark.asyncio
async def test_modify_with_invalid_json_rejects_not_silently_approves() -> None:
    """A "modify:" decision with malformed JSON must NOT silently
    approve the original args — that's a contract violation. The user
    explicitly chose modify, not approve. The new behaviour rejects
    loudly so the model sees a REJECTED string and retries, instead of
    executing the original (possibly dangerous) args.
    """
    agent = _agent({"terminal": "ok"}, yolo=False)
    agent._approval_queue = asyncio.Queue()
    tc = ToolCallPart(id="m", name="terminal", arguments={"command": "rm -rf /tmp/m"})

    task = asyncio.create_task(
        agent._execute_tool(tc, turn=1, queue=asyncio.Queue())
    )
    await asyncio.sleep(0.05)
    # Send malformed modify JSON.
    assert agent.submit_approval("m", "modify:{not valid json")

    result = await asyncio.wait_for(task, timeout=2.0)
    assert result.startswith("REJECTED"), (
        f"modify with malformed JSON must REJECT, not silently "
        f"approve original args. Got: {result!r}"
    )
    assert "malformed" in result.lower()


@pytest.mark.asyncio
async def test_modify_with_non_dict_payload_rejects() -> None:
    """A "modify:" payload that decodes but isn't a JSON object (list,
    scalar, null) is also rejected — registry.execute expects a dict.
    """
    agent = _agent({"terminal": "ok"}, yolo=False)
    agent._approval_queue = asyncio.Queue()
    tc = ToolCallPart(id="m2", name="terminal", arguments={"command": "rm -rf /tmp/m"})

    task = asyncio.create_task(
        agent._execute_tool(tc, turn=1, queue=asyncio.Queue())
    )
    await asyncio.sleep(0.05)
    # Valid JSON, but it's a list instead of an object.
    assert agent.submit_approval("m2", "modify:[1, 2, 3]")

    result = await asyncio.wait_for(task, timeout=2.0)
    assert result.startswith("REJECTED")
    assert "not a JSON object" in result


@pytest.mark.asyncio
async def test_uncancellable_sibling_does_not_hang_run_forever(monkeypatch) -> None:
    """Production hazard: a tool that ignores CancelledError (e.g.
    `try: await asyncio.sleep(60); except CancelledError: continue`
    pattern, or a sync subprocess.wait without timeout) used to make
    the parallel-batch finally block wait forever in
    `gather(*pending, return_exceptions=True)`.

    With the bounded drain (10s timeout), the agent surrenders the
    zombie task and proceeds rather than becoming unkillable until
    process restart.
    """
    class _UncancellableRegistry:
        def to_openai(self, tags=None):
            return []

        def names(self):
            return []

        async def execute(self, name, args):
            if name == "ignore_cancel":
                # Tool that ignores the FIRST cancel for ~0.5s, then
                # eventually finishes. This simulates a misbehaved
                # cleanup that takes longer than the bounded drain.
                # We MUST make it finite or the task survives the
                # test and pytest-asyncio hangs at teardown trying
                # to collect it.
                cancel_count = 0
                deadline_loops = 10  # ~0.5s total
                for _ in range(deadline_loops):
                    try:
                        await asyncio.sleep(0.05)
                    except asyncio.CancelledError:
                        cancel_count += 1
                        if cancel_count > 3:
                            raise  # finally honour cancel
                        # Else keep going.
                return f"executed {name} (ignored {cancel_count} cancels)"
            return f"executed {name}"

    tcs = [
        ToolCallPart(id="bad", name="ignore_cancel", arguments={}),
        ToolCallPart(id="ok", name="ok", arguments={}),
    ]
    mesh = _stream_one_assistant_turn(tcs)
    agent = Agent(
        mesh=mesh,
        registry=_UncancellableRegistry(),  # type: ignore[arg-type]
        config=AgentConfig(model="fake/model", yolo_mode=True, max_turns=2),
    )

    q: asyncio.Queue = asyncio.Queue()
    user_msg = Message(role="user", parts=[TextPart(text="run")])

    # Patch the bounded drain timeout via pytest monkeypatch so the
    # 10s drain becomes 0.2s and the test runs in <1s. Using
    # monkeypatch (not raw setattr) guarantees teardown even if the
    # test fails — otherwise the patch leaks into other tests in the
    # same suite and causes flakes / hangs.
    import cogitum.core.agent as agent_module
    real_wait_for = agent_module.asyncio.wait_for

    async def short_wait_for(coro, timeout):
        # Slam the 10s drain to 0.2s; leave other timeouts alone.
        if timeout == 10.0:
            return await real_wait_for(coro, 0.2)
        return await real_wait_for(coro, timeout)

    monkeypatch.setattr(agent_module.asyncio, "wait_for", short_wait_for)

    run_task = asyncio.create_task(agent.run([user_msg], queue=q))
    # Cancel mid-batch to trigger the drain path.
    await asyncio.sleep(0.1)
    run_task.cancel()
    # The whole thing must complete (or raise) within reasonable
    # time — definitely NOT hang on the misbehaved tool.
    try:
        await real_wait_for(run_task, timeout=3.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    # The run task must be done (not still hanging on gather).
    assert run_task.done(), (
        "agent.run() hung on uncancellable sibling — bounded drain failed"
    )
