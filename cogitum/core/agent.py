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

import random as _random

_MAX_RETRIES = 8
_CONTEXT_FILL_THRESHOLD = 0.80  # compact at 80% context usage

_RETRYABLE_STATUS_RE = re.compile(r"\b(429|5\d{2})\b")
_RECOVERY_TIME_RE = re.compile(r"next recovery in\s+(\d+[.,]?\d*)\s*s?", re.IGNORECASE)
_RETRY_AFTER_RE = re.compile(r"\[Retry-After:\s*(\d+[.,]?\d*)\]")


def _jittered_backoff(attempt: int, base_delay: float = 3.0, max_delay: float = 60.0) -> float:
    """Exponential backoff with jitter (Hermes-style). Never returns 0."""
    exponent = max(0, attempt - 1)
    delay = min(base_delay * (2 ** exponent), max_delay)
    jitter = _random.uniform(0, 0.5 * delay)
    return delay + jitter


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
    """Extract recovery time from error messages.

    Sources (checked in order):
      1. [Retry-After: Xs] — from HTTP header, most authoritative
      2. 'next recovery in Xs' — from KeyPool NoKeyAvailable message

    Handles both '15.4' and '15,4' decimal formats.
    Returns None for values < 2s (forces backoff instead of busy-looping).
    Caps at 60s — if pool says 280s, we retry sooner (pool may recover earlier).
    """
    msg = str(exc)

    # Try Retry-After header first (most authoritative)
    match = _RETRY_AFTER_RE.search(msg)
    if not match:
        match = _RECOVERY_TIME_RE.search(msg)

    if match:
        value_str = match.group(1).replace(",", ".")
        try:
            val = float(value_str)
            if val < 2.0:
                return None  # fall through to exponential backoff
            return min(val, 60.0)
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
class AgentRetry:
    """Silent retry — TUI shows a friendly status instead of error text."""
    attempt: int
    max_attempts: int
    delay: float
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


