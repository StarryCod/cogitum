"""
cogitum.core.legion_worker
~~~~~~~~~~~~~~~~~~~~~~~~~~

Agent-backed worker callable for the Legion orchestrator.

The runtime in :mod:`cogitum.core.legion` is provider-agnostic — it
calls a single ``worker(node, run, send_message, spawn_l2)``
coroutine to drive each cogitator. This module is that coroutine.

What it does, per turn:

  1. Build the cogitator's system prompt:
       - Imperial Fists base persona for cogitators (terse,
         action-oriented, no preamble)
       - Realtime sibling roster (parent + siblings + children)
       - Inbox messages from siblings (drained on read)
       - Goal + context the parent passed down
  2. Stream one LLM turn through the mesh.
  3. Collect text + tool calls.
  4. Execute tools in parallel (same shape as the main agent loop).
       - Special handling for ``legion`` and ``legion_message`` —
         these route through the Legion runtime, not REGISTRY.
       - L2 nodes never see the ``legion`` tool in their schema, so
         they can't recurse beyond MAX_DEPTH=2.
  5. Feed results back, loop until text-only response or turn cap.
  6. Return final text — that becomes ``LegionNode.output``.

Concurrency control: each cogitator independently leases keys from
the mesh; the mesh's keypool already round-robins across keys, so we
don't need an extra semaphore at this layer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, TYPE_CHECKING

from .events import (
    ChunkKind, Message, TextPart, ThinkingPart,
    ToolCallPart, ToolResultPart,
)
from .legion import (
    LegionNode, LegionRun,
    render_inbox_for, render_roster_for,
)

if TYPE_CHECKING:
    from .llm.mesh import Mesh
    from .tools import ToolRegistry

logger = logging.getLogger(__name__)


# Hard caps for sub-cogitator agent loops. Generous but bounded —
# we don't want a runaway L2 to burn through 50 turns on one node.
_MAX_TURNS_PER_NODE = 12
_MAX_TOKENS_PER_TURN = 8192
_TOOL_RESULT_TRUNC = 4000          # tool output beyond this gets a [truncated] tail


# ─────────────────────────────────────────────────────────────────────────
# System prompt construction
# ─────────────────────────────────────────────────────────────────────────


_BASE_COGITATOR_PROMPT = """\
You are a Cogitator — a focused worker inside Cogitum's Legion. The
Magos (the user) issued a high-level task. The lead Cogitum split
that task into pieces and dispatched them to a parallel team of
Cogitators. You are one of those workers.

Your role: complete YOUR specific goal. Don't try to solve the
whole task — your siblings are handling the other parts.

Style:
  - Terse. Technical. No preamble, no hedging, no apology.
  - Match the language of the goal text.
  - When you finish your goal, emit a SHORT final answer (one to
    six sentences, or a single code block) describing what you did
    and the outcome. The lead Cogitum aggregates all sibling
    outputs — keep yours scannable.

Coordination with siblings:
  - The "LEGION ROSTER" block below shows what every other
    Cogitator in this run is doing in real time. Read it. Don't
    duplicate their work. If two of you are heading for the same
    file, use ``legion_message`` to coordinate.
  - The "INBOX" block (when present) carries messages your
    siblings sent you. They're hints / questions / coordination,
    not orders. Reply with your own ``legion_message`` if needed,
    or just incorporate the info into your work.

