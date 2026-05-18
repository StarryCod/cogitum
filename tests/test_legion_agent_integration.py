"""End-to-end integration test: Agent → legion tool → Legion runtime → cogitator → output.

Exercises the full glue: tool registration, sentinel handoff in the
agent loop, worker callable, sibling roster + inbox in the system
prompt, and aggregated summary feedback to the L0 agent.

Mocks the LLM via a deterministic Mesh stub — no network, no model
roulette. Two scripted "models":

  * lead_model: emits a `legion` tool call on turn 1, then a final
    text answer on turn 2 once the legion summary is back.
  * cogitator_model: each cogitator just echoes its goal as final
    text — no tools, no recursion. Keeps the test simple while
    still exercising the wiring.
"""
from __future__ import annotations

import asyncio
import json
import pytest
from typing import AsyncIterator

from cogitum.core.events import (
    ChunkKind, StreamChunk, Usage,
    Message, TextPart, ToolCallPart,
)


# ─────────────────────────────────────────────────────────────────────────
# Mock Mesh
# ─────────────────────────────────────────────────────────────────────────


class _MockMesh:
    """Mesh stub that routes by detecting whether the system prompt
    contains the cogitator base prompt (legion_worker) vs the lead
    Cogitum prompt. Returns scripted chunk streams."""

    def __init__(self) -> None:
        self.lead_turn = 0
        self.legion_run_id_seen = False

    def list_resolved(self) -> list:
        return []

    def resolve(self, qid: str):
        return None

    async def stream(self, req) -> AsyncIterator[StreamChunk]:
        is_cogitator = "You are a Cogitator" in (req.system or "")

        if is_cogitator:
            async for c in self._cogitator_response(req):
                yield c
        else:
            async for c in self._lead_response(req):
                yield c

    async def _lead_response(self, req) -> AsyncIterator[StreamChunk]:
        """Lead Cogitum: turn 1 emit legion tool call; turn 2 finalise."""
        self.lead_turn += 1
        if self.lead_turn == 1:
            yield StreamChunk(kind=ChunkKind.TEXT, text="Splitting work across cogitators.")
            yield StreamChunk(
                kind=ChunkKind.TOOL_CALL_DONE,
                tool_call_id="lead-tc-1",
                tool_call_name="legion",
                tool_call_args={
                    "tasks": json.dumps([
                        {"id": "alpha", "goal": "task-A"},
                        {"id": "beta", "goal": "task-B"},
                    ]),
                    "root_goal": "test root",
                },
            )
            yield StreamChunk(kind=ChunkKind.STOP, stop_reason="tool_use")
            return
        # turn 2: legion summary is now in the message history; finalise.
        yield StreamChunk(kind=ChunkKind.TEXT, text="DONE: cogitators reported back.")
        yield StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn")

    async def _cogitator_response(self, req) -> AsyncIterator[StreamChunk]:
        """Each cogitator: extract its goal from system prompt, echo it."""
        sys = req.system or ""
        # Find the "Goal:" line written by legion_worker._build_system_prompt.
        goal = "?"
        for line in sys.splitlines():
            line = line.strip()
            if line.startswith("Goal:"):
                goal = line.split(":", 1)[1].strip()
                break
        yield StreamChunk(kind=ChunkKind.TEXT, text=f"completed: {goal}")
        yield StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn")


# ─────────────────────────────────────────────────────────────────────────
# Test fixture: build a real Agent against the mock mesh
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _enable_legion(_isolated_config):
    """Legion is gated behind experimental.legion_enabled in settings.toml.
    Tests need the flag ON.

    Order matters here: the conftest fixture clears sys.modules before
    yielding cfg, but cogitum.core.builtin_tools captures
    _LEGION_ENABLED at import time. Sequence:
      1. conftest clears modules + sets COGITUM_CONFIG_DIR
      2. our fixture writes settings.toml into the resolved config dir
      3. clear modules again so the next builtin_tools import re-reads
         the just-written settings
    """
    cfg_root = _isolated_config / "cogitum"
    cfg_root.mkdir(parents=True, exist_ok=True)
    (cfg_root / "settings.toml").write_text(
        "[experimental]\nlegion_enabled = true\n", encoding="utf-8"
    )
    # Step 3 — invalidate any cogitum modules that may have been
    # imported between the conftest setup and now (pytest's fixture
    # graph may pull in cogitum.* indirectly).
    import sys
    for m in list(sys.modules):
        if m.startswith("cogitum"):
            sys.modules.pop(m, None)
    yield


