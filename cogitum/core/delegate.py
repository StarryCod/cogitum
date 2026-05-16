"""
cogitum.core.delegate
~~~~~~~~~~~~~~~~~~~~~
Delegate task — spawn parallel sub-agents for complex work.

Two modes:
  1. Workers — N parallel agents, each doing their own task (with tool access)
  2. Experts (Review Board) — N observer agents watching the orchestrator,
     each with a unique perspective (security, scale, UX, UI, optimization, frontend)

Features:
  - Cooperative retry: siblings keep retrying 429 / "all keys unavailable" as long
    as at least one sibling is still active. Exponential backoff with jitter.
  - Mini agent loop for workers: stream → detect tool_calls → execute → loop.
  - Depth-limited recursive delegation (max depth 2).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

from .events import (
    Message, TextPart, ToolCallPart, ToolResultPart,
    ChunkKind,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORKER_STAGGER_DELAY = 2.0   # seconds between each worker start
BACKOFF_BASE = 5.0           # base delay for exponential backoff
BACKOFF_CAP = 60.0           # max delay cap
MAX_TOOL_TURNS = 10          # max tool-use iterations per worker
MAX_DELEGATE_DEPTH = 2       # main(0) → sub(1) → sub-sub(2), no deeper


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass
class WorkerTask:
    """A task for a parallel worker agent."""
    id: str
    goal: str
    context: str = ""
    model: str = ""  # override, empty = use default


@dataclass
class WorkerResult:
    """Result from a completed worker."""
    task_id: str
    goal: str
    output: str
    success: bool
    elapsed: float = 0.0
    error: str = ""


@dataclass
class ExpertProfile:
    """An expert reviewer with a specific focus."""
    name: str
    role: str
    system_prompt: str


# Pre-defined expert profiles
EXPERT_PROFILES: dict[str, ExpertProfile] = {
    "security": ExpertProfile(
        name="Security Expert",
        role="security",
        system_prompt=(
            "You are a security expert reviewing code and architecture decisions. "
            "Focus on: authentication, authorization, input validation, injection attacks, "
            "secrets management, SSRF, path traversal, privilege escalation. "
            "Flag any security concern immediately. Be specific about the vulnerability "
            "and suggest a fix."
        ),
    ),
    "scale": ExpertProfile(
        name="Scale Engineer",
        role="scale",
        system_prompt=(
            "You are a scalability engineer reviewing architecture decisions. "
            "Focus on: unbounded growth, memory leaks, O(n²) algorithms, blocking I/O, "
            "connection pooling, caching strategy, rate limiting, backpressure. "
            "Flag anything that won't scale to 10x current load."
        ),
    ),
    "optimization": ExpertProfile(
        name="Performance Expert",
        role="optimization",
        system_prompt=(
            "You are a performance optimization expert. "
            "Focus on: unnecessary allocations, hot paths, I/O batching, lazy evaluation, "
            "caching opportunities, redundant computation, async vs sync choices. "
            "Suggest concrete optimizations with expected impact."
        ),
    ),
    "ux": ExpertProfile(
        name="UX Expert",
        role="ux",
        system_prompt=(
            "You are a UX expert reviewing user-facing behavior. "
            "Focus on: error messages clarity, loading states, feedback loops, "
            "progressive disclosure, recovery from errors, undo capability, "
            "consistency of interactions. Flag confusing or frustrating UX patterns."
        ),
    ),
    "ui": ExpertProfile(
        name="UI Expert",
        role="ui",
        system_prompt=(
            "You are a UI design expert reviewing visual implementation. "
            "Focus on: layout consistency, color harmony, typography hierarchy, "
            "spacing rhythm, visual noise, contrast ratios, responsive behavior. "
            "Flag visual inconsistencies or accessibility issues."
        ),
    ),
    "frontend": ExpertProfile(
        name="Frontend Expert",
        role="frontend",
        system_prompt=(
            "You are a frontend architecture expert. "
            "Focus on: component structure, state management, event handling, "
            "render performance, widget lifecycle, CSS specificity, "
            "separation of concerns. Flag anti-patterns and suggest refactors."
        ),
    ),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_retryable_error(error_text: str) -> bool:
    """Check if an error is retryable (429 or all keys unavailable)."""
    lower = error_text.lower()
    return any(s in lower for s in (
        "429", "rate limit", "rate_limit", "too many requests", "rpm",
        "all keys unavailable", "keys unavailable", "key pool",
        "keypool", "no keys available", "cooldown",
    ))


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter. Base 5s, cap 60s."""
    delay = min(BACKOFF_BASE * (2 ** attempt), BACKOFF_CAP)
    jitter = random.uniform(0, delay * 0.3)
    return delay + jitter