Tools:
  - You have the same tool catalog as the lead Cogitum, with two
    additions you should use sparingly:
      * ``legion_message(to, body)``  — send a sibling a short note.
      * ``legion(tasks=[...])``       — spawn up to 3 sub-Cogitators
        for genuinely independent subtasks. ONLY available to L1
        Cogitators (you'll see it in the schema if you have it).

When all your work is done, stop emitting tool calls and produce
your final answer. The output is what the lead Cogitum will see —
make it count, but make it brief."""


def _build_system_prompt(
    node: LegionNode,
    run: LegionRun,
    base_system_extra: str = "",
) -> str:
    """Compose the per-turn system prompt for a cogitator.

    Caller passes ``base_system_extra`` to layer additional context on
    top (e.g. project memory, skills summary) — the worker doesn't
    inject those itself, leaving that policy to the caller.
    """
    parts: list[str] = [_BASE_COGITATOR_PROMPT]

    # Goal + context block — the actual assignment for this node.
    parts.append("\n═══ YOUR ASSIGNMENT ═══")
    parts.append(f"Cogitator id: {node.id}  (depth {node.depth})")
    parts.append(f"Goal: {node.goal or '(no explicit goal)'}")
    if node.context:
        parts.append(f"Context from parent:\n{node.context}")

    # Realtime roster — siblings, parent, children. Generated per turn
    # so a cogitator sees status changes since its last turn.
    roster = render_roster_for(run, node.id)
    if roster:
        parts.append("\n" + roster)

    # Inbox is consumed on read. If there's nothing, we skip the
    # block entirely — no point burning tokens on an empty header.
    inbox = render_inbox_for(node)
    if inbox:
        parts.append("\n" + inbox)

    if base_system_extra:
        parts.append("\n" + base_system_extra.strip())

    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────
# Tool dispatch — separates legion-internal tools from REGISTRY tools
# ─────────────────────────────────────────────────────────────────────────


def _tools_schema_for(
    registry: "ToolRegistry",
    node: LegionNode,
    can_recurse: bool,
) -> list[dict]:
    """Return the OpenAI-format tool schema for this cogitator.

    ALL standard Cogitum tools are exposed (registry.to_openai()).
    Plus, for L1 cogitators (``can_recurse=True``), the ``legion``
    tool is appended so they can spawn sub-cogitators. ``legion_message``
    is always available so any node can talk to siblings.

    We DELIBERATELY remove ``delegate_task`` from the schema even if
    REGISTRY still has it: legion supersedes it, and we don't want
    cogitators to fall back into the old delegation path inside a
    swarm.
    """
    schema = [
        s for s in registry.to_openai()
        if s.get("function", {}).get("name") != "delegate_task"
    ]

    schema.append({
        "type": "function",
        "function": {
            "name": "legion_message",
            "description": (
                "Send a short coordination message to a sibling Cogitator. "
                "Use this when you need to tell a sibling about something "
                "you noticed (file overlap, useful context, blocking question). "
                "Delivery is async — you do NOT wait for a reply; the "
                "recipient sees your message in their next turn's inbox."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": (
                            "Cogitator id from the LEGION ROSTER. "
                            "Use \"*\" to broadcast to every sibling."
                        ),
                    },
                    "body": {
                        "type": "string",
                        "description": "Message body. Keep it under 200 words.",
                    },
                },
                "required": ["to", "body"],
            },
        },
    })

    if can_recurse:
        schema.append({
            "type": "function",
            "function": {
                "name": "legion",
                "description": (
                    "Spawn up to 3 sub-Cogitators (L2) to work on parts of "
                    "your goal in parallel. Returns once they ALL finish, "
                    "with a list of their outputs in order. ONLY use for "
                    "genuinely independent subtasks — sequential work is "
                    "faster as a single-actor loop. NOT available to L2 "
                    "Cogitators."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tasks": {
                            "type": "array",
                            "description": "List of {id, goal, context?} dicts.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "goal": {"type": "string"},
                                    "context": {"type": "string"},
                                },
                                "required": ["goal"],
                            },
                        },
                    },
                    "required": ["tasks"],
                },
            },
        })

    return schema


async def _execute_tool(
    tc: ToolCallPart,
    *,
    registry: "ToolRegistry",
    send_message: Callable[[str, str], None],
    spawn_l2: Callable[[list[dict]], Awaitable[list[str]]] | None,
) -> str:
    """Run a single tool call. Routes legion-internal calls separately.

    Returns the result text that will be fed back to the LLM as a
    tool message.
    """
    name = tc.name
    args = tc.arguments or {}

    # legion_message — fire-and-forget.
    if name == "legion_message":
        to = str(args.get("to", "")).strip()
        body = str(args.get("body", "")).strip()
        if not to:
            return "ERROR: legion_message requires 'to' (sibling id or '*')"
        if not body:
            return "ERROR: legion_message requires 'body'"
        send_message(to, body)
        return f"OK: message dispatched to {to}"

    # legion — spawn L2.
    if name == "legion":
        if spawn_l2 is None:
            return (
                "ERROR: legion is not available at this depth (you are L2). "
                "Complete your task directly without further delegation."
            )
        tasks = args.get("tasks") or []
        if not isinstance(tasks, list) or not tasks:
            return "ERROR: legion requires a non-empty 'tasks' array"
        try:
            outputs = await spawn_l2(tasks)
        except ValueError as e:
            return f"ERROR: {e}"
        # Format the same way the L0 summary does — predictable shape.
        lines: list[str] = [f"Sub-Cogitators ({len(outputs)}) returned:"]
        for i, t in enumerate(tasks):
            goal = t.get("goal", f"task-{i}")
            out = outputs[i] if i < len(outputs) else "(no output)"
            lines.append(f"\n── [{goal}] ──\n{out}")
        return "\n".join(lines)

    # Standard REGISTRY tool.
    spec = registry.get(name)
    if spec is None:
        return f"ERROR: unknown tool '{name}'"

    try:
        result = await spec.call(**args)
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"

    text = str(result)
    if len(text) > _TOOL_RESULT_TRUNC:
        text = text[:_TOOL_RESULT_TRUNC] + f"\n…[truncated; {len(text)} chars total]"
    return text


# ─────────────────────────────────────────────────────────────────────────
# Worker callable factory — one closure per Cogitum process
# ─────────────────────────────────────────────────────────────────────────


def make_legion_worker(
    *,
    mesh: "Mesh",
    registry: "ToolRegistry",
    model: str | Callable[[], str] | None = None,
    base_system_extra: str = "",
) -> Callable:
    """Build the worker callable that Legion will dispatch.

    ``mesh``, ``registry`` are captured by closure so the runtime
    layer doesn't have to know about them.

    ``model`` may be:
      * a static string (pin every cogitator to that model);
      * a zero-arg callable that returns the current model id (used
        when the lead agent's model can change mid-session via
        /model — the worker reads the live value on every turn);
      * None (let the mesh pick its default).

    Returns a coroutine matching :class:`cogitum.core.legion.WorkerCallable`.
    """
    from .llm.mesh import StreamRequest

    def _resolve_model() -> str:
        if callable(model):
            try:
                return (model() or "").strip()
            except Exception:
                return ""
        return (model or "").strip()

    async def _worker(
        node: LegionNode,
        run: LegionRun,
        send_message: Callable[[str, str], None],
        spawn_l2: Callable[[list[dict]], Awaitable[list[str]]] | None,
    ) -> str:
        can_recurse = spawn_l2 is not None
        tools_schema = _tools_schema_for(registry, node, can_recurse=can_recurse)

        history: list[Message] = [
            Message(role="user", parts=[TextPart(text=node.goal)])
        ]

        last_text = ""

        for turn in range(_MAX_TURNS_PER_NODE):
            system = _build_system_prompt(node, run, base_system_extra)

            # Update last_action so siblings see what we're up to.
            node.last_action = f"turn {turn + 1}/{_MAX_TURNS_PER_NODE}"
            run._emit("node_status",
                      node_id=node.id, status=node.status.value,
                      last_action=node.last_action)

            req = StreamRequest(
                messages=history,
                model=_resolve_model(),
                system=system,
                tools=tools_schema,
                max_tokens=_MAX_TOKENS_PER_TURN,
            )

            text_parts: list[str] = []
            tool_calls: list[ToolCallPart] = []
            stream_error: str = ""

            async for chunk in mesh.stream(req):
                if chunk.kind == ChunkKind.TEXT and chunk.text:
                    text_parts.append(chunk.text)
                    # Stream tokens to TUI subscribers — let the tree
                    # view show live progress for the active node.
                    run._emit("node_token", node_id=node.id, delta=chunk.text)
                elif chunk.kind == ChunkKind.TOOL_CALL_DONE:
                    tool_calls.append(ToolCallPart(
                        id=chunk.tool_call_id or f"tc-{turn}-{len(tool_calls)}",
                        name=chunk.tool_call_name or "",
                        arguments=chunk.tool_call_args or {},
                    ))
                elif chunk.kind == ChunkKind.ERROR and chunk.error:
                    # Capture the FIRST stream error per turn — that's
                    # almost always the cause; later errors are usually
                    # downstream symptoms (timeout cleanup, etc.).
                    if not stream_error:
                        stream_error = chunk.error

            # Hard mesh failure on a turn that produced nothing
            # otherwise → raise so the runtime marks the node FAILED
            # with a real error, instead of returning the error text
            # as a "done" output (which was misleading: status=done +
            # output="[stream error: ...]").
            if stream_error and not tool_calls and not "".join(text_parts).strip():
                raise RuntimeError(f"mesh stream failed: {stream_error}")

            text = "".join(text_parts).strip()
            if text:
                last_text = text

            # No tool calls → final answer turn. Done.
            if not tool_calls:
                break

            # Append assistant turn to history.
            assistant_parts: list[Any] = []
            if text:
                assistant_parts.append(TextPart(text=text))
            assistant_parts.extend(tool_calls)
            history.append(Message(role="assistant", parts=assistant_parts))

            # Execute tool calls in parallel.
            results = await asyncio.gather(*[
                _execute_tool(
                    tc, registry=registry,
                    send_message=send_message, spawn_l2=spawn_l2,
                )
                for tc in tool_calls
            ])

            tool_result_parts = [
                ToolResultPart(
                    tool_call_id=tc.id,
                    content=res,
                    is_error=res.startswith("ERROR:"),
                )
                for tc, res in zip(tool_calls, results)
            ]
            history.append(Message(role="tool", parts=tool_result_parts))

        return last_text or "(cogitator produced no output)"

    return _worker


__all__ = ["make_legion_worker"]
