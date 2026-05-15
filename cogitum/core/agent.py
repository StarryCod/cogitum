"""
cogitum.core.agent
~~~~~~~~~~~~~~~~~~
Agentic loop: prompt → LLM stream → tool_calls → execute → next iteration.

Events emitted on the queue (all are dataclasses):
  AgentText      — streamed text delta
  AgentThinking  — streamed thinking delta (reasoning models)
  AgentToolCall  — tool invocation started
  AgentToolResult— tool result received
  AgentDone      — loop finished (final turn index + usage)
  AgentError     — unrecoverable error
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from cogitum.core.events import (
    ChunkKind,
    Message,
    Role,
    StreamChunk,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolResultPart,
    Turn,
    Usage,
)
from cogitum.core.llm.mesh import Mesh
from cogitum.core.tools import ToolRegistry

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Agent events (sent over asyncio.Queue to the TUI)
# ---------------------------------------------------------------------------

@dataclass
class AgentText:
    delta: str
    turn: int = 0


@dataclass
class AgentThinking:
    delta: str
    turn: int = 0


@dataclass
class AgentToolCall:
    tool_name: str
    arguments: dict[str, Any]
    call_id: str
    turn: int = 0


@dataclass
class AgentToolResult:
    tool_name: str
    call_id: str
    result: str
    error: bool = False
    turn: int = 0


@dataclass
class AgentDone:
    turns: int
    usage: Usage | None = None


@dataclass
class AgentError:
    message: str
    exc: BaseException | None = None


AgentEvent = AgentText | AgentThinking | AgentToolCall | AgentToolResult | AgentDone | AgentError

# ---------------------------------------------------------------------------
# Agent config
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    model: str | None = None          # override mesh default
    max_turns: int = 20               # hard cap on tool-call iterations
    max_tokens: int = 8192
    temperature: float | None = None
    system: str = (
        "You are Cogitum, a sovereign agentic assistant running inside a "
        "terminal UI. You have access to tools. Think step by step, use tools "
        "when needed, and produce concise, accurate answers."
    )
    tools_enabled: bool = True
    tool_tags: list[str] | None = None   # None = all tools


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class Agent:
    """
    Stateless agentic loop.  Each `run()` call is independent.
    Results are pushed onto `queue` as AgentEvent objects.
    """

    def __init__(
        self,
        mesh: Mesh,
        registry: ToolRegistry,
        config: AgentConfig | None = None,
    ) -> None:
        self.mesh = mesh
        self.registry = registry
        self.cfg = config or AgentConfig()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        user_message: str,
        history: list[Message] | None = None,
        queue: asyncio.Queue[AgentEvent] | None = None,
    ) -> list[Message]:
        """
        Run the agentic loop.

        Parameters
        ----------
        user_message : str
            The new user prompt.
        history : list[Message] | None
            Prior conversation messages (mutated in-place with new messages).
        queue : asyncio.Queue | None
            If provided, AgentEvent objects are pushed here as they arrive.

        Returns
        -------
        list[Message]
            Updated history including the new messages from this run.
        """
        q = queue or asyncio.Queue()
        messages: list[Message] = list(history or [])

        # Append user message
        messages.append(Message(role="user", parts=[TextPart(text=user_message)]))

        tools_schema = (
            self.registry.to_openai(self.cfg.tool_tags)
            if self.cfg.tools_enabled
            else []
        )

        total_usage: Usage | None = None
        iteration = 0

        try:
            while iteration < self.cfg.max_turns:
                iteration += 1

                assistant_text_parts: list[TextPart] = []
                assistant_thinking_parts: list[ThinkingPart] = []
                assistant_tool_calls: list[ToolCallPart] = []

                # pending streaming tool calls: call_id → {name, args_buf}
                pending: dict[str, dict[str, str]] = {}

                # ── stream one LLM turn ──────────────────────────────────
                async for chunk in self._stream(messages, tools_schema):

                    if chunk.kind == ChunkKind.TEXT:
                        delta = chunk.text
                        await q.put(AgentText(delta=delta, turn=iteration))
                        if assistant_text_parts:
                            assistant_text_parts[-1] = TextPart(
                                text=assistant_text_parts[-1].text + delta
                            )
                        else:
                            assistant_text_parts.append(TextPart(text=delta))

                    elif chunk.kind == ChunkKind.THINKING:
                        delta = chunk.thinking
                        await q.put(AgentThinking(delta=delta, turn=iteration))
                        if assistant_thinking_parts:
                            assistant_thinking_parts[-1] = ThinkingPart(
                                text=assistant_thinking_parts[-1].text + delta,
                                signature=chunk.thinking_signature,
                            )
                        else:
                            assistant_thinking_parts.append(
                                ThinkingPart(text=delta, signature=chunk.thinking_signature)
                            )

                    elif chunk.kind == ChunkKind.TOOL_CALL_DELTA:
                        cid = chunk.tool_call_id or f"call_{len(pending)}"
                        if cid not in pending:
                            pending[cid] = {
                                "name": chunk.tool_call_name or "",
                                "args_buf": "",
                            }
                        if chunk.tool_call_name:
                            pending[cid]["name"] = chunk.tool_call_name
                        pending[cid]["args_buf"] += chunk.tool_call_args_delta or ""

                    elif chunk.kind == ChunkKind.TOOL_CALL_DONE:
                        cid = chunk.tool_call_id or ""
                        if cid in pending:
                            tc_info = pending.pop(cid)
                        else:
                            # finalised without prior delta (some providers send all at once)
                            tc_info = {
                                "name": chunk.tool_call_name or "",
                                "args_buf": "",
                            }
                        # prefer fully-parsed args if provider sent them
                        if chunk.tool_call_args is not None:
                            args = chunk.tool_call_args
                        else:
                            try:
                                args = json.loads(tc_info["args_buf"] or "{}")
                            except json.JSONDecodeError:
                                args = {}

                        tc_part = ToolCallPart(
                            id=cid,
                            name=tc_info["name"],
                            arguments=args,
                        )
                        assistant_tool_calls.append(tc_part)
                        await q.put(AgentToolCall(
                            tool_name=tc_info["name"],
                            arguments=args,
                            call_id=cid,
                            turn=iteration,
                        ))

                    elif chunk.kind == ChunkKind.USAGE:
                        total_usage = chunk.usage

                    elif chunk.kind == ChunkKind.STOP:
                        break

                    elif chunk.kind == ChunkKind.ERROR:
                        raise RuntimeError(chunk.error or "stream error")

                # flush any pending tool calls that never got TOOL_CALL_DONE
                for cid, tc_info in pending.items():
                    try:
                        args = json.loads(tc_info["args_buf"] or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    tc_part = ToolCallPart(id=cid, name=tc_info["name"], arguments=args)
                    assistant_tool_calls.append(tc_part)
                    await q.put(AgentToolCall(
                        tool_name=tc_info["name"],
                        arguments=args,
                        call_id=cid,
                        turn=iteration,
                    ))

                # ── commit assistant message ─────────────────────────────
                all_parts = (
                    assistant_thinking_parts
                    + assistant_text_parts
                    + assistant_tool_calls
                )
                if all_parts:
                    messages.append(Message(role="assistant", parts=all_parts))

                # ── no tool calls → done ─────────────────────────────────
                if not assistant_tool_calls:
                    break

                # ── execute tools in parallel ────────────────────────────
                tasks = [
                    self._execute_tool(tc, iteration)
                    for tc in assistant_tool_calls
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                tool_result_parts: list[ToolResultPart] = []
                for tc, res in zip(assistant_tool_calls, results):
                    if isinstance(res, BaseException):
                        content = f"ERROR: {res}"
                        is_error = True
                    else:
                        content = str(res)
                        is_error = False

                    tool_result_parts.append(ToolResultPart(
                        tool_call_id=tc.id,
                        content=content,
                        is_error=is_error,
                    ))
                    await q.put(AgentToolResult(
                        tool_name=tc.name,
                        call_id=tc.id,
                        result=content,
                        error=is_error,
                        turn=iteration,
                    ))

                # tool results go in as a "tool" role message
                messages.append(Message(role="tool", parts=tool_result_parts))

            await q.put(AgentDone(turns=iteration, usage=total_usage))

        except Exception as exc:
            log.exception("Agent loop error")
            await q.put(AgentError(message=str(exc), exc=exc))

        return messages

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _stream(
        self,
        messages: list[Message],
        tools_schema: list[dict],
    ) -> AsyncIterator[StreamChunk]:
        """Delegate to mesh.stream() with current message history."""
        async for chunk in self.mesh.stream(
            messages=messages,
            model=self.cfg.model,
            max_tokens=self.cfg.max_tokens,
            temperature=self.cfg.temperature,
            system=self.cfg.system,
            tools=tools_schema if tools_schema else None,
        ):
            yield chunk

    async def _execute_tool(
        self,
        tc: ToolCallPart,
        turn: int,
    ) -> str:
        """Execute a single tool call and return its string result."""
        try:
            result = await self.registry.execute(tc.name, tc.arguments)
            return str(result)
        except KeyError:
            return f"ERROR: unknown tool '{tc.name}'"
        except Exception as exc:
            log.warning("Tool %s raised: %s", tc.name, exc)
            raise