# ---------------------------------------------------------------------------
# Worker execution (with mini agent loop + cooperative retry)
# ---------------------------------------------------------------------------

async def run_workers(
    tasks: list[WorkerTask],
    mesh: Any,  # Mesh instance
    max_concurrent: int = 10,
    system: str = "",
    max_tokens: int = 4096,
    tools_registry: Any = None,  # ToolRegistry instance, enables tool use
) -> list[WorkerResult]:
    """Run multiple worker agents in parallel with staggered starts.

    Each worker gets its own conversation context and runs independently.
    Workers are started with a delay between each to avoid RPM limits.

    Cooperative retry: on 429 / "all keys unavailable", a worker keeps retrying
    as long as at least one sibling is still active (not failed). Only gives up
    when ALL siblings have also failed.

    Mini agent loop: if tools_registry is provided, workers can use tools.
    They stream a response, detect tool_calls, execute them, feed results back,
    and loop until a text response or max_turns.
    """
    semaphore = asyncio.Semaphore(max_concurrent)
    results: list[WorkerResult | None] = [None] * len(tasks)

    # Shared state for cooperative retry
    active_count = asyncio.Event()
    active_count.set()  # at least one is active initially
    # Track how many workers are still running (not permanently failed)
    active_workers: set[int] = set(range(len(tasks)))
    active_lock = asyncio.Lock()

    async def _mark_done(idx: int) -> None:
        """Mark a worker as done (success or permanent failure)."""
        async with active_lock:
            active_workers.discard(idx)
            if not active_workers:
                active_count.clear()

    def _any_sibling_active(idx: int) -> bool:
        """Check if any sibling worker is still active."""
        return any(i != idx and i in active_workers for i in range(len(tasks)))

    # Build tools schema if registry provided
    tools_schema = None
    if tools_registry is not None:
        tools_schema = tools_registry.to_openai()

    async def _stream_response(messages: list[Message], model: str, sys_prompt: str, max_tok: int):
        """Stream a single LLM request. Returns (text_output, tool_calls, errors)."""
        from .llm.mesh import StreamRequest
        req = StreamRequest(
            messages=messages,
            model=model,
            system=sys_prompt,
            tools=tools_schema,
            max_tokens=max_tok,
        )

        output_parts: list[str] = []
        tool_calls: list[ToolCallPart] = []
        error_parts: list[str] = []

        async for chunk in mesh.stream(req):
            if chunk.kind == ChunkKind.TEXT and chunk.text:
                output_parts.append(chunk.text)
            elif chunk.kind == ChunkKind.THINKING and chunk.thinking:
                output_parts.append(chunk.thinking)
            elif chunk.kind == ChunkKind.TOOL_CALL_DONE:
                tc = ToolCallPart(
                    id=chunk.tool_call_id or f"tc-{len(tool_calls)}",
                    name=chunk.tool_call_name or "",
                    arguments=chunk.tool_call_args or {},
                )
                tool_calls.append(tc)
            elif chunk.kind == ChunkKind.ERROR and chunk.error:
                error_parts.append(chunk.error)

        return "".join(output_parts), tool_calls, error_parts

    async def _execute_tool(name: str, arguments: dict) -> str:
        """Execute a tool and return string result."""
        if tools_registry is None:
            return "ERROR: no tools available"
        try:
            result = await tools_registry.execute(name, arguments)
            return str(result) if result is not None else "(no output)"
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"

    async def _run_agent_loop(task: WorkerTask, system_prompt: str, max_tok: int) -> tuple[str, str]:
        """Run the mini agent loop for a worker. Returns (output, error)."""
        messages = [
            Message(role="user", parts=[TextPart(
                text=f"{task.goal}\n\nContext:\n{task.context}" if task.context else task.goal
            )])
        ]

        all_output: list[str] = []

        for turn in range(MAX_TOOL_TURNS):
            text_output, tool_calls, error_parts = await _stream_response(
                messages, task.model or "", system_prompt, max_tok
            )

            # If we got errors and no output, propagate
            if not text_output and not tool_calls and error_parts:
                return "", " | ".join(error_parts)

            # Collect text output
            if text_output:
                all_output.append(text_output)

            # If no tool calls, we're done
            if not tool_calls:
                break

            # Build assistant message with tool calls
            assistant_parts: list = []
            if text_output:
                assistant_parts.append(TextPart(text=text_output))
            assistant_parts.extend(tool_calls)
            messages.append(Message(role="assistant", parts=assistant_parts))

            # Execute tools and build results
            tool_result_parts: list[ToolResultPart] = []
            for tc in tool_calls:
                # Skip delegate_task if it would exceed depth
                result_text = await _execute_tool(tc.name, tc.arguments)

                # Handle delegate results inline (they return DELEGATE_WORKERS: etc)
                # Sub-agents shouldn't trigger further delegation from here;
                # the depth check in delegate_task tool handles that.
                is_error = result_text.startswith("ERROR:")
                tool_result_parts.append(ToolResultPart(
                    tool_call_id=tc.id,
                    content=result_text,
                    is_error=is_error,
                ))

            messages.append(Message(role="tool", parts=tool_result_parts))

        return "\n".join(all_output) if all_output else "(no output)", ""

    async def _run_one(idx: int, task: WorkerTask) -> None:
        async with semaphore:
            start = time.time()
            system_prompt = system or (
                "You are a focused worker agent. Complete the task concisely and accurately. "
                "You have access to tools — use them as needed to accomplish your goal."
            )

            attempt = 0
            while True:
                try:
                    output, error = await _run_agent_loop(task, system_prompt, max_tokens)

                    if error and _is_retryable_error(error):
                        # Cooperative retry: keep going if siblings are active
                        if _any_sibling_active(idx):
                            delay = _backoff_delay(attempt)
                            log.warning(
                                "Worker %s hit retryable error (attempt %d), "
                                "siblings active, retrying in %.1fs: %s",
                                task.id, attempt + 1, delay, error[:100],
                            )
                            await asyncio.sleep(delay)
                            attempt += 1
                            continue
                        else:
                            # All siblings failed too — give up
                            await _mark_done(idx)
                            results[idx] = WorkerResult(
                                task_id=task.id,
                                goal=task.goal,
                                output="",
                                success=False,
                                elapsed=time.time() - start,
                                error=f"All agents exhausted: {error}",
                            )
                            return

                    elif error:
                        # Non-retryable error
                        await _mark_done(idx)
                        results[idx] = WorkerResult(
                            task_id=task.id,
                            goal=task.goal,
                            output="",
                            success=False,
                            elapsed=time.time() - start,
                            error=error,
                        )
                        return

                    # Success
                    await _mark_done(idx)
                    results[idx] = WorkerResult(
                        task_id=task.id,
                        goal=task.goal,
                        output=output or "(no output)",
                        success=True,
                        elapsed=time.time() - start,
                    )
                    return

                except Exception as e:
                    err_str = f"{type(e).__name__}: {e}"
                    if _is_retryable_error(err_str) and _any_sibling_active(idx):
                        delay = _backoff_delay(attempt)
                        log.warning(
                            "Worker %s exception (attempt %d), siblings active, "
                            "retrying in %.1fs: %s",
                            task.id, attempt + 1, delay, err_str[:100],
                        )
                        await asyncio.sleep(delay)
                        attempt += 1
                        continue

                    await _mark_done(idx)
                    results[idx] = WorkerResult(
                        task_id=task.id,
                        goal=task.goal,
                        output="",
                        success=False,
                        elapsed=time.time() - start,
                        error=err_str,
                    )
                    return

    # Stagger worker starts to avoid RPM limits
    worker_tasks: list[asyncio.Task] = []
    for i, task in enumerate(tasks):
        if i > 0:
            await asyncio.sleep(WORKER_STAGGER_DELAY)
        worker_tasks.append(asyncio.create_task(_run_one(i, task)))

    # Wait for all workers to complete
    await asyncio.gather(*worker_tasks)
    return [r for r in results if r is not None]  # type: ignore