AgentEvent = AgentText | AgentThinking | AgentRetry | AgentToolCall | AgentToolResult | AgentDone | AgentError

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
        "You are Cogitum, a sovereign agentic assistant. You run inside a terminal "
        "TUI or Telegram gateway. You have full tool access and persistent memory "
        "across sessions. You are direct, concise, and action-oriented.\n\n"

        "═══ CORE PRINCIPLES ═══\n"
        "• Default to action — implement changes rather than suggesting them.\n"
        "• Read code before making claims about it. Verify before presenting results.\n"
        "• If an approach fails twice, step back and try a fundamentally different one.\n"
        "• Match the user's language and communication style.\n"
        "• Admit uncertainty. Don't present assumptions as facts.\n\n"

        "═══ MEMORY — your persistent brain ═══\n"
        "Save durable facts proactively using the memory tool:\n"
        "• User corrects you or says 'remember this' → save immediately.\n"
        "• User shares preferences, habits, project conventions → save.\n"
        "• You discover environment facts (OS, tools, project structure) → save.\n"
        "• Write memories as declarative facts, not instructions to yourself.\n"
        "  Good: 'Project uses pytest with xdist' — Bad: 'Run tests with pytest -n 4'\n"
        "• Priority: user preferences > environment facts > procedural knowledge.\n"
        "• Keep entries compact. Don't save task progress or temporary state.\n\n"

        "═══ SKILLS — your procedural memory ═══\n"
        "Skills are reusable workflows for recurring tasks.\n"
        "• BEFORE a task: skills(action='list') to find relevant skills. Load with "
        "skills(action='read', name='...'). Follow the skill's instructions.\n"
        "• AFTER solving a hard problem (5+ tool calls, errors overcome, non-obvious "
        "workflow): offer to save as a skill. Include: trigger conditions, numbered "
        "steps, pitfalls, verification.\n"
        "• If a skill you used was wrong or incomplete: UPDATE IT immediately.\n"
        "• Create project-specific skills to adapt to the codebase (build commands, "
        "test patterns, deploy flows, architecture decisions).\n"
        "• Skills > general knowledge. Always prefer a skill's approach over guessing.\n\n"

        "═══ ADAPTIVE BEHAVIOR ═══\n"
        "• First time in a project: read config files (package.json, pyproject.toml, "
        "Makefile, etc.) to understand build tools, test runners, linters.\n"
        "• Match the project's style, conventions, and libraries — don't introduce new ones.\n"
        "• After completing work, run the project's build/test step to verify.\n"
        "• When making recommendations, explain your reasoning.\n"
        "• For safety-sensitive changes (auth, infra, data), state what was verified "
        "and what could not be verified.\n\n"

        "═══ TOOLS ═══\n"
        "DELEGATE — for complex multi-part tasks:\n"
        "• delegate_task spawns parallel sub-agents with full tool access.\n"
        "• Use workers mode for independent subtasks, experts mode for review.\n\n"

        "COGIT — smart checkpoints:\n"
        "• Auto-saves before dangerous operations (write_file overwrite, rm, git reset).\n"
        "• Save manually: cogit(action='save', label='...')\n"
        "• Restore: cogit(action='restore', index=N)\n"
        "• List: cogit(action='list')\n\n"

        "WEB — search and browse:\n"
        "• web_search(query='...') — DuckDuckGo, no API key needed.\n"
        "• browser(action='open', url='...') — Playwright headless Chromium.\n"
        "  Actions: open, click, type, text, screenshot, scroll, close.\n"
        "• fetch_url(url='...') — quick fetch + HTML strip for simple pages.\n\n"

        "MEDIA — send files to user (Telegram gateway only):\n"
        "• send_media(path='/path/to/file.png') — send photo or document.\n"
        "• Auto-detects type from extension (.png/.jpg/.webp → photo, else → document).\n"
        "• Use after generating images, screenshots, or files the user needs.\n\n"

        "═══ WORKFLOW ═══\n"
        "1. Understand the request. Ask clarifying questions only if truly ambiguous.\n"
        "2. Check memory and skills for relevant context.\n"
        "3. Plan if complex (3+ steps). Act immediately if simple.\n"
        "4. Execute with tools. Verify results.\n"
        "5. Save learnings to memory/skills if non-trivial.\n"
        "6. Report concisely. Don't over-explain obvious results.\n"
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
        self._active_tool_tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        user_message: str,
        history: list[Message] | None = None,
        queue: asyncio.Queue[AgentEvent] | None = None,
        inject_queue: asyncio.Queue[str] | None = None,
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
        inject_queue : asyncio.Queue[str] | None
            If provided, user messages placed here are injected into the
            conversation between tool-call iterations (not after the whole
            loop finishes). This lets the TUI feed queued messages to the
            agent mid-turn.

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
                        # "cancelled" is not a real error — it's user-initiated stop
                        if "cancelled" in (chunk.error or "").lower():
                            break
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

                # ── execute tools in parallel (cancellable) ────────────────
                tool_tasks = [
                    asyncio.create_task(self._execute_tool_indexed(i, tc, iteration))
                    for i, tc in enumerate(assistant_tool_calls)
                ]
                # Expose tasks so TUI can cancel them on Esc
                self._active_tool_tasks = tool_tasks

                # Collect results as they complete (stream to UI immediately)
                tool_result_parts: list[ToolResultPart] = [None] * len(assistant_tool_calls)

                try:
                    for coro in asyncio.as_completed(tool_tasks):
                        idx, result = await coro
                        tc = assistant_tool_calls[idx]
                        content = str(result)
                        is_error = content.startswith("ERROR:")

                        # Handle delegate_task async execution
                        if content.startswith("DELEGATE_WORKERS:"):
                            content = await self._run_delegate_workers(content[17:])
                            is_error = False
                        elif content.startswith("DELEGATE_EXPERTS:"):
                            content = await self._run_delegate_experts(content[17:])
                            is_error = False

                        tool_result_parts[idx] = ToolResultPart(
                            tool_call_id=tc.id,
                            content=content,
                            is_error=is_error,
                        )
                        # Stream result to UI immediately
                        await q.put(AgentToolResult(
                            tool_name=tc.name,
                            call_id=tc.id,
                            result=content,
                            error=is_error,
                            turn=iteration,
                        ))
                finally:
                    self._active_tool_tasks = []

                # Filter out None (shouldn't happen but safety)
                tool_result_parts = [p for p in tool_result_parts if p is not None]

                # tool results go in as a "tool" role message
                messages.append(Message(role="tool", parts=tool_result_parts))

                # ── inject queued user messages between iterations ────────
                if inject_queue:
                    while not inject_queue.empty():
                        try:
                            injected_text = inject_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        messages.append(Message(role="user", parts=[TextPart(text=injected_text)]))
                        await q.put(AgentText(
                            delta=f"\n📨 injected: {injected_text[:60]}{'…' if len(injected_text) > 60 else ''}\n",
                            turn=iteration,
                        ))

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
        from cogitum.core.memory import get_memory_context

        # Inject persistent memory into system prompt
        system = self.cfg.system
        mem_ctx = get_memory_context()
        if mem_ctx:
            system = f"{system}\n\n{mem_ctx}"

        # Inject skills summary (compact list of available skills)
        from cogitum.core.skills import skill_summary
        skills_ctx = skill_summary()
        if skills_ctx:
            system = f"{system}\n\n{skills_ctx}"

        req = StreamRequest(
            messages=messages,
            model=self.cfg.model or "",
            system=system,
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
        """Wrap _stream() with retry + exponential backoff on transient errors.

        Handles both raised exceptions AND ChunkKind.ERROR chunks from mesh
        (e.g. 'all keys unavailable' which comes as an ERROR chunk, not exception).

        IMPORTANT: Once any content has been yielded (text, thinking, tool_call),
        we do NOT retry — partial content is already in the TUI. Only retry on
        errors that happen BEFORE any content arrives (connection errors, rate
        limits during handshake, etc.).
        """
        last_exc: BaseException | None = None

        for attempt in range(_MAX_RETRIES + 1):
            got_error_chunk = False
            error_msg = ""
            has_yielded_content = False
            try:
                async for chunk in self._stream(messages, tools_schema):
                    # Intercept ERROR chunks — check if retryable before yielding
                    if chunk.kind == ChunkKind.ERROR:
                        error_msg = chunk.error or "stream error"
                        fake_exc = RuntimeError(error_msg)
                        # Only retry if NO content has been yielded yet
                        if (not has_yielded_content
                                and _is_retryable_error(fake_exc)
                                and attempt < _MAX_RETRIES):
                            got_error_chunk = True
                            last_exc = fake_exc
                            break  # break inner loop to retry
                        # Content already yielded or not retryable — yield as-is
                        yield chunk
                        continue
                    # Track whether we've sent any real content to the TUI
                    if chunk.kind in (ChunkKind.TEXT, ChunkKind.THINKING,
                                      ChunkKind.TOOL_CALL_DELTA, ChunkKind.TOOL_CALL_DONE):
                        has_yielded_content = True
                    yield chunk
                if not got_error_chunk:
                    return  # success — exit the retry loop
            except (RuntimeError, OSError, asyncio.TimeoutError, Exception) as exc:
                last_exc = exc
                # If content was already yielded, don't retry — it would duplicate
                if has_yielded_content:
                    raise
                if attempt >= _MAX_RETRIES or not _is_retryable_error(exc):
                    raise
                error_msg = str(exc)

            # Retry logic — parse recovery delay or use exponential backoff
            recovery_delay = _parse_recovery_delay(last_exc) if last_exc else None
            delay = recovery_delay if recovery_delay is not None else _jittered_backoff(attempt + 1)
            log.warning(
                "Stream attempt %d failed (%s), retrying in %.1fs",
                attempt + 1, error_msg, delay,
            )
            # Notify TUI about retry (friendly status, not raw error)
            await queue.put(AgentRetry(
                attempt=attempt + 1,
                max_attempts=_MAX_RETRIES,
                delay=delay,
                turn=turn,
            ))
            # Sleep in small increments so CancelledError (Esc) is responsive
            slept = 0.0
            while slept < delay:
                step = min(0.5, delay - slept)
                await asyncio.sleep(step)
                slept += step

        # All retries exhausted — raise the original error
        if last_exc:
            raise last_exc
        raise RuntimeError("stream failed after retries")

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

    async def _execute_tool_indexed(
        self,
        index: int,
        tc: ToolCallPart,
        turn: int,
    ) -> tuple[int, str]:
        """Execute a tool and return (index, result) for as_completed matching."""
        result = await self._execute_tool(tc, turn)
        return (index, result)

    async def _execute_tool(
        self,
        tc: ToolCallPart,
        turn: int,
    ) -> str:
        """Execute a single tool call and return its string result."""
        try:
            result = await asyncio.wait_for(
                self.registry.execute(tc.name, tc.arguments),
                timeout=120.0,  # 2 min max per tool
            )
            return str(result)
        except asyncio.TimeoutError:
            return f"ERROR: tool '{tc.name}' timed out after 120s"
        except asyncio.CancelledError:
            return "ERROR: tool execution cancelled by user"
        except KeyError:
            return f"ERROR: unknown tool '{tc.name}'"
        except Exception as exc:
            log.warning("Tool %s raised: %s", tc.name, exc)
            return f"ERROR: {type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------
    # Delegate task execution
    # ------------------------------------------------------------------

    async def _run_delegate_workers(self, payload_json: str) -> str:
        """Execute parallel worker agents."""
        import json
        from .delegate import run_workers, WorkerTask
        from .tools import REGISTRY

        try:
            task_list = json.loads(payload_json)
        except json.JSONDecodeError as e:
            return f"ERROR: invalid delegate payload: {e}"

        tasks = []
        for t in task_list:
            tasks.append(WorkerTask(
                id=t.get("id", f"task-{len(tasks)}"),
                goal=t.get("goal", ""),
                context=t.get("context", ""),
                model=t.get("model", "") or (self.cfg.model or ""),
            ))

        results = await run_workers(
            tasks, mesh=self.mesh, max_concurrent=10,
            max_tokens=self.cfg.max_tokens,
            tools_registry=REGISTRY,
        )

        # Format results
        lines = []
        for r in results:
            status = "✓" if r.success else "✗"
            lines.append(f"[{status}] {r.task_id} ({r.elapsed:.1f}s):")
            if r.success:
                lines.append(r.output[:2000])
            else:
                lines.append(f"  ERROR: {r.error}")
            lines.append("")

        return f"Workers completed ({sum(1 for r in results if r.success)}/{len(results)} success):\n\n" + "\n".join(lines)

    async def _run_delegate_experts(self, payload_json: str) -> str:
        """Execute expert review board."""
        import json
        from .delegate import run_expert_review
        from .tools import REGISTRY

        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as e:
            return f"ERROR: invalid delegate payload: {e}"

        content = payload.get("content", "")
        experts = payload.get("experts", [])
        # Use model from payload, fall back to agent's configured model
        model = payload.get("model", "") or (self.cfg.model or "")

        results = await run_expert_review(
            content=content,
            experts=experts or None,
            mesh=self.mesh,
            model=model,
            max_tokens=2048,
            tools_registry=REGISTRY,
        )

        # Format results
        lines = ["Expert Review Board Results:", ""]
        for role, feedback in results.items():
            lines.append(f"━━━ {role.upper()} ━━━")
            lines.append(feedback[:1500])
            lines.append("")

        return "\n".join(lines)