@pytest.fixture
def real_agent():
    from cogitum.core.agent import Agent, AgentConfig
    from cogitum.core.tools import REGISTRY

    mesh = _MockMesh()
    cfg = AgentConfig(model="mock", platform="cli")
    cfg.tools_enabled = True
    agent = Agent(mesh=mesh, registry=REGISTRY, config=cfg)
    return agent, mesh


# ─────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lead_agent_dispatches_legion_and_receives_summary(real_agent):
    """Full round-trip: lead → legion tool → 2 cogitators → summary → final answer."""
    agent, mesh = real_agent
    queue: asyncio.Queue = asyncio.Queue()
    user_msg = "Magos's order: do A and B in parallel."

    # Drive the agent. The loop will internally call _run_legion when
    # the tool returns LEGION_RUN:..., which spawns 2 cogitators.
    await agent.run(user_message=user_msg, queue=queue)

    # Drain queue events; we mostly care that the run finished and
    # the legion summary made it back into the lead's context.
    events: list = []
    while not queue.empty():
        events.append(queue.get_nowait())

    kinds = [type(e).__name__ for e in events]
    assert "AgentToolCall" in kinds
    assert "AgentToolResult" in kinds
    assert "AgentDone" in kinds

    # The tool result for the `legion` call must carry both
    # cogitators' outputs.
    from cogitum.core.agent import AgentToolResult
    legion_results = [
        e for e in events
        if isinstance(e, AgentToolResult) and e.tool_name == "legion"
    ]
    assert legion_results, "no legion tool result in event stream"
    body = legion_results[0].result
    assert "[alpha]" in body
    assert "[beta]" in body
    assert "completed: task-A" in body
    assert "completed: task-B" in body
    assert "Legion run-" in body  # header line from _run_legion


@pytest.mark.asyncio
async def test_legion_tool_in_main_schema():
    """The `legion` tool must be visible to the LLM via REGISTRY.to_openai()."""
    from cogitum.core.tools import REGISTRY

    schema = REGISTRY.to_openai()
    names = [s["function"]["name"] for s in schema]
    assert "legion" in names

    legion_def = next(s for s in schema if s["function"]["name"] == "legion")
    params = legion_def["function"]["parameters"]
    assert "tasks" in params["properties"]
    assert "tasks" in params["required"]


@pytest.mark.asyncio
async def test_cogitator_sees_sibling_roster_in_prompt():
    """Each cogitator in a swarm must see its siblings in the roster
    block injected by legion_worker._build_system_prompt."""
    from cogitum.core.legion import Legion, NodeStatus

    captured_prompts: dict[str, str] = {}

    async def capture_worker(node, run, send_message, spawn_l2):
        # Pretend we're rendering a turn — run the same prompt builder
        # the real worker does, then stop.
        from cogitum.core.legion_worker import _build_system_prompt
        captured_prompts[node.id] = _build_system_prompt(node, run)
        return f"done: {node.goal}"

    legion = Legion()
    legion.register_worker(capture_worker)

    await legion.start_run(
        root_goal="parent task",
        tasks=[
            {"id": "A", "goal": "alpha goal"},
            {"id": "B", "goal": "beta goal"},
            {"id": "C", "goal": "gamma goal"},
        ],
    )

    # A's prompt should mention its goal, parent root_goal, and siblings B/C.
    a_prompt = captured_prompts["A"]
    assert "alpha goal" in a_prompt
    assert "parent task" in a_prompt
    assert "[B]" in a_prompt
    assert "[C]" in a_prompt
    assert "YOU" in a_prompt


@pytest.mark.asyncio
async def test_cogitator_inbox_messages_appear_in_prompt():
    """A message dropped in a cogitator's inbox must surface in its
    next system prompt build."""
    from cogitum.core.legion import Legion
    from cogitum.core.legion_worker import _build_system_prompt

    seen_prompts: dict[str, list[str]] = {"B": []}

    async def worker(node, run, send_message, spawn_l2):
        if node.id == "A":
            send_message("B", "hello B from A")
            await asyncio.sleep(0.05)  # let delivery land
            return "A done"
        if node.id == "B":
            await asyncio.sleep(0.02)
            seen_prompts["B"].append(_build_system_prompt(node, run))
            return "B done"
        return "?"

    legion = Legion()
    legion.register_worker(worker)
    await legion.start_run(
        root_goal="r",
        tasks=[{"id": "A", "goal": "alpha"}, {"id": "B", "goal": "beta"}],
    )

    # B's prompt should include the inbox message from A.
    assert seen_prompts["B"]
    assert "hello B from A" in seen_prompts["B"][0]
    assert "INBOX" in seen_prompts["B"][0]