# ---------------------------------------------------------------------------
# Expert Review Board (with cooperative retry)
# ---------------------------------------------------------------------------

async def run_expert_review(
    content: str,
    experts: list[str] | None = None,
    mesh: Any = None,
    model: str = "",
    max_tokens: int = 2048,
    tools_registry: Any = None,
) -> dict[str, str]:
    """Run expert review board on content (code, plan, architecture).

    Each expert reviews independently and returns feedback.
    Experts are staggered to avoid RPM limits.
    Cooperative retry on 429 / "all keys unavailable".
    """
    if mesh is None:
        return {"error": "No mesh provided — cannot run expert review"}

    if experts is None:
        experts = list(EXPERT_PROFILES.keys())

    # Filter to valid experts
    profiles = [EXPERT_PROFILES[e] for e in experts if e in EXPERT_PROFILES]
    if not profiles:
        return {"error": "No valid expert profiles specified"}

    if not model:
        return {"error": "No model specified — experts need a model to use (pass model from agent config)"}

    results: dict[str, str] = {}
    lock = asyncio.Lock()

    # Cooperative retry state
    active_experts: set[int] = set(range(len(profiles)))
    active_lock_experts = asyncio.Lock()

    async def _mark_expert_done(idx: int) -> None:
        async with active_lock_experts:
            active_experts.discard(idx)

    def _any_expert_active(idx: int) -> bool:
        return any(i != idx and i in active_experts for i in range(len(profiles)))

    async def _review(idx: int, profile: ExpertProfile) -> None:
        attempt = 0
        while True:
            try:
                messages = [
                    Message(role="user", parts=[TextPart(
                        text=f"Review the following and provide feedback from your perspective:\n\n{content}"
                    )])
                ]

                from .llm.mesh import StreamRequest
                req = StreamRequest(
                    messages=messages,
                    model=model,
                    system=profile.system_prompt,
                    tools=None,
                    max_tokens=max_tokens,
                )

                output_parts: list[str] = []
                error_parts: list[str] = []
                async for chunk in mesh.stream(req):
                    if chunk.kind == ChunkKind.TEXT and chunk.text:
                        output_parts.append(chunk.text)
                    elif chunk.kind == ChunkKind.THINKING and chunk.thinking:
                        output_parts.append(chunk.thinking)
                    elif chunk.kind == ChunkKind.ERROR and chunk.error:
                        error_parts.append(chunk.error)

                output = "".join(output_parts)
                if not output and error_parts:
                    combined_error = " | ".join(error_parts)
                    if _is_retryable_error(combined_error):
                        if _any_expert_active(idx):
                            delay = _backoff_delay(attempt)
                            log.warning(
                                "Expert %s hit retryable error (attempt %d), "
                                "siblings active, retrying in %.1fs: %s",
                                profile.role, attempt + 1, delay, combined_error[:100],
                            )
                            await asyncio.sleep(delay)
                            attempt += 1
                            continue
                        else:
                            await _mark_expert_done(idx)
                            async with lock:
                                results[profile.role] = f"ERROR: All agents exhausted: {combined_error}"
                            return

                    await _mark_expert_done(idx)
                    async with lock:
                        results[profile.role] = f"ERROR: {combined_error}"
                    return

                await _mark_expert_done(idx)
                async with lock:
                    results[profile.role] = output or "(no feedback)"
                return

            except Exception as e:
                err_str = f"{type(e).__name__}: {e}"
                if _is_retryable_error(err_str) and _any_expert_active(idx):
                    delay = _backoff_delay(attempt)
                    log.warning(
                        "Expert %s exception (attempt %d), siblings active, "
                        "retrying in %.1fs: %s",
                        profile.role, attempt + 1, delay, err_str[:100],
                    )
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue

                await _mark_expert_done(idx)
                async with lock:
                    results[profile.role] = f"ERROR: {err_str}"
                return

    # Stagger expert starts to avoid RPM limits
    expert_tasks: list[asyncio.Task] = []
    for i, profile in enumerate(profiles):
        if i > 0:
            await asyncio.sleep(WORKER_STAGGER_DELAY)
        expert_tasks.append(asyncio.create_task(_review(i, profile)))

    # Wait for all experts to complete
    await asyncio.gather(*expert_tasks)
    return results
