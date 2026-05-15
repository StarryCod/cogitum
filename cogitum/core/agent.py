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
import re
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
# Retry / compaction constants
# ---------------------------------------------------------------------------

_MAX_RETRIES = 3
_BACKOFF_SECONDS = (1.0, 3.0, 9.0)  # exponential: 1s, 3s, 9s
_CONTEXT_FILL_THRESHOLD = 0.80  # compact at 80% context usage

_RETRYABLE_STATUS_RE = re.compile(r"\b(429|5\d{2})\b")
_RECOVERY_TIME_RE = re.compile(r"next recovery in\s+(\d+[.,]?\d*)\s*s?", re.IGNORECASE)


def _is_retryable_error(exc: BaseException) -> bool:
    """Determine if an error is transient and worth retrying."""
    msg = str(exc).lower()
    # Rate limit or server errors (429, 5xx)
    if _RETRYABLE_STATUS_RE.search(str(exc)):
        return True
    # All keys unavailable in pool (mesh/keypool cooldown)
    if "keys unavailable" in msg or "recovery" in msg:
        return True
    # Connection / timeout errors
    if any(kw in msg for kw in ("timeout", "timed out", "connection", "connect",
                                 "reset by peer", "eof", "broken pipe")):
        return True
    return False


def _parse_recovery_delay(exc: BaseException) -> float | None:
    """Extract recovery time from 'next recovery in Xs' error messages.

    Handles both '15.4' and '15,4' decimal formats.
    Returns the delay in seconds, or None if not found.
    """
    match = _RECOVERY_TIME_RE.search(str(exc))
    if match:
        # Normalize comma decimal separator to dot
        value_str = match.group(1).replace(",", ".")
        try:
            return float(value_str)
        except ValueError:
            return None
    return None


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
    preliminary: bool = False


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
        accumulated_tokens: int = 0
        iteration = 0

        try:
            while iteration < self.cfg.max_turns:
                iteration += 1

                # ── context compaction check ───────────────────────────────
                context_window = self._get_context_window()
                if (context_window > 0
                        and accumulated_tokens >= int(context_window * _CONTEXT_FILL_THRESHOLD)):
                    messages = await self._compact_context(messages, q)
                    old_tokens = accumulated_tokens
                    accumulated_tokens = 0  # reset after compaction
                    await q.put(AgentText(
                        delta=f"\n⟳ context compacted (was {old_tokens} tokens)\n",
                        turn=iteration,
                    ))

                assistant_text_parts: list[TextPart] = []
                assistant_thinking_parts: list[ThinkingPart] = []
                assistant_tool_calls: list[ToolCallPart] = []

                # pending streaming tool calls: call_id → {name, args_buf}
                pending: dict[str, dict[str, str]] = {}

                # ── stream one LLM turn (with retry) ──────────────────────
                async for chunk in self._stream_with_retry(messages, tools_schema, q, iteration):

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
                            # Emit preliminary event so TUI can show 'preparing...' card
                            await q.put(AgentToolCall(
                                tool_name=chunk.tool_call_name or "",
                                arguments={},
                                call_id=cid,
                                turn=iteration,
                                preliminary=True,
                            ))
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
                        if chunk.usage:
                            accumulated_tokens += (
                                (chunk.usage.input_tokens or 0)
                                + (chunk.usage.output_tokens or 0)
                            )

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
        from cogitum.core.llm.mesh import StreamRequest
        req = StreamRequest(
            messages=messages,
            model=self.cfg.model or "",
            system=self.cfg.system,
            tools=tools_schema,
            max_tokens=self.cfg.max_tokens,
            temperature=self.cfg.temperature,
        )
        async for chunk in self.mesh.stream(req):
            yield chunk

    async def _stream_with_retry(
        self,
        messages: list[Message],
        tools_schema: list[dict],
        queue: asyncio.Queue[AgentEvent],
        turn: int,
    ) -> AsyncIterator[StreamChunk]:
        """Wrap _stream() with retry + exponential backoff on transient errors."""
        last_exc: BaseException | None = None

        for attempt in range(_MAX_RETRIES + 1):
            try:
                async for chunk in self._stream(messages, tools_schema):
                    yield chunk
                return  # success — exit the retry loop
            except (RuntimeError, OSError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt >= _MAX_RETRIES or not _is_retryable_error(exc):
                    break
                # Use recovery time from error message if available,
                # otherwise fall back to default exponential backoff
                recovery_delay = _parse_recovery_delay(exc)
                delay = recovery_delay if recovery_delay is not None else _BACKOFF_SECONDS[attempt]
                log.warning(
                    "Stream attempt %d failed (%s), retrying in %.1fs",
                    attempt + 1, exc, delay,
                )
                await queue.put(AgentText(
                    delta=f"\n⟳ retrying (attempt {attempt + 2}) in {delay:.1f}s...\n",
                    turn=turn,
                ))
                await asyncio.sleep(delay)

        # All retries exhausted — raise the original error
        raise last_exc  # type: ignore[misc]

    def _get_context_window(self) -> int:
        """Return the context window size for the current model, or 0 if unknown."""
        model_ref = self.cfg.model or ""
        if not model_ref:
            return 0
        try:
            resolved = self.mesh.resolve(model_ref)
            if resolved:
                return resolved[0].model.context_window
        except Exception:
            pass
        return 0

    async def _compact_context(
        self,
        messages: list[Message],
        queue: asyncio.Queue[AgentEvent],
    ) -> list[Message]:
        """Summarize conversation to free context space."""
        from cogitum.core.llm.mesh import StreamRequest

        # Preserve the system message (first message if role is system-like)
        system_msg = self.cfg.system

        # Build compaction prompt with the full conversation
        conversation_text_parts: list[str] = []
        for msg in messages:
            role = msg.role
            for part in msg.parts:
                if isinstance(part, TextPart):
                    conversation_text_parts.append(f"[{role}]: {part.text}")
                elif isinstance(part, ToolCallPart):
                    conversation_text_parts.append(
                        f"[{role}]: tool_call({part.name}, {json.dumps(part.arguments)})"
                    )
                elif isinstance(part, ToolResultPart):
                    conversation_text_parts.append(
                        f"[tool_result]: {part.content[:500]}"
                    )

        conversation_dump = "\n".join(conversation_text_parts)
        compaction_prompt = (
            "Summarize this conversation preserving all key facts, decisions, "
            "code snippets, and context. Be thorough but concise.\n\n"
            f"{conversation_dump}"
        )

        # Stream the compaction (no tools)
        compaction_messages = [
            Message(role="user", parts=[TextPart(text=compaction_prompt)])
        ]
        req = StreamRequest(
            messages=compaction_messages,
            model=self.cfg.model or "",
            system="You are a precise summarizer. Preserve all important details.",
            tools=[],
            max_tokens=self.cfg.max_tokens,
            temperature=0.0,
        )

        summary_buf: list[str] = []
        async for chunk in self.mesh.stream(req):
            if chunk.kind == ChunkKind.TEXT:
                summary_buf.append(chunk.text)
            elif chunk.kind == ChunkKind.ERROR:
                log.warning("Compaction stream error: %s", chunk.error)
                return messages  # fall back to original on failure

        compacted_summary = "".join(summary_buf)
        if not compacted_summary.strip():
            return messages  # compaction produced nothing, keep original

        # Replace messages with compacted version
        return [
            Message(role="user", parts=[TextPart(text=compacted_summary)]),
            Message(role="assistant", parts=[TextPart(
                text="Understood. I have the full context. Continuing."
            )]),
        ]

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
