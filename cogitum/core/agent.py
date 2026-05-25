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
import contextlib
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

from cogitum.core.events import (
    ChunkKind,
    Message,
    StreamChunk,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolResultPart,
    Usage,
)
from cogitum.core.llm.mesh import Mesh
from cogitum.core.message_sanitization import sanitize_messages_for_provider
from cogitum.core.tools import ToolRegistry

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retry / compaction constants
# ---------------------------------------------------------------------------

import random as _random

_MAX_RETRIES = 8
_MAX_RETRIES_NO_MODAL = 10  # used when the retry-confirm modal is disabled
_CONTEXT_FILL_THRESHOLD = 0.80  # compact at 80% context usage
# How many of the most-recent messages to keep verbatim during compaction.
# Older messages are summarized; the tail is preserved untouched so the
# model can still see the live tool_call/tool_result pairs it just made.
# Tuned conservatively — at 16 messages we keep roughly the last 4-8 turns
# of agent ↔ tool exchange, which is plenty for the model to keep working
# without losing thread.
_COMPACTION_KEEP_TAIL = 16

# After this many consecutive failed attempts, escalate to a user-visible
# confirmation modal IF the modal is enabled in settings (Setup → Other).
# Below this we silently retry — short transient blips shouldn't pop a
# modal in the user's face. Three attempts is the sweet spot.
_RETRY_CONFIRM_THRESHOLD = 3
# Seconds the modal waits for the user before auto-continuing. The
# agent waits *unbounded* for the modal's decision — only the modal
# owns this timer so the two timers can't race.
_RETRY_CONFIRM_TIMEOUT = 5.0

# Sentinel used by the approval-future machinery to signal that a duplicate
# call_id arrived and the existing future was preempted. Receivers must
# detect this and treat the duplicate request as REJECTED rather than
# crashing on a non-string decision.
_DUPLICATE_ID_SENTINEL = "__cogitum_duplicate_call_id__"


def _retry_confirm_enabled() -> bool:
    """Read the user's preference fresh each call.

    Default OFF — modal is opt-in. Reads ``[other] retry_confirm_enabled``
    from settings.toml; missing or non-bool means off.
    """
    try:
        from .llm.loader import load_settings
        settings = load_settings() or {}
        other = settings.get("other") or {}
        return bool(other.get("retry_confirm_enabled", False))
    except Exception as e:
        # F40: was silent. A corrupt settings.toml silently disabled the
        # retry-confirmation modal forever; operator never knew why the
        # opt-in feature stopped responding to flips.
        log.warning(
            "settings load failed, retry_confirm disabled: %s: %s",
            type(e).__name__, e,
        )
        return False

_RETRYABLE_STATUS_RE = re.compile(r"\b(429|5\d{2})\b")
_RECOVERY_TIME_RE = re.compile(r"next recovery in\s+(\d+[.,]?\d*)\s*s?", re.IGNORECASE)
_RETRY_AFTER_RE = re.compile(r"\[Retry-After:\s*([^\]]+)\]")
# Anthropic / generic "try again in 12s" / "retry in 1m 30s" / "wait 2 minutes".
_HUMAN_WAIT_RE = re.compile(
    r"(?:try again|retry|wait|available|reset(?:s)?(?:\s+in)?)"
    r"\s+(?:in\s+)?(\d+[.,]?\d*)\s*(ms|s(?:ec(?:ond)?s?)?|m(?:in(?:ute)?s?)?|h(?:our)?s?)\b",
    re.IGNORECASE,
)
# Token-bucket "X requests per minute" hints — e.g. Cerebras 429 body.
_RPM_HINT_RE = re.compile(
    r"(\d+)\s*(?:requests|req|rpm).{0,30}?(minute|hour|second|day)",
    re.IGNORECASE,
)


# Error classes — different categories deserve different wait strategies.
class _ErrorClass:
    RATE_LIMIT = "rate_limit"      # 429, "rate limit" — wait per Retry-After (transient)
    QUOTA_EXCEEDED = "quota"       # billing / insufficient_quota — won't clear without user action
    OVERLOADED = "overloaded"      # 529 / "overloaded_error" — short wait, not user fault
    SERVER = "server"              # 500/502/503/504 — medium backoff
    NETWORK = "network"            # connection / timeout / EOF — short backoff, fast-retry
    POOL_EXHAUSTED = "pool"        # all keys cooling — respect pool's recovery hint
    UNKNOWN = "unknown"            # anything else retryable


def _jittered_backoff(attempt: int, base_delay: float = 3.0, max_delay: float = 60.0) -> float:
    """Exponential backoff with jitter (Hermes-style). Never returns 0."""
    exponent = max(0, attempt - 1)
    delay = min(base_delay * (2 ** exponent), max_delay)
    jitter = _random.uniform(0, 0.5 * delay)
    return delay + jitter


def _classify_error(exc: BaseException) -> str:
    """Categorize an exception/error string for retry strategy selection.

    Order matters — checks for the most specific signals first so a
    generic 'connection reset' inside a 429 body still classifies as
    RATE_LIMIT (the right wait curve).
    """
    msg = str(exc)
    msg_l = msg.lower()

    # Pool/keypool exhaustion — mesh-level
    if "keys unavailable" in msg_l or "no key available" in msg_l:
        return _ErrorClass.POOL_EXHAUSTED

    # Permanent billing / quota errors. Distinguished from rate limit
    # because they will NOT clear by waiting — the user has to top up
    # their account, fix billing, or switch provider. Surfacing this as
    # a separate class lets the agent show a confirmation modal instead
    # of silently retrying for 5 minutes.
    if (
        "insufficient_quota" in msg_l
        or "exceeded your current quota" in msg_l
        or "check your plan and billing" in msg_l
        or "billing_hard_limit" in msg_l
        or "billing not active" in msg_l
        or "you exceeded your current quota" in msg_l
    ):
        return _ErrorClass.QUOTA_EXCEEDED

    # Anthropic 'overloaded_error' / 529 — recovers in seconds typically.
    if "overloaded" in msg_l or "\b529\b" in msg or "529" in msg:
        return _ErrorClass.OVERLOADED

    # Rate limit (most common premium-API failure mode)
    if _RETRYABLE_STATUS_RE.search(msg) and "429" in msg:
        return _ErrorClass.RATE_LIMIT
    if "rate limit" in msg_l or "rate_limit" in msg_l \
            or "too many requests" in msg_l:
        return _ErrorClass.RATE_LIMIT

    # 5xx (excluding 529 above)
    if re.search(r"\b5\d{2}\b", msg):
        return _ErrorClass.SERVER

    # Network / transport
    network_keywords = (
        "timeout", "timed out", "connection", "connect",
        "reset by peer", "eof", "broken pipe",
        "network", "remote end closed", "unreachable",
        "no route to host", "name or service not known",
    )
    if any(kw in msg_l for kw in network_keywords):
        return _ErrorClass.NETWORK

    return _ErrorClass.UNKNOWN


def _is_retryable_error(exc: BaseException) -> bool:
    """Determine if an error is transient and worth retrying."""
    klass = _classify_error(exc)
    if klass == _ErrorClass.UNKNOWN:
        # Fall back to the legacy substring check for edge cases the
        # classifier doesn't cover yet (provider-specific wording).
        msg = str(exc).lower()
        return "recovery" in msg
    return True


def _parse_human_duration(raw: str) -> float | None:
    """Parse '12', '12s', '500ms', '1m', '1.5h' into seconds.

    Used for Retry-After values that are not a bare integer-second
    count (HTTP allows seconds OR an HTTP-date; some providers emit
    '60s' / '1m' / ISO 8601 here too — be liberal).
    """
    raw = raw.strip().rstrip(".,;")
    if not raw:
        return None
    # Bare number → assume seconds.
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        pass
    m = re.match(r"^\s*(\d+[.,]?\d*)\s*(ms|s|sec(?:ond)?s?|m|min(?:ute)?s?|h|hour?s?)\s*$",
                 raw, re.IGNORECASE)
    if not m:
        return None
    val = float(m.group(1).replace(",", "."))
    unit = m.group(2).lower()
    if unit == "ms":
        return val / 1000.0
    if unit.startswith("h"):
        return val * 3600.0
    if unit.startswith("m") and unit != "ms":
        # 'm' / 'min' / 'minute(s)'
        return val * 60.0
    # default seconds
    return val


def _parse_recovery_delay(exc: BaseException) -> float | None:
    """Extract recovery time from error messages.

    Sources (checked in order):
      1. ``[Retry-After: …]`` — from HTTP header (most authoritative).
         Now accepts '60', '60s', '500ms', '1m', '2 minutes'.
      2. Inline 'try again in 12s' / 'retry in 1m 30s' (Anthropic,
         OpenRouter, xAI all phrase rate limits this way).
      3. ``next recovery in Xs`` — from KeyPool ``NoKeyAvailable``.

    Caps at 60s (long waits stall the agent — pool recovers earlier);
    rejects values < 2s (force backoff floor instead of busy-looping).
    """
    msg = str(exc)

    # 1. Retry-After header (parsed liberally).
    match = _RETRY_AFTER_RE.search(msg)
    if match:
        val = _parse_human_duration(match.group(1))
        if val is not None and val >= 2.0:
            return min(val, 60.0)

    # 2. Inline human phrasing.
    match = _HUMAN_WAIT_RE.search(msg)
    if match:
        unit = (match.group(2) or "s").lower()
        try:
            num = float(match.group(1).replace(",", "."))
        except ValueError:
            num = None
        if num is not None:
            if unit.startswith("ms"):
                seconds = num / 1000.0
            elif unit.startswith("h"):
                seconds = num * 3600.0
            elif unit.startswith("m") and unit != "ms":
                seconds = num * 60.0
            else:
                seconds = num
            if seconds >= 2.0:
                return min(seconds, 60.0)

    # 3. KeyPool recovery hint.
    match = _RECOVERY_TIME_RE.search(msg)
    if match:
        try:
            val = float(match.group(1).replace(",", "."))
        except ValueError:
            return None
        if val < 2.0:
            return None
        return min(val, 60.0)

    return None


def _compute_retry_delay(
    exc: BaseException | None,
    attempt: int,
) -> tuple[float, str]:
    """Pick the wait duration based on error class and attempt number.

    Returns (delay_seconds, reason_tag) so the caller can log why it
    chose this delay (handy when debugging stuck retries).

    Strategy by class:
      • Authoritative hint (Retry-After / 'try again in X') always wins.
      • POOL_EXHAUSTED — same: respect pool's recovery hint.
      • QUOTA_EXCEEDED — minimal poll (5s) so the user sees the
        confirmation modal almost immediately. Waiting won't fix
        billing; we mostly retry to give the modal a chance to show.
      • RATE_LIMIT (no hint) — exponential, capped at 30s (rate limits
        usually clear within seconds on premium APIs; long waits hurt UX).
      • OVERLOADED — short, capped at 15s. Anthropic 529 recovers fast.
      • SERVER — medium backoff (cap 60s).
      • NETWORK — fast-retry: cap 10s. Transport blips fix themselves.
      • UNKNOWN — default exponential cap 60s.
    """
    klass = _classify_error(exc) if exc is not None else _ErrorClass.UNKNOWN

    # QUOTA_EXCEEDED bypasses Retry-After: the API always echoes back a
    # short "try again" hint on quota-exhausted responses, but waiting
    # 30 minutes for billing to refresh would block the agent silently.
    # Better to show the modal fast and let the user decide.
    if klass == _ErrorClass.QUOTA_EXCEEDED:
        return _jittered_backoff(attempt + 1, base_delay=2.0, max_delay=5.0), "quota"

    if exc is not None:
        hint = _parse_recovery_delay(exc)
        if hint is not None:
            return hint, "hint"

    if klass == _ErrorClass.NETWORK:
        return _jittered_backoff(attempt + 1, base_delay=1.5, max_delay=10.0), "network"
    if klass == _ErrorClass.OVERLOADED:
        return _jittered_backoff(attempt + 1, base_delay=2.0, max_delay=15.0), "overloaded"
    if klass == _ErrorClass.RATE_LIMIT:
        return _jittered_backoff(attempt + 1, base_delay=3.0, max_delay=30.0), "rate_limit"
    if klass == _ErrorClass.SERVER:
        return _jittered_backoff(attempt + 1, base_delay=3.0, max_delay=60.0), "server"
    if klass == _ErrorClass.POOL_EXHAUSTED:
        return _jittered_backoff(attempt + 1, base_delay=2.0, max_delay=20.0), "pool"
    return _jittered_backoff(attempt + 1, base_delay=3.0, max_delay=60.0), "unknown"


# ---------------------------------------------------------------------------
# Delegate result classification (LEGION_RUN / DELEGATE_WORKERS /
# DELEGATE_EXPERTS share the same set of "this came back as failure"
# prefixes). Centralised so a new prefix is added in one place — F40
# audit found three drifting copies of this list.
# ---------------------------------------------------------------------------

_DELEGATE_ERROR_PREFIXES = (
    "ERROR:",
    "REJECTED:",
    "Internal error",
    # _run_delegate_workers formats the header as
    # "Workers completed (N/M success):" — N==0 means every worker
    # failed. M==0 (zero tasks dispatched) is also surfaced as failure
    # since the parent asked for work and got none done.
    "Workers completed (0/",
    # Symmetric guard for an experts-side "0 of N completed" header —
    # the current _run_delegate_experts doesn't emit this format, but
    # we want the prefix detected if/when it does.
    "Experts completed (0/",
    # A bare traceback string slipped through one of the delegate
    # branches means an unhandled exception leaked into the result —
    # always an error.
    "Traceback (most recent call last):",
)

_DELEGATE_ERROR_SUBSTRINGS = (
    "raised an exception",
)


# _format_tool_result_for_model is re-exported from tool_result_format so
# subworker paths (legion_worker, delegate) can import the same normaliser
# without a circular dep on agent.py. The leading-underscore alias preserves
# existing call sites inside this module.
from cogitum.core.tool_result_format import (  # noqa: E402
    format_tool_result_for_model as _format_tool_result_for_model,
)


def _result_indicates_error(text: str) -> bool:
    """Return True if a delegate / legion subagent result string looks like
    an error.

    Used by all three sentinel branches (LEGION_RUN, DELEGATE_WORKERS,
    DELEGATE_EXPERTS) so the parent model sees a uniform error signal
    no matter which dispatch path the failure came back through.

    Detection rules:
      - any of the prefixes in ``_DELEGATE_ERROR_PREFIXES`` at start
      - any of the substrings in ``_DELEGATE_ERROR_SUBSTRINGS`` anywhere
        (covers worker output that says "task X raised an exception:" mid
        line — the swarm formatter doesn't always lead with ERROR:).
    """
    if not isinstance(text, str) or not text:
        return False
    for p in _DELEGATE_ERROR_PREFIXES:
        if text.startswith(p):
            return True
    for s in _DELEGATE_ERROR_SUBSTRINGS:
        if s in text:
            return True
    return False


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
class AgentRetryConfirm:
    """Stalled-retry notification for the UI.

    Fire-and-forget: the agent emits this to let the UI pop a modal,
    then keeps retrying on its own backoff schedule. The UI doesn't
    need to signal back — Abort cancels the agent task directly via
    ``app.action_cancel_agent()`` (same path as Esc), Continue just
    closes the modal and the agent's already-running sleep finishes
    on its own.

    No futures, no events, no signaling. The simplest thing that
    works: cancellation propagates through asyncio's normal channels.
    """
    attempt: int
    max_attempts: int
    error_class: str
    error_message: str
    auto_continue_in: float
    turn: int = 0


@dataclass
class AgentToolCall:
    tool_name: str
    arguments: dict[str, Any]
    call_id: str
    turn: int = 0
    preliminary: bool = False
    danger_level: str = "low"  # "low", "medium", "danger"


@dataclass
class AgentApprovalRequest:
    """Emitted when a tool call needs user approval (medium/danger level)."""
    tool_name: str
    arguments: dict[str, Any]
    call_id: str
    danger_level: str  # "medium" or "danger"
    turn: int = 0


@dataclass
class AgentToolResult:
    tool_name: str
    call_id: str
    result: str
    error: bool = False
    turn: int = 0


@dataclass
class AgentInjected:
    """A queued user message was consumed by the agent mid-turn."""
    text: str
    turn: int = 0


@dataclass
class AgentDone:
    turns: int
    usage: Usage | None = None


@dataclass
class AgentCompacted:
    """Context was compacted (auto at threshold OR manual via /compact).

    Carries before/after token estimates so the inspector can reset
    its running ``tokens_used`` counter to the new authoritative
    value — without this, the cumulative bar keeps growing forever
    and the user can't see that compaction actually freed space.

    ``status`` lets the consumer distinguish three outcomes for the
    manual /compact path so the user gets a meaningful message
    instead of "X → X tokens, N → N messages":
      * ``ok`` — work was done, before/after differ
      * ``not_needed`` — buffer fit in the keep-tail window so
        compaction was a no-op by design
      * ``no_change`` — compaction ran but didn't shrink anything
        (summarizer returned same content, stream errored, etc.)
    """
    before_tokens: int
    after_tokens: int
    messages_before: int
    messages_after: int
    manual: bool = False
    status: str = "ok"  # 'ok' | 'not_needed' | 'no_change'


@dataclass
class AgentTurnPersist:
    """Mid-run persistence checkpoint.

    Emitted by ``Agent.run()`` after every turn that mutated
    ``messages`` (after committing the assistant response, after
    appending tool results, after a fallback summary). The TUI / TG
    gateway should react by writing ``messages`` to disk so a
    process crash mid-loop can only lose the in-flight turn, never
    accumulated history.

    The ``messages`` field is a SHALLOW SNAPSHOT taken at emission
    time — a fresh ``list(...)`` copy so subsequent agent turns
    can't retroactively change what the consumer sees as "the
    state at this checkpoint". Message objects inside the list are
    still shared by reference (deep-copy would be too expensive
    per persist), so consumers must serialize/snapshot before any
    long await rather than holding the list across turn boundaries.

    Why an event vs an inline ``Store.replace_messages`` call:
    persistence is a UI/gateway concern (which store, which session
    id, what backend). The agent stays storage-agnostic and the
    consumer decides what to do.
    """
    messages: list  # list[Message] — shallow snapshot at emit, see docstring
    iteration: int

    @property
    def messages_count(self) -> int:
        """Count at emission time (frozen — the list is a snapshot)."""
        return len(self.messages)


@dataclass
class AgentError:
    message: str
    exc: BaseException | None = None


AgentEvent = AgentText | AgentThinking | AgentRetry | AgentRetryConfirm | AgentToolCall | AgentApprovalRequest | AgentToolResult | AgentInjected | AgentDone | AgentCompacted | AgentTurnPersist | AgentError

# ---------------------------------------------------------------------------
# Agent config
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    model: str | None = None          # override mesh default
    # Hard cap on tool-call iterations. 0 = unlimited (no cap at all).
    # Default raised from 20 → 0 because the cap silently truncated long
    # agent runs: the loop committed the final tool_result message, broke
    # out, and the model never got a chance to see/respond to its own
    # last tool batch. Symptom: "output от тулов перестаёт идти модели".
    # The compaction loop keeps memory bounded, so an unbounded turn
    # count is safe.
    max_turns: int = 0
    max_tokens: int = 32768
    temperature: float | None = None
    system: str = (
        "You are Cogitum, a sovereign agentic assistant. You run inside a terminal "
        "TUI or Telegram gateway. You have full tool access, persistent memory, "
        "skills, and session history across sessions. You are direct, concise, "
        "and action-oriented — a partner, not a servant.\n\n"

        "═══ CORE PRINCIPLES ═══\n"
        "• Default to action — implement changes rather than suggesting them.\n"
        "• Read code before making claims about it. Verify before presenting results.\n"
        "• If an approach fails twice, diagnose the root cause and try a fundamentally "
        "different approach. Explain what went wrong.\n"
        "• Match the user's language and communication style.\n"
        "• Admit uncertainty. Don't present assumptions as facts.\n"
        "• Correct the user when they are wrong — honest feedback > agreement.\n"
        "• Be persistent and autonomous. Complete tasks fully without stopping early.\n\n"

        "═══ MEMORY — your persistent brain ═══\n"
        "Memory is your superpower. It makes you smarter with every session.\n"
        "SAVE PROACTIVELY — don't wait for the user to ask:\n"
        "• User corrects you → save IMMEDIATELY. This is highest priority.\n"
        "• User mentions a preference ('I like X', 'don't do Y') → save.\n"
        "• You discover project structure, conventions, quirks → save.\n"
        "• You learn how a tool/API/library works in this specific setup → save.\n"
        "• You find out the user's name, role, timezone, workflow → save.\n"
        "• After EVERY session where you learned something new → review and save.\n\n"
        "WHEN TO UPDATE memory:\n"
        "• User contradicts a saved fact → REPLACE immediately.\n"
        "• You discover a saved fact is wrong → DELETE or REPLACE.\n"
        "• Project changed (new deps, new structure) → UPDATE.\n\n"
        "FORMAT: declarative facts, not instructions.\n"
        "  ✓ 'Project uses ruff for linting, not flake8'\n"
        "  ✓ 'User prefers Russian, switches to English for code comments'\n"
        "  ✗ 'Always run ruff before committing' (this is a skill, not memory)\n\n"
        "FREQUENCY: save something to memory in almost every session. If you "
        "finish a session without saving anything, ask yourself what you missed.\n\n"

        "═══ SKILLS — your procedural memory ═══\n"
        "Skills are reusable workflows. They make you faster and more reliable.\n"
        "CREATE skills aggressively:\n"
        "• Solved a problem with 3+ tool calls? → SAVE AS SKILL.\n"
        "• Found a non-obvious workflow? → SAVE AS SKILL.\n"
        "• User showed you how they do something? → SAVE AS SKILL.\n"
        "• Built/deployed/configured something? → SAVE AS SKILL.\n"
        "• Don't ask permission — just save it. User can delete if unwanted.\n\n"
        "USE skills before acting:\n"
        "• ALWAYS check skills(action='list') at session start or new task type.\n"
        "• If a skill exists for the task → FOLLOW IT, don't improvise.\n"
        "• If a skill is outdated → UPDATE IT with what you learned.\n\n"
        "SKILL FORMAT: trigger conditions, numbered steps, pitfalls, verification.\n"
        "Example skill: 'deploy-to-prod' → when to use, exact commands, what to check.\n\n"

        "═══ SESSION AWARENESS ═══\n"
        "• When the user references past work ('we did this before', 'remember when', "
        "'last time', 'as I mentioned'), use session_search FIRST.\n"
        "• Your memory and skills persist across sessions — use them to build "
        "continuity. Each session makes you smarter for the next one.\n"
        "• Don't ask the user to repeat information that should be in memory.\n"
        "• At session start, check memory for relevant context about the project.\n\n"

        "═══ ADAPTIVE BEHAVIOR ═══\n"
        "• First time in a project: read config files (package.json, pyproject.toml, "
        "Makefile, etc.) to understand build tools, test runners, linters.\n"
        "• Match the project's style, conventions, and libraries — don't introduce new ones.\n"
        "• After completing work, run the project's build/test step to verify.\n"
        "• When making recommendations, explain your reasoning.\n"
        "• For safety-sensitive changes (auth, infra, data), state what was verified "
        "and what could not be verified.\n"
        "• For destructive operations (rm -rf, git reset --hard, DROP TABLE), "
        "confirm with the user before executing.\n\n"

        "═══ TOOLS ═══\n"
        "All tools have automatic danger classification (low/medium/danger). "
        "Medium and danger commands require user approval — you don't need to "
        "specify danger level, it's detected automatically from the command.\n\n"

        "TERMINAL — 3 modes:\n"
        "• terminal(command='...') — normal mode, waits for completion, no timeout.\n"
        "  Use for fast commands that complete quickly (ls, git status, pytest -x).\n"
        "• terminal(command='...', mode='timeout', timeout=30) — kills if exceeds limit.\n"
        "  Returns last 8KB stdout + hint on timeout. Use for commands that MIGHT hang\n"
        "  (network calls, slow builds, downloads). Default timeout 120s.\n"
        "• terminal(command='...', mode='background') — starts in background, returns PID.\n"
        "  Management (always pass mode='background' for these):\n"
        "    command='list'                       → all background PIDs + status\n"
        "    command='read',  pid=N               → tail stdout/stderr of process\n"
        "    command='write', pid=N, stdin='...'  → send input to process stdin\n"
        "    command='close', pid=N               → close stdin (send EOF). Use when\n"
        "                                           process waits for end-of-input.\n"
        "    command='kill',  pid=N               → SIGTERM the process\n"
        "  Use background for: servers, long builds, watchers, downloads >2min,\n"
        "  anything that runs indefinitely or in parallel with other work.\n"
        "  Background processes auto-cleanup 300s after exit.\n\n"

        "FILE OPERATIONS:\n"
        "• read_file(path='...', offset=0, limit=100) — read file with line numbers.\n"
        "• write_file(path='...', content='...') — create/overwrite file.\n"
        "• edit_file(path='...', old_string='...', new_string='...') — find-and-replace.\n"
        "  old_string MUST match exactly once in the file. Include 3-5 lines of context\n"
        "  around the change to make it unique.\n"
        "• search_files(pattern='...', path='.', file_glob='*.py') — regex search (ripgrep).\n"
        "• list_dir(path='.') — list directory contents.\n\n"

        "COGIT — smart checkpoints (your version control):\n"
        "Cogit is YOUR safety net. Use it PROACTIVELY — don't wait for the user to ask.\n\n"
        "WHEN TO SAVE (do it yourself, automatically):\n"
        "• BEFORE any risky edit (refactor, delete, rewrite) → cogit save.\n"
        "• BEFORE writing to multiple files → cogit save.\n"
        "• After completing a working feature → cogit save (lock in progress).\n"
        "• Before trying an experimental approach → cogit save (easy rollback).\n"
        "• Every 3-5 successful tool calls in a complex task → cogit save.\n"
        "• If you're about to do something you're not 100% sure about → cogit save.\n\n"
        "HOW TO USE:\n"
        "• cogit(action='save', label='before refactor auth') — save checkpoint.\n"
        "• cogit(action='save', label='auth module', scope='src/auth/') — save ONLY specific dir.\n"
        "  USE SCOPE! Don't checkpoint the whole project when you're editing one folder.\n"
        "  Examples: scope='cogitum/core/', scope='main.py', scope='*.py'\n"
        "• cogit(action='list') — see all checkpoints with file counts and scope.\n"
        "• cogit(action='diff', index=N) — show what changed since checkpoint N.\n"
        "• cogit(action='restore', index=N) — restore files from checkpoint N.\n"
        "• cogit(action='cleanup') — remove old checkpoints (keeps last 10).\n\n"
        "LABELS should be descriptive: 'before auth refactor', 'working login flow', "
        "'pre-migration', 'stable API v2'. Not 'checkpoint 1'.\n\n"
        "PATTERN: save → make changes → test → if broken: restore. If working: save again.\n"
        "Think of it as quicksave in a game — do it often, especially before boss fights.\n\n"

        "DELEGATE — for complex multi-part tasks:\n"
        "• delegate_task spawns parallel sub-agents with full tool access.\n"
        "• Use workers mode for independent subtasks, experts mode for review.\n"
        "• Subagents have NO memory of your conversation — pass all context explicitly.\n\n"

        "SESSION SEARCH — cross-session memory:\n"
        "• session_search(action='list') — browse recent sessions.\n"
        "• session_search(action='search', query='...') — find sessions by title/content.\n"
        "• session_search(action='read', session_id='...', limit=20) — read messages.\n"
        "• Use when user references past work or you need context from before.\n\n"

        "WEB — search and browse:\n"
        "• web_search(query='...') — DuckDuckGo, no API key needed. Real results, not stub.\n"
        "• browser(action='open', url='...') — Playwright headless Chromium.\n"
        "  Navigation: open, back, forward, reload, close.\n"
        "  Inspection: text (full page text), title, url, links (all <a href>),\n"
        "              extract (CSS selector → text), screenshot (PNG path).\n"
        "  Interaction: click (CSS selector), type (selector + text), scroll (px),\n"
        "               act (arbitrary JS in page context — returns evaluated result).\n"
        "  Use 'extract' with selector for structured scraping. Use 'act' for\n"
        "  anything the standard actions don't cover (form submit, complex DOM ops).\n"
        "• fetch_url(url='...') — quick fetch + HTML strip for simple static pages.\n"
        "  Faster than browser, no JS execution. Use for plain HTML/markdown/JSON.\n\n"

        "MEDIA — send files to user (Telegram gateway only):\n"
        "• send_media(path='/path/to/file.png') — send photo or document.\n"
        "• Auto-detects type from extension (.png/.jpg/.webp → photo, else → document).\n\n"

        "MEMORY — persistent across sessions:\n"
        "• memory(action='add', content='fact', target='memory') — save new fact.\n"
        "• memory(action='add', content='preference', target='user') — save user profile fact.\n"
        "• memory(action='replace', content='new', old_text='unique substring of old entry').\n"
        "• memory(action='remove', old_text='unique substring').\n"
        "• target='memory' for env/project facts, target='user' for user preferences.\n\n"

        "SKILLS — reusable workflows:\n"
        "• skills(action='list') — list all available skills.\n"
        "• skills(action='read', name='...') — load skill content.\n"
        "• skills(action='write', name='my-skill', content='# SKILL.md\\n...') — create/update.\n"
        "• skills(action='delete', name='...') — remove a skill.\n"
        "• Use category='devops' etc. when creating to organize.\n\n"

        "═══ WORKFLOW ═══\n"
        "1. Understand the request. Ask clarifying questions only if truly ambiguous.\n"
        "2. Check memory for relevant context. Check skills for relevant workflows.\n"
        "3. If editing code: cogit save FIRST (scope to the dir you'll touch).\n"
        "4. Plan if complex (3+ steps). Act immediately if simple.\n"
        "5. Execute with tools. Verify results (run tests, check output).\n"
        "6. If it worked: cogit save (lock in progress). If broken: cogit restore.\n"
        "7. Save learnings: memory (facts), skills (procedures).\n"
        "8. Report concisely. Don't over-explain obvious results.\n\n"

        "═══ SELF-IMPROVEMENT TRIGGERS ═══\n"
        "After EVERY interaction, ask yourself:\n"
        "• Did I learn something about the user? → memory save.\n"
        "• Did I learn something about the project? → memory save.\n"
        "• Did I solve a non-trivial problem? → skill save.\n"
        "• Did I use a skill that was wrong? → skill update.\n"
        "• Did I make a change I might need to undo? → cogit save.\n"
        "• Am I about to do something risky? → cogit save.\n"
        "These are NOT optional. They are part of being a good agent.\n\n"

        "═══ RESPONSE STYLE ═══\n"
        "• Keep responses focused and proportional to the task.\n"
        "• Simple questions get short answers. Complex tasks get thorough responses.\n"
        "• Use plain text for prose, code blocks for code. No unnecessary markdown.\n"
        "• Skip filler acknowledgments ('You're absolutely right'). Be direct.\n"
        "• When reporting tool results, summarize — don't dump raw output.\n"
    )
    tools_enabled: bool = True
    tool_tags: list[str] | None = None   # None = all tools
    platform: str = "cli"  # "cli" or "telegram" — injected into context
    # YOLO mode — when True, all medium/danger tool approvals are
    # auto-granted without prompting the user. The agent runs fully
    # autonomous: terminal commands, file writes, network calls all
    # execute immediately. Toggled per-session via /yolo (TUI + TG).
    # Default OFF because the safety net catches mistakes; YOLO is an
    # explicit "I trust this run, don't interrupt me" switch.
    yolo_mode: bool = False
    # F38: optional monotonic-clock deadline. When set, the approval
    # gate automatically flips ``yolo_mode`` back to False the first
    # time ``time.monotonic() > yolo_until``. Lets operators opt into
    # a time-boxed YOLO ("just for the next 30 min") without
    # remembering to /yolo off afterwards. None = permanent until
    # manual toggle. We use monotonic, not wall-clock, so an NTP
    # step-back / DST shift / manual clock edit can never extend a
    # privileged window. Persistence is in-memory only — restart
    # resets the TTL, which is the right security default anyway.
    yolo_until: float | None = None


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
        # Monotonically-increasing fallback counter for tool calls that
        # arrive without a provider-supplied tool_call_id. The previous
        # `f"call_{len(pending)}"` scheme could collide: pending shrinks
        # when an entry is popped on TOOL_CALL_DONE, so the *next*
        # idless tool call would reuse the same synthetic id and clobber
        # the live entry of a still-streaming tool call. The counter is
        # per-Agent-instance and never decreases.
        self._tool_call_seq: int = 0
        # Per-call_id futures for approval routing. The approval queue
        # was FIFO-only, which silently swapped decisions when the
        # model emitted multiple medium/danger tools in one batch and
        # the user responded out of order (B-then-A in the UI but the
        # agent paired the decisions in arrival order, not by call_id).
        # Each pending approval now has its own Future keyed by
        # ToolCallPart.id; consumers route by call_id. The
        # `_approval_queue` attribute is still set as a non-None marker
        # so the gating logic in _execute_tool stays a one-line check;
        # actual decision routing is done via the futures map.
        self._approval_futures: dict[str, asyncio.Future] = {}
        # F3 fix: tool_call ids whose JSON arguments failed to parse on
        # the wire. ``_execute_tool`` short-circuits with the stored
        # error string and skips ``registry.execute`` so a malformed
        # arguments blob never reaches a real side-effect. Cleared per
        # turn (the ``run()`` loop owns the lifecycle).
        self._malformed_tool_call_ids: dict[str, str] = {}
        # Reference to the event loop the agent is running in. Captured
        # lazily on the first ``run()`` call (loop only exists then) so
        # cross-thread ``submit_approval`` callers (Telegram, Discord
        # gateway threads) can safely route ``set_result`` via
        # ``loop.call_soon_threadsafe`` instead of silently no-op-ing
        # against a Future bound to a different loop.
        self._main_loop: asyncio.AbstractEventLoop | None = None

        # Serialises the two paths that mutate ``messages`` from outside
        # the same loop iteration: the in-run ``_compact_context`` call
        # and the manual ``compact_now`` triggered by /compact while
        # ``run()`` is still streaming. Without the lock, both paths
        # could compute compaction against different snapshots of the
        # buffer and the next ``AgentTurnPersist`` would race the
        # manual replace_messages on disk.
        self._compact_lock = asyncio.Lock()

        # Wire the Legion worker once per Agent instance. The worker
        # callable closes over this agent's mesh + registry so every
        # cogitator in a swarm uses the same provider pool and tool
        # set as the lead agent. Idempotent — re-registration on a
        # second Agent simply overwrites the previous worker.
        #
        # Model is passed as a *callback* so cogitators always pick
        # up whatever model the lead is currently on — including
        # /model switches mid-session. (Capturing cfg.model as a
        # static string at __init__ would lock the swarm to the
        # startup model.)
        from .legion import get_legion
        from .legion_worker import make_legion_worker
        get_legion().register_worker(
            make_legion_worker(
                mesh=self.mesh,
                registry=self.registry,
                model=lambda: self.cfg.model or "",
            )
        )

    async def aclose(self) -> None:
        """Tear down per-Agent state safely.

        Cancels every pending approval future so any tool-call coroutine
        still suspended in ``await fut`` wakes up with CancelledError
        instead of dangling forever — that path was leaking tasks on
        TUI exit / TG ``bot.stop()`` because ``_approval_futures`` was
        only ever pruned when a decision arrived. Idempotent: safe to
        call twice during shutdown sequences.
        """
        # Snapshot first — cancelling a future can synchronously trigger
        # callbacks that mutate the dict (the awaiter pops its own entry).
        for call_id, fut in list(self._approval_futures.items()):
            if fut is None or fut.done():
                continue
            try:
                fut.cancel()
            except Exception:
                log.debug(
                    "aclose: cancel failed for %s", call_id, exc_info=True,
                )
        self._approval_futures.clear()

    def submit_approval(self, call_id: str, decision: str) -> bool:
        """Route a user's approval decision to the waiting tool call.

        Replaces the old FIFO `_approval_queue.put(...)` contract.
        Decisions are paired with their request by `call_id`, not by
        arrival order — so when the model emits two medium/danger
        tools in parallel and the user clicks B-approve, A-reject in
        any order, each decision still lands on the right call.

        Returns True if a pending approval was found and routed; False
        if the call_id is unknown (stale, already-resolved, or never
        existed) or the decision string is malformed. Stale callbacks
        from previous turns are ignored rather than corrupting future
        approvals.

        ``decision`` MUST be one of:
          - ``"approve"``        — execute as-is
          - ``"reject"``         — return REJECTED to the model
          - ``"modify:<json>"``  — execute with the JSON-decoded args

        Anything else returns False and logs a warning. This guard
        protects against typos ("approbe"), kept-going UI contracts
        ("yes"), or third-party gateways shoving raw user text in.

        Thread-safety: safe to call from any thread. If invoked from a
        thread other than the agent's main event loop (e.g. a Telegram
        callback handler running in its own thread), the actual
        ``Future.set_result`` is dispatched through
        ``loop.call_soon_threadsafe`` so it lands on the right loop.
        Cross-thread calls return True optimistically (the dispatch was
        scheduled); the result is delivered asynchronously.
        """
        # Type guards: callback_data from Telegram (or any other gateway)
        # can deliver None, dict, bytes, or other unexpected types if a
        # caller skips proper deserialization. Reject anything that isn't
        # a real string before touching string-only operations like
        # `.startswith` so we fail closed instead of crashing the loop.
        if not isinstance(call_id, str) or not isinstance(decision, str):
            log.warning(
                "submit_approval: non-string args call_id=%r decision=%r; ignoring",
                call_id, decision,
            )
            return False
        if not (
            decision == "approve"
            or decision == "reject"
            or decision.startswith("modify:")
        ):
            log.warning(
                "submit_approval: invalid decision=%r for call_id=%s; ignoring",
                decision, call_id,
            )
            return False
        fut = self._approval_futures.get(call_id)
        if fut is None or fut.done():
            return False

        # Cross-thread routing. ``set_result`` on a Future bound to
        # another loop is a silent no-op (the awaiter never wakes), so
        # we MUST hop loops via ``call_soon_threadsafe`` when we detect
        # the call originated from a non-main thread.
        loop = self._main_loop
        if loop is not None and loop.is_running():
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is not loop:
                # Different loop (or no loop on this thread at all):
                # marshal the resolution back to the agent's loop.
                def _resolve() -> None:
                    f = self._approval_futures.get(call_id)
                    if f is None or f.done():
                        return
                    try:
                        f.set_result(decision)
                    except asyncio.InvalidStateError:
                        # Concurrent resolver beat us — benign.
                        pass
                loop.call_soon_threadsafe(_resolve)
                return True

        try:
            fut.set_result(decision)
        except asyncio.InvalidStateError:
            # Single-loop callers can't race here, but a TUI/TG dispatch
            # arriving via ``loop.call_soon_threadsafe`` from another
            # thread can land a second set_result between our ``done()``
            # check and the call below. Treat it as a no-op rather than
            # crashing the gateway/TUI handler.
            return False
        return True

    async def run(
        self,
        user_message: str,
        history: list[Message] | None = None,
        queue: asyncio.Queue[AgentEvent] | None = None,
        inject_queue: asyncio.Queue[str] | None = None,
        approval_queue: asyncio.Queue[str] | None = None,
        _suppress_fallback_summary: bool = False,
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
        approval_queue : asyncio.Queue[str] | None
            Marker for "approval gating is active". When non-None,
            medium/danger tool calls emit AgentApprovalRequest and
            block until the user responds. When None, all tools
            execute without approval.

            Decisions are NOT routed through this queue — they go via
            ``Agent.submit_approval(call_id, decision)`` which pairs
            each user reply with the right pending tool by ``call_id``.
            The queue parameter is kept as a non-None gating marker
            for backward compatibility; nothing is ever read from it.

        Returns
        -------
        list[Message]
            Updated history including the new messages from this run.
        """
        q = queue or asyncio.Queue()
        self._approval_queue = approval_queue
        # Capture the running loop for cross-thread ``submit_approval``
        # routing. Re-captured every run() in case the agent is reused
        # across loops (rare, but harmless to refresh).
        try:
            self._main_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._main_loop = None
        # R3 fix (audit gap #10): clear stale parse-err entries from a
        # previous run that may have been cancelled mid-stream BEFORE
        # _execute_tool got a chance to pop the id. Otherwise a provider
        # that uses sequential ids (vLLM-style call_0, call_1, ...) on a
        # fresh run could collide with a stale id and have its first
        # legitimate tool call short-circuited with a misleading parse
        # error. This dict is per-run state, never per-Agent.
        self._malformed_tool_call_ids.clear()
        messages: list[Message] = list(history or [])

        total_usage: Usage | None = None
        accumulated_tokens: int = 0
        iteration = 0
        # Why this turn ended — set by every break/return path so the
        # diagnostic log at the bottom records an accurate reason.
        # Default sentinel "unknown" indicates a code path forgot to
        # update it (would surface as a noisy log line for us to find).
        turn_exit_reason: str = "unknown"
        # Approximate length of the final assistant text response,
        # used to surface "model emitted nothing" failures in the log.
        # Stays 0 on max_turns_reached / cancelled / exception paths
        # because no final response was produced — that's correct.
        final_response_len: int = 0

        try:
            # Append user message INSIDE the try so a malformed
            # user_message (or registry.to_openai failure) still
            # produces a Turn-ended diagnostic via the finally below.
            messages.append(
                Message(role="user", parts=[TextPart(text=user_message)])
            )

            tools_schema = (
                self.registry.to_openai(self.cfg.tool_tags)
                if self.cfg.tools_enabled
                else []
            )
            # max_turns == 0 → unlimited iterations. Compaction at
            # 60% context still bounds memory; this just stops the
            # loop from cutting the model off mid-thought after a
            # fixed number of tool batches.
            while self.cfg.max_turns == 0 or iteration < self.cfg.max_turns:
                iteration += 1

                # ── context compaction check ───────────────────────────────
                # Two signals: (a) authoritative USAGE input_tokens
                # from the previous turn, (b) cheap pre-flight estimate
                # built from the message buffer when USAGE has not
                # arrived yet (first turn, providers that defer USAGE).
                # The estimate avoids the historical failure mode where
                # the agent only knew it was over-budget AFTER the
                # provider already rejected the request with
                # context_length_exceeded.
                context_window = self._get_context_window()
                estimated_tokens = self._estimate_prompt_tokens(messages)
                effective_tokens = max(accumulated_tokens, estimated_tokens)
                if (context_window > 0
                        and effective_tokens >= int(context_window * _CONTEXT_FILL_THRESHOLD)):
                    msgs_before = len(messages)
                    # Hold the compaction lock so a manual /compact
                    # racing in via compact_now can't snapshot the
                    # buffer mid-rewrite. compact_now bails early
                    # ('busy') when this is held.
                    async with self._compact_lock:
                        messages = await self._compact_context(messages, q)
                    accumulated_tokens = 0  # reset after compaction
                    after_tokens = self._estimate_prompt_tokens(messages)
                    await q.put(AgentCompacted(
                        before_tokens=effective_tokens,
                        after_tokens=after_tokens,
                        messages_before=msgs_before,
                        messages_after=len(messages),
                        manual=False,
                    ))
                    await q.put(AgentText(
                        delta=(
                            f"\n⟳ context compacted "
                            f"(~{effective_tokens} → ~{after_tokens} tokens, "
                            f"{msgs_before} → {len(messages)} messages)\n"
                        ),
                        turn=iteration,
                    ))
                    # Persist post-compaction state immediately so a
                    # crash before the next assistant commit doesn't
                    # leave the OLD bloated history on disk. Wrapped
                    # in try/except so a queue rejection here can't
                    # poison the run — the next persist will catch
                    # up if this one fails.
                    try:
                        await q.put(AgentTurnPersist(
                            messages=list(messages),
                            iteration=iteration,
                        ))
                    except Exception:
                        log.exception(
                            "AgentTurnPersist after compaction "
                            "failed (suppressed)"
                        )

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
                        # P0-3 fix (audit_tools_history.md): Anthropic
                        # streams thinking content and its cryptographic
                        # signature as separate chunks — text deltas
                        # arrive with signature=None, and the signature
                        # itself comes in a final ``signature_delta``
                        # block with empty text. Naively overwriting
                        # ``signature`` on every chunk meant any
                        # text-delta arriving after the signature_delta
                        # (or a None-signature chunk landing later in
                        # the merge) wiped the signature. Without it,
                        # ``normalize_messages_anthropic`` drops the
                        # whole thinking block and Anthropic refuses
                        # to validate the model's prior reasoning on
                        # follow-up turns. We now preserve the
                        # signature once we've seen one, only updating
                        # when a new non-empty signature arrives.
                        new_sig = chunk.thinking_signature or None
                        # GAP-9 fix (audit_r2_history.md): tag the
                        # thinking with the model that produced it so
                        # ``normalize_messages_anthropic`` can drop
                        # stale signatures after a /model switch.
                        # Anthropic binds signatures cryptographically
                        # to a specific model — replaying a signature
                        # from claude-3-5-sonnet against claude-opus-4
                        # produces HTTP 400.
                        cur_model = self.cfg.model or None
                        if assistant_thinking_parts:
                            prev = assistant_thinking_parts[-1]
                            kept_sig = new_sig if new_sig else prev.signature
                            assistant_thinking_parts[-1] = ThinkingPart(
                                text=prev.text + delta,
                                signature=kept_sig,
                                model=prev.model or cur_model,
                            )
                        else:
                            assistant_thinking_parts.append(
                                ThinkingPart(
                                    text=delta,
                                    signature=new_sig,
                                    model=cur_model,
                                )
                            )

                    elif chunk.kind == ChunkKind.TOOL_CALL_DELTA:
                        if chunk.tool_call_id:
                            cid = chunk.tool_call_id
                        else:
                            cid = f"call_{self._tool_call_seq}"
                            self._tool_call_seq += 1
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
                        # F3 fix: provider parsers signal a JSON decode
                        # failure with ``tool_call_args=None`` plus a
                        # human-readable error string in
                        # ``tool_call_args_delta``. Record the call_id
                        # in ``self._malformed_tool_call_ids`` so
                        # ``_execute_tool`` skips registry execution
                        # and returns the ERROR straight to the model.
                        # The args themselves default to ``{}`` for
                        # downstream wire-shape (the tool_call still
                        # has to appear in the assistant message so
                        # the paired tool_result has a valid id).
                        parse_err: str | None = None
                        if chunk.tool_call_args is None and chunk.tool_call_args_delta:
                            parse_err = chunk.tool_call_args_delta
                            args = {}
                        elif chunk.tool_call_args is not None:
                            args = chunk.tool_call_args
                        else:
                            try:
                                args = json.loads(tc_info["args_buf"] or "{}")
                            except json.JSONDecodeError as exc:
                                preview = (tc_info["args_buf"] or "")[:200]
                                parse_err = (
                                    f"ERROR: invalid JSON in tool arguments: "
                                    f"{exc.msg} | preview: {preview}"
                                )
                                args = {}

                        tc_part = ToolCallPart(
                            id=cid,
                            name=tc_info["name"],
                            arguments=args,
                        )
                        assistant_tool_calls.append(tc_part)
                        if parse_err:
                            self._malformed_tool_call_ids[cid] = parse_err

                        # Classify danger level
                        from cogitum.core.builtin_tools import classify_danger
                        _danger = classify_danger(tc_info["name"], args)

                        await q.put(AgentToolCall(
                            tool_name=tc_info["name"],
                            arguments=args,
                            call_id=cid,
                            turn=iteration,
                            danger_level=_danger,
                        ))

                    elif chunk.kind == ChunkKind.USAGE:
                        total_usage = chunk.usage
                        if chunk.usage:
                            # input_tokens from the provider already covers
                            # the FULL prompt for this turn (system + entire
                            # history). Earlier this assignment used `+=`
                            # which double-counted across turns: by turn 10
                            # accumulated_tokens was ~10× the real prompt
                            # size, tripping compaction at ~10% real usage
                            # and wiping the tool_call/tool_result pairs
                            # the model needed. The cumulative shape was a
                            # quiet O(N²) inflation that mimicked "the
                            # model stopped seeing tool outputs in long
                            # sessions" — because compaction had eaten
                            # them.
                            accumulated_tokens = (
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
                    parse_err: str | None = None
                    try:
                        args = json.loads(tc_info["args_buf"] or "{}")
                    except json.JSONDecodeError as exc:
                        # F3 fix (flush path): a stream that aborted mid
                        # tool_call leaves args_buf as a truncated JSON
                        # fragment. Falling back to ``{}`` would let the
                        # tool execute with empty kwargs (e.g. ``git
                        # status`` runs unintentionally). Mark malformed
                        # so ``_execute_tool`` returns ERROR instead.
                        preview = (tc_info["args_buf"] or "")[:200]
                        parse_err = (
                            f"ERROR: invalid JSON in tool arguments: "
                            f"{exc.msg} | preview: {preview}"
                        )
                        args = {}
                    tc_part = ToolCallPart(id=cid, name=tc_info["name"], arguments=args)
                    assistant_tool_calls.append(tc_part)
                    if parse_err:
                        self._malformed_tool_call_ids[cid] = parse_err
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
                    # Persist now: an assistant message (text +
                    # reasoning + tool_calls) is the smallest
                    # crash-safe boundary. Even if the run dies
                    # before the tool batch executes, the model's
                    # commitment to call those tools is on disk.
                    # Wrapped in try/except so a broken consumer
                    # queue can't kill the run — persistence is a
                    # checkpoint hint, never a fatal contract.
                    try:
                        await q.put(AgentTurnPersist(
                            messages=list(messages),
                            iteration=iteration,
                        ))
                    except Exception:
                        log.exception(
                            "AgentTurnPersist after assistant commit "
                            "failed (suppressed)"
                        )

                # ── no tool calls → done ─────────────────────────────────
                if not assistant_tool_calls:
                    final_response_len = sum(
                        len(p.text) for p in assistant_text_parts
                    )
                    if assistant_text_parts:
                        turn_exit_reason = "end_turn"
                    elif assistant_thinking_parts:
                        # Reasoning-only response with no text and no
                        # tool_calls. Provider stopped but the model
                        # gave the user nothing visible.
                        turn_exit_reason = "thinking_only_response"
                    else:
                        # No text, no thinking, no tool_calls. Either
                        # the provider returned an empty stream or the
                        # stream ended without any content chunks.
                        turn_exit_reason = "empty_response"
                    break

                # ── execute tools in parallel (cancellable) ────────────────
                final_parts = await self._dispatch_tool_calls(
                    assistant_tool_calls=assistant_tool_calls,
                    queue=q,
                    iteration=iteration,
                )

                # tool results go in as a "tool" role message — but
                # only if we actually have results. Empty parts list
                # is wire-illegal (provider rejects on /resume) and
                # signals every tool was cancelled mid-flight before
                # producing anything; persisting an empty tool turn
                # would leave the session in that wire-illegal state.
                if final_parts:
                    messages.append(
                        Message(role="tool", parts=final_parts)
                    )
                    # Persist after tool results land: this is the
                    # most expensive thing we want crash-safe — a long
                    # terminal/file-read result that completed should
                    # survive a crash on the very next stream call.
                    # Wrapped in try/except for the same reason as
                    # the assistant-commit persist above.
                    try:
                        await q.put(AgentTurnPersist(
                            messages=list(messages),
                            iteration=iteration,
                        ))
                    except Exception:
                        log.exception(
                            "AgentTurnPersist after tool results "
                            "failed (suppressed)"
                        )

                # ── inject queued user messages between iterations ────────
                if inject_queue:
                    while not inject_queue.empty():
                        try:
                            injected_text = inject_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        messages.append(Message(role="user", parts=[TextPart(text=injected_text)]))
                        await q.put(AgentInjected(text=injected_text, turn=iteration))

            else:
                # while-else fires only when the loop's condition went
                # False naturally (i.e. max_turns hit, since `break`
                # paths set their own reason and skip the else). With
                # max_turns=0 (default) this branch is unreachable, but
                # tests and explicit-cap users still hit it.
                turn_exit_reason = (
                    f"max_turns_reached({iteration}/{self.cfg.max_turns})"
                )

            # NB: AgentDone is NOT emitted here — moved into the
            # finally below so it lands AFTER the fallback summary's
            # AgentText deltas. UI consumers treat AgentDone as
            # "stop rendering / re-enable input"; if it arrived
            # before the fallback's text, the closing summary would
            # be invisible to them.

        except asyncio.CancelledError:
            turn_exit_reason = "cancelled"
            raise
        except Exception as exc:
            turn_exit_reason = f"exception:{type(exc).__name__}"
            log.exception("Agent loop error")
            await q.put(AgentError(message=str(exc), exc=exc))

        finally:
            # Nested try/finally guarantees the diagnostic ALWAYS
            # runs, even if the fallback summary raises (including
            # CancelledError raised during its mesh.stream — the
            # outer except Exception below does NOT catch
            # BaseException, so cancel would otherwise skip past
            # both the diag block AND AgentDone). The diagnostic is
            # the canonical record of "what happened in this turn"
            # and operators rely on its presence.
            try:
                if not _suppress_fallback_summary:
                    try:
                        turn_exit_reason, final_response_len = (
                            await self._run_fallback_summary_if_needed(
                                messages=messages,
                                queue=q,
                                iteration=iteration,
                                turn_exit_reason=turn_exit_reason,
                                final_response_len=final_response_len,
                            )
                        )
                    except asyncio.CancelledError:
                        # User cancelled mid-fallback. Mark and let
                        # it propagate AFTER the diagnostic in the
                        # outer finally below.
                        turn_exit_reason = (
                            f"{turn_exit_reason}+fallback_cancelled"
                            if "+fallback" not in turn_exit_reason
                            else turn_exit_reason
                        )
                        raise

                # ── AgentDone ────────────────────────────────────
                # Emitted AFTER the fallback so the UI sees its
                # closing-summary deltas before the "stop rendering"
                # signal. Skipped on the exception path because
                # AgentError was already pushed there. Wrapped in
                # its own try/except so a broken caller queue
                # cannot turn a successful run into an exception
                # path — the diagnostic in the inner finally below
                # is the canonical record either way.
                if not turn_exit_reason.startswith("exception:") \
                        and turn_exit_reason != "cancelled":
                    try:
                        await q.put(
                            AgentDone(turns=iteration, usage=total_usage)
                        )
                    except Exception:
                        log.exception(
                            "AgentDone emission failed (suppressed)"
                        )
            finally:
                self._emit_turn_exit_diagnostic(
                    messages=messages,
                    turn_exit_reason=turn_exit_reason,
                    iteration=iteration,
                    final_response_len=final_response_len,
                )

        return messages

    async def _dispatch_tool_calls(
        self,
        *,
        assistant_tool_calls: list[ToolCallPart],
        queue: asyncio.Queue[AgentEvent],
        iteration: int,
    ) -> list[ToolResultPart]:
        """Run every ``assistant_tool_calls`` entry in parallel, collect results, repair wire shape.

        Extracted from ``run()``'s per-iteration body. Mirrors the
        original control flow exactly:

          1. Spawn one task per tool_call via
             ``_execute_tool_indexed`` and pin them on
             ``self._active_tool_tasks`` so the TUI can cancel them on
             ``Esc`` / ``/stop``.
          2. As tasks complete, dispatch sentinel results
             (``LEGION_RUN``, ``DELEGATE_WORKERS``, ``DELEGATE_EXPERTS``)
             through the matching helper, classify the result via
             ``_result_indicates_error`` for the legion/delegate paths
             and ``startswith("ERROR:")`` for plain tool calls, and
             stream each ``AgentToolResult`` to ``queue`` immediately
             so the UI sees results as they arrive.
          3. ``finally``: cancel any still-running tasks (with a
             bounded 10s drain) so a misbehaving tool that ignores
             ``CancelledError`` cannot wedge the run.
          4. Wire-shape repair: synthesize a placeholder
             ``ToolResultPart`` for every slot left ``None`` (cancel /
             crash mid-flight) so the next provider request never
             rejects the turn for "tool_use ids without matching
             tool_result".

        Returns the post-repair list of ``ToolResultPart`` entries.
        Length is ALWAYS equal to ``len(assistant_tool_calls)`` —
        this is the wire-shape invariant the caller depends on.

        Cancellation
        ------------
        ``asyncio.CancelledError`` from the outer run (user
        ``/stop``) propagates through this method so the run unwinds
        cleanly. Sibling-cancel of an individual child task does
        NOT propagate — its slot is repaired with a placeholder.
        """
        tool_tasks = [
            asyncio.create_task(self._execute_tool_indexed(i, tc, iteration, queue=queue))
            for i, tc in enumerate(assistant_tool_calls)
        ]
        # Expose tasks so TUI can cancel them on Esc
        self._active_tool_tasks = tool_tasks

        # Collect results as they complete (stream to UI immediately).
        # Slots stay None until populated; the wire-shape contract
        # requires len(parts) == len(assistant_tool_calls), so any
        # surviving None is replaced with a synthesized error part
        # in the finally block below.
        tool_result_parts: list[ToolResultPart | None] = [None] * len(assistant_tool_calls)

        try:
            # NOTE (UX, audit M3): this loop awaits each completed
            # task sequentially. Workers run in parallel via
            # asyncio.create_task above, so wall-clock latency is
            # unaffected — but the per-result postprocessing
            # (LEGION_RUN/DELEGATE_*, queue emit, error
            # classification) blocks the next iteration until done.
            # In practice each branch is sub-millisecond unless a
            # legion sub-run is invoked, in which case the whole
            # sub-run is awaited inline and other completed
            # workers wait. Acceptable for now: the legion path
            # is the only one that can stall, and parallelising
            # it would require restructuring the wire-shape
            # repair (slot indices have to land in
            # tool_result_parts in deterministic order).
            for coro in asyncio.as_completed(tool_tasks):
                try:
                    idx, result = await coro
                except asyncio.CancelledError:
                    # Distinguish "outer run cancelled" (user
                    # /stop) from "this child was cancelled
                    # externally" (sibling-cancel after error,
                    # explicit cancel of one task). On the
                    # outer-cancel path we MUST re-raise so
                    # the run unwinds. On the child-cancel
                    # path we leave the slot as None and let
                    # the placeholder synthesis below repair
                    # the wire shape.
                    current = asyncio.current_task()
                    if current is not None and current.cancelling() > 0:
                        raise
                    continue
                except BaseException:
                    # Catastrophic child failure that bypassed
                    # _execute_tool's broad Exception catch
                    # (e.g. KeyboardInterrupt-derived). Leave
                    # the slot None — synthesized below.
                    continue
                tc = assistant_tool_calls[idx]
                content = str(result)
                is_error = content.startswith("ERROR:")

                # Handle async-dispatched tool sentinels.
                # legion is the only one going forward; the
                # DELEGATE_* sentinels are kept ONLY because some
                # third-party MCP tools or legacy skills may still
                # emit them — new code should not.
                if content.startswith("LEGION_RUN:"):
                    content = await self._run_legion(content[11:])
                    # Use the shared classifier so a swarm that
                    # came back with "Workers completed (0/N …)"
                    # or a leaked traceback is still flagged.
                    is_error = _result_indicates_error(content)
                elif content.startswith("DELEGATE_WORKERS:"):
                    content = await self._run_delegate_workers(content[17:])
                    # Mirror LEGION_RUN: failures inside the delegated
                    # subagent come back as "ERROR:" / "REJECTED:" /
                    # "Internal error" prefixes, plus the
                    # "Workers completed (0/…)" formatter signal
                    # and bare-traceback leak. Hardcoding
                    # is_error=False silently swallowed those — the
                    # parent model couldn't tell a 50-worker swarm
                    # crashed.
                    is_error = _result_indicates_error(content)
                elif content.startswith("DELEGATE_EXPERTS:"):
                    content = await self._run_delegate_experts(content[17:])
                    is_error = _result_indicates_error(content)

                tool_result_parts[idx] = ToolResultPart(
                    tool_call_id=tc.id,
                    content=content,
                    is_error=is_error,
                )
                # Stream result to UI immediately. Wrapped in
                # suppress: a closed/overflowed consumer queue
                # must not crash the run loop — the wire-shape
                # repair below depends on tool_result_parts being
                # appended to messages, not on the UI seeing it.
                with contextlib.suppress(Exception):
                    await queue.put(AgentToolResult(
                        tool_name=tc.name,
                        call_id=tc.id,
                        result=content,
                        error=is_error,
                        turn=iteration,
                    ))
        finally:
            # Cancel any tasks still running. Reaches this branch on:
            #   (1) outer CancelledError (user /stop) — propagates after cleanup
            #   (2) one task raised an unexpected BaseException — siblings
            #       would otherwise keep running with no consumer (memory +
            #       provider-cost leak, race with next turn's batch)
            # We MUST drain so cancelled tasks don't surface as "Task was
            # destroyed but it is pending!" warnings on shutdown.
            pending = [t for t in tool_tasks if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                # Bounded drain. A misbehaving tool that ignores
                # CancelledError (e.g. `try: await sleep(60); except
                # CancelledError: continue`, or a sync subprocess.wait
                # without timeout in a thread executor) would otherwise
                # block this gather forever and the entire run would
                # be unkillable until process restart. 10s is generous
                # for any well-behaved cleanup path; anything beyond
                # that is a tool bug we surface and move on from.
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True),
                        timeout=10.0,
                    )
                except asyncio.TimeoutError:
                    log.warning(
                        "tool task drain timed out (10s); %d tasks "
                        "still pending after cancel — proceeding "
                        "without them. Likely a tool that ignores "
                        "CancelledError; check for sync subprocess "
                        "or shielded sleep in the tool implementation.",
                        sum(1 for t in pending if not t.done()),
                    )
            self._active_tool_tasks = []

        # Wire-shape repair: every assistant tool_call MUST have a
        # matching tool_result on the next turn or Anthropic/OpenAI
        # reject the request with "tool_use ids without matching
        # tool_result". Slots may stay None if the task was cancelled
        # mid-flight before populating, or if a BaseException leaked
        # past _execute_tool's try/except. Synthesize an error part
        # so the wire stays legal.
        for i, p in enumerate(tool_result_parts):
            if p is None:
                tc = assistant_tool_calls[i]
                # Include the tool name so the model can pick a
                # smarter retry strategy (re-run vs alternate tool)
                # — generic "[Result unavailable]" is too thin a
                # signal when 8 tools ran in parallel.
                placeholder_text = (
                    f"[Result unavailable for {tc.name} — tool "
                    "execution was cancelled or crashed before "
                    "producing a result. Re-run the tool if the "
                    "data is still needed.]"
                )
                tool_result_parts[i] = ToolResultPart(
                    tool_call_id=tc.id,
                    content=placeholder_text,
                    is_error=True,
                )
                # Emit a synthetic AgentToolResult so the UI shows
                # the missing slot rather than a phantom in-flight tool.
                # Wrapped in suppress for the same reason as the
                # streaming emit above: the run loop must not die
                # because a consumer dropped its queue.
                with contextlib.suppress(Exception):
                    await queue.put(AgentToolResult(
                        tool_name=tc.name,
                        call_id=tc.id,
                        result=placeholder_text,
                        error=True,
                        turn=iteration,
                    ))

        # After repair, every slot is non-None. Narrow the type
        # so downstream Message(parts=...) doesn't carry the
        # `| None` union (mypy/pyright would flag the original).
        final_parts: list[ToolResultPart] = [
            p for p in tool_result_parts if p is not None
        ]
        assert len(final_parts) == len(assistant_tool_calls), (
            "wire-shape invariant: every tool_call must have a "
            "matching tool_result after orphan-slot repair"
        )
        return final_parts

    async def _run_fallback_summary_if_needed(
        self,
        *,
        messages: list[Message],
        queue: asyncio.Queue[AgentEvent],
        iteration: int,
        turn_exit_reason: str,
        final_response_len: int,
    ) -> tuple[str, int]:
        """Run the closing-summary fallback when the turn produced no final text.

        Extracted from ``run()``'s outer ``finally``. Mutates
        ``messages`` in place by appending the synthesized assistant
        message; returns the (possibly updated) ``turn_exit_reason``
        and ``final_response_len`` so the caller can keep its
        diagnostic accurate.

        If the loop ended without producing a final assistant text
        response, ask the model for one extra closing turn WITHOUT
        tools so the user always gets a coherent end to the
        conversation. Hermes-agent does the same via
        ``_handle_max_iterations``.

        Trigger conditions (in priority order):
          * ``turn_exit_reason`` in ``{max_turns_reached*, empty_response,
            thinking_only_response}``
          * ``last_msg_role == "tool"`` (loop ended after a tool batch
            and the model never got to comment on it)

        Do NOT trigger on:
          * ``end_turn`` — we already have a final response
          * ``cancelled`` — user explicitly aborted
          * ``exception:*`` — ``AgentError`` event already fired; a
            second stream call risks the same exception

        Wrapped in try/except so a fallback failure can never mask the
        original exit. ``CancelledError`` is **re-raised** by this
        helper so the caller can update ``turn_exit_reason`` with
        ``+fallback_cancelled`` and ensure the OUTER finally's nested
        try still fires the diagnostic before propagation.
        """
        try:
            needs_fallback = (
                (
                    turn_exit_reason.startswith("max_turns_reached")
                    or turn_exit_reason in (
                        "empty_response",
                        "thinking_only_response",
                    )
                    or (messages and messages[-1].role == "tool")
                )
                and not turn_exit_reason.startswith("exception:")
                and turn_exit_reason != "cancelled"
            )
            if needs_fallback:
                summary_text = await self._emit_fallback_summary(
                    messages=list(messages),
                    queue=queue,
                    iteration=iteration,
                    reason=turn_exit_reason,
                )
                if summary_text:
                    messages.append(Message(
                        role="assistant",
                        parts=[TextPart(text=summary_text)],
                    ))
                    final_response_len = len(summary_text)
                    turn_exit_reason = (
                        f"{turn_exit_reason}+fallback_summary"
                    )
                    # Persist the fallback summary too —
                    # it's the user-visible final answer.
                    # Wrapped in try/except so a queue
                    # rejection can't poison the run.
                    try:
                        await queue.put(AgentTurnPersist(
                            messages=list(messages),
                            iteration=iteration,
                        ))
                    except Exception:
                        log.exception(
                            "AgentTurnPersist after fallback "
                            "failed (suppressed)"
                        )
        except asyncio.CancelledError:
            # Caller ``run()`` updates ``turn_exit_reason`` with the
            # ``+fallback_cancelled`` suffix in its own except handler.
            # We just re-raise here so the diagnostic still fires
            # before propagation.
            raise
        except Exception:
            log.exception(
                "Fallback summary failed (suppressed)"
            )
        return turn_exit_reason, final_response_len

    def _emit_turn_exit_diagnostic(
        self,
        *,
        messages: list[Message],
        turn_exit_reason: str,
        iteration: int,
        final_response_len: int,
    ) -> None:
        """Log the per-turn diagnostic line.

        Always logged, on EVERY exit path including cancellation that
        may have raised inside the fallback. WARNING level when the
        last message is a tool result — that's the "agent stopped
        mid-work" pattern users report as "model didn't see tool
        feedback". Hermes-agent uses the same shape.

        Wrapped in its own try/except so a malformed Message in
        ``messages`` can never poison Agent.run()'s return value
        (or, in the cancel path, mask the CancelledError).
        """
        try:
            last_msg_role = (
                messages[-1].role if messages else None
            )
            last_tool_name: str | None = None
            if last_msg_role == "tool":
                for m in reversed(messages):
                    if m.role == "assistant" and m.tool_calls:
                        last_tool_name = m.tool_calls[-1].name
                        break
            tool_turns = sum(
                1 for m in messages
                if m.role == "assistant" and m.tool_calls
            )

            diag = (
                "Turn ended: reason=%s model=%s iterations=%d "
                "tool_turns=%d last_msg_role=%s last_tool=%s "
                "response_len=%d messages=%d"
            )
            level = (
                logging.WARNING
                if last_msg_role == "tool"
                else logging.INFO
            )
            log.log(
                level,
                diag,
                turn_exit_reason,
                self.cfg.model or "?",
                iteration,
                tool_turns,
                last_msg_role,
                last_tool_name
                or ("?" if last_msg_role == "tool" else "—"),
                final_response_len,
                len(messages),
            )
        except Exception:
            log.exception(
                "Turn-exit diagnostic failed (suppressed)"
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _emit_fallback_summary(
        self,
        *,
        messages: list[Message],
        queue: asyncio.Queue[AgentEvent],
        iteration: int,
        reason: str,
    ) -> str:
        """One extra LLM call WITHOUT tools to close out an unfinished turn.

        Triggered from ``run()``'s finally when the loop ended without
        producing a final assistant text response (max_turns,
        empty_response, thinking_only_response, or last_msg_role==tool).
        Asks the model to summarise what's been done so the user
        always sees a coherent end to the conversation rather than a
        truncated half-turn or silent stop.

        Streams ``AgentText`` chunks to ``queue`` so the TUI/TG see
        the fallback the same way they see any other response.
        Returns the accumulated text (may be empty on stream failure).

        Implementation notes:
          * tools=[] — the model can't call more tools to escape
          * temperature=0 — we want a stable closing summary
          * sanitize_messages_for_provider stays in the path
            (called via _stream → mesh.stream contract); the call
            sites that bypass _stream and go straight to mesh.stream
            already wrap with sanitize_messages_for_provider.
          * No retry wrapper — if the fallback itself fails, we
            return "" and the diagnostic above logs the situation.
            Better to silently degrade than to recurse.
        """
        from cogitum.core.llm.mesh import StreamRequest
        from cogitum.core.memory import get_memory_context
        from datetime import datetime

        prefix = (
            "─── closing summary ───\n"
            f"[The agent loop ended without a final response "
            f"(reason={reason}). Producing a closing summary of what "
            "has been done so far without further tool use.]\n\n"
        )

        # Inject a one-off user nudge for the summarizer. We build a
        # NEW list via concatenation (`messages + [synthetic]`) so
        # the caller's history is never mutated and the synthetic
        # prompt cannot leak into persisted state.
        nudge = (
            "You've reached the end of the agent loop without "
            "producing a final response. Please summarise what you "
            "found, what tools you ran, and what the result is — "
            "without calling any more tools. If the work is "
            "incomplete, say so and explain what's left."
        )
        synthetic = Message(role="user", parts=[TextPart(text=nudge)])
        messages_for_summary = messages + [synthetic]

        # Build a thin system prompt — we don't need the full skills
        # catalog here, just the persona + memory + datetime context.
        system = self.cfg.system or ""
        mem = get_memory_context()
        if mem:
            system = f"{system}\n\n{mem}"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        platform_label = (
            "Telegram" if self.cfg.platform == "telegram" else "CLI TUI"
        )
        system = (
            f"{system}\n\n═══ CONTEXT ═══\n"
            f"Current time: {now}\nPlatform: {platform_label}"
        )

        req = StreamRequest(
            messages=sanitize_messages_for_provider(messages_for_summary),
            model=self.cfg.model or "",
            system=system,
            tools=[],  # critical: no tools, otherwise we recurse
            max_tokens=self.cfg.max_tokens,
            temperature=0.0,
        )

        accumulated: list[str] = []
        try:
            # Emit prefix INSIDE the try so a queue.put failure
            # (e.g. caller-supplied bounded queue rejecting writes)
            # is caught alongside the stream errors and we still
            # honour the docstring contract: "may be empty on
            # stream failure" — never raise, always return a string.
            await queue.put(AgentText(delta=prefix, turn=iteration))
            async for chunk in self.mesh.stream(req):
                if chunk.kind == ChunkKind.TEXT and chunk.text:
                    accumulated.append(chunk.text)
                    await queue.put(AgentText(delta=chunk.text, turn=iteration))
                elif chunk.kind == ChunkKind.ERROR:
                    log.warning(
                        "Fallback summary stream error: %s", chunk.error,
                    )
                    break
                # Ignore THINKING / TOOL_* / USAGE — fallback should
                # only produce text. If the model tries to emit a
                # tool call here despite tools=[], we simply drop it.
        except asyncio.CancelledError:
            # Let cancellation propagate; the caller's outer
            # finally will log the diagnostic and we don't want to
            # swallow the user's stop signal.
            raise
        except Exception:
            log.exception("Fallback summary stream raised")
            return ""

        return "".join(accumulated).strip()

    async def _stream(
        self,
        messages: list[Message],
        tools_schema: list[dict],
    ) -> AsyncIterator[StreamChunk]:
        """Delegate to mesh.stream() with current message history."""
        from cogitum.core.llm.mesh import StreamRequest
        from cogitum.core.memory import get_memory_context
        from cogitum.core.godmode import is_godmode_active
        from datetime import datetime

        # Inject persistent memory into system prompt
        system = self.cfg.system
        # When the user has explicitly toggled a godmode preset, do NOT
        # dilute it with the skill catalogue or other meta-instructions.
        # The skill summary tells the model "you MUST load skills before
        # answering", which is a competing directive that materially
        # weakens any jailbreak frame — the model averages the two and
        # the user perceives it as "godmode is being ignored". Memory
        # is still injected because users want their personal facts to
        # survive a jailbreak; only the catalogue gets suppressed.
        godmode_on = is_godmode_active(system)
        mem_ctx = get_memory_context()
        if mem_ctx:
            system = f"{system}\n\n{mem_ctx}"

        if not godmode_on:
            # Inject skills summary (compact list of available skills)
            from cogitum.core.skills import skill_summary
            skills_ctx = skill_summary()
            if skills_ctx:
                system = f"{system}\n\n{skills_ctx}"

        # Inject current datetime + platform context
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        platform_label = "Telegram" if self.cfg.platform == "telegram" else "CLI TUI"
        system = f"{system}\n\n═══ CONTEXT ═══\nCurrent time: {now}\nPlatform: {platform_label}"

        req = StreamRequest(
            messages=sanitize_messages_for_provider(messages),
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
        # Pick limits based on the user's preference. Modal off → 10
        # attempts then surface a regular AgentError (the pre-modal
        # behaviour the user explicitly asked to keep as default).
        # Modal on → 8 attempts but with an interactive escalation
        # after the 3rd failure, then again every 3 failures.
        modal_enabled = _retry_confirm_enabled()
        max_retries = _MAX_RETRIES if modal_enabled else _MAX_RETRIES_NO_MODAL
        # Tracks the attempt number of the most-recent modal (0 = never).
        # Used to gate "show another modal in 3 more failures" — keeps
        # the wait spacing between modals consistent regardless of the
        # absolute attempt count. Without this, after the first modal
        # (which fires at attempt 3) the threshold check
        # ``attempt_num >= 3`` was always true so every subsequent
        # failure popped a new modal back-to-back.
        last_modal_at = 0

        for attempt in range(max_retries + 1):
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
                if attempt >= max_retries or not _is_retryable_error(exc):
                    raise
                error_msg = str(exc)

            # Retry logic — pick delay by error class (rate_limit /
            # overloaded / network / server / pool / unknown), respecting
            # any authoritative hint (Retry-After / 'try again in X').
            delay, reason = _compute_retry_delay(last_exc, attempt)
            log.warning(
                "Stream attempt %d failed (%s) [class=%s], retrying in %.1fs",
                attempt + 1, error_msg, reason, delay,
            )

            # Escalate to a user-visible confirmation modal once we've
            # burned through ``_RETRY_CONFIRM_THRESHOLD`` attempts since
            # the last modal. Fire-and-forget — agent doesn't wait for
            # a reply. UI pops the modal in parallel; if the user
            # clicks Abort, the UI cancels ``_agent_task`` (same as
            # Esc), the cancellation propagates through asyncio and
            # the agent's backoff sleep below dies cleanly. If the
            # user clicks Continue (or the modal auto-continues), the
            # backoff sleep finishes and we go round again.
            attempt_num = attempt + 1
            klass = _classify_error(last_exc) if last_exc else _ErrorClass.UNKNOWN
            permanent_class = klass == _ErrorClass.QUOTA_EXCEEDED
            attempts_since_last_modal = attempt_num - last_modal_at
            should_confirm = modal_enabled and (
                permanent_class
                or attempts_since_last_modal >= _RETRY_CONFIRM_THRESHOLD
            )

            if should_confirm:
                trimmed = error_msg.strip()
                if len(trimmed) > 240:
                    trimmed = trimmed[:240] + "…"
                await queue.put(AgentRetryConfirm(
                    attempt=attempt_num,
                    max_attempts=max_retries,
                    error_class=klass,
                    error_message=trimmed,
                    auto_continue_in=_RETRY_CONFIRM_TIMEOUT,
                    turn=turn,
                ))
                last_modal_at = attempt_num

            # Notify TUI about retry (friendly status, not raw error)
            await queue.put(AgentRetry(
                attempt=attempt + 1,
                max_attempts=max_retries,
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
            log.debug("swallowed exception", exc_info=True)
        return 0

    def _estimate_prompt_tokens(self, messages: list[Message]) -> int:
        """Cheap pre-flight token estimate for the current message buffer.

        Authoritative ``USAGE`` input_tokens only arrive AFTER a turn
        has already been streamed by the provider. Relying on it alone
        gave us a class of failure where, on the first turn after a
        big paste or a /resume of a long history, we sent a request
        the provider rejected outright with ``context_length_exceeded``
        — the model never saw its own tools.

        Estimate uses the rough 4-chars-per-token heuristic, which
        over-estimates English text by ~10% and under-estimates
        compact tokens like file paths slightly; that's fine because
        compaction at 60 % gives plenty of headroom.

        ToolResultPart content is included in full (not truncated) —
        long tool outputs are the main reason a session balloons,
        and the estimate exists to catch exactly that case.
        """
        total_chars = (self.cfg.system or "").__len__()
        for msg in messages:
            for part in msg.parts:
                if isinstance(part, TextPart):
                    total_chars += len(part.text)
                elif isinstance(part, ThinkingPart):
                    total_chars += len(part.text)
                elif isinstance(part, ToolCallPart):
                    # arguments dump approximates wire size
                    try:
                        # ``ensure_ascii=False`` matches the wire serializer
                        # used elsewhere in this file. The default escapes
                        # every non-ASCII char as ``\uXXXX`` (~6× per glyph),
                        # which inflates the count for Russian/CJK args and
                        # triggers compaction prematurely on those locales.
                        total_chars += len(json.dumps(part.arguments, ensure_ascii=False))
                    except (TypeError, ValueError):
                        total_chars += 64
                    total_chars += len(part.name) + 16  # name + framing
                elif isinstance(part, ToolResultPart):
                    total_chars += len(part.content) + 32
        # 4 chars ≈ 1 token (GPT-style); rough but stable.
        return total_chars // 4

    async def _compact_context(
        self,
        messages: list[Message],
        queue: asyncio.Queue[AgentEvent],
    ) -> list[Message]:
        """Compact old context while keeping the recent tail intact.

        Old behaviour was destructive: the entire history was replaced
        by a single summary + a fake assistant ack. That broke three
        things at once —

          1. tool_use / tool_result pairs the model had just emitted
             disappeared, so on the very next turn it could not see
             its own latest work (the bug the user kept hitting).
          2. Each ToolResultPart was hard-truncated at 4 KB, dropping
             everything past 4000 chars from terminal stdout, file
             reads, browser scrapes — the summarizer never even saw
             the data, so the data was unrecoverable.
          3. A synthetic assistant message ("Understood. I have the
             full context.") was injected. The model treats its own
             prior turns as ground truth; a fabricated "I have it"
             response makes it *more* confident it remembers things
             it never saw.

        New shape:

          • Split messages into ``head`` (older) and ``tail`` (last
            ``_COMPACTION_KEEP_TAIL`` messages, with safety to keep
            tool_use/tool_result pairs together).
          • Summarize ONLY the head. The summary becomes a single
            user message at the start.
          • The tail is appended verbatim — every TextPart,
            ThinkingPart, ToolCallPart, ToolResultPart preserved
            byte-for-byte. The model walks back into its own work
            mid-thought.
          • No fake assistant ack. The summary is framed as
            background context the model is being briefed on, not
            as something it said.
          • Tool result truncation in the head is generous (16 KB)
            and explicitly tells the summarizer *what* was lost so
            it can record at least the existence of long outputs.

        Returns the new ``messages`` list. On any failure (empty
        summary, stream error) returns the input unchanged so the
        agent loop can keep going — better to risk hitting context
        limit than to wipe the user's session.
        """
        from cogitum.core.llm.mesh import StreamRequest

        if len(messages) <= _COMPACTION_KEEP_TAIL:
            # Nothing to compact — the whole buffer fits in the tail
            # window. Caller will retry once more usage data arrives.
            return messages

        # Find the split point. Start with naive index, then walk
        # backwards so a tool-role message is never separated from
        # the assistant message that produced its tool_calls — the
        # provider rejects orphan tool_results with a contract error.
        split_idx = len(messages) - _COMPACTION_KEEP_TAIL
        while split_idx > 0 and messages[split_idx].role == "tool":
            split_idx -= 1

        head = messages[:split_idx]
        tail = messages[split_idx:]
        if not head:
            # Whole buffer is one tightly-coupled tail. Compaction
            # can't help here — the tail itself is what's expensive.
            return messages

        # Render head into a compaction prompt. Tool results get a
        # generous cap (vs the old 4 KB) so the summarizer actually
        # sees the substance instead of guessing from a truncated
        # snippet.
        #
        # P0-2 fix (audit_tools_history.md): each ToolResultPart carries
        # its tool_call_id, but the structural pairing with the
        # originating ToolCallPart was previously dropped — the
        # summarizer saw "[tool_result]: 42" without knowing which
        # tool produced it or with which arguments. We now walk the
        # head once to build a tool_call_id → (name, args) map and
        # render results as
        #   [tool_result for <name>(<short_args>)]: <body>
        # so the summary preserves "what was called" alongside "what
        # came back".
        _HEAD_TOOL_RESULT_CAP = 16_000
        _ARGS_PREVIEW_CAP = 200
        tool_call_index: dict[str, tuple[str, str]] = {}
        for msg in head:
            for part in msg.parts:
                if isinstance(part, ToolCallPart):
                    args_repr = json.dumps(
                        part.arguments, ensure_ascii=False, default=str
                    )
                    if len(args_repr) > _ARGS_PREVIEW_CAP:
                        args_repr = (
                            args_repr[:_ARGS_PREVIEW_CAP]
                            + f"…[+{len(args_repr) - _ARGS_PREVIEW_CAP} chars]"
                        )
                    tool_call_index[part.id] = (part.name, args_repr)

        conversation_text_parts: list[str] = []
        for msg in head:
            role = msg.role
            for part in msg.parts:
                if isinstance(part, TextPart):
                    conversation_text_parts.append(f"[{role}]: {part.text}")
                elif isinstance(part, ThinkingPart):
                    # Thinking blocks rarely need to survive into the
                    # summary — they're internal scratch, not facts.
                    # Skip them to keep the summary focused.
                    continue
                elif isinstance(part, ToolCallPart):
                    args_repr = tool_call_index.get(
                        part.id,
                        (
                            part.name,
                            json.dumps(part.arguments, ensure_ascii=False, default=str),
                        ),
                    )[1]
                    conversation_text_parts.append(
                        f"[{role}]: tool_call {part.name}(id={part.id}, "
                        f"args={args_repr})"
                    )
                elif isinstance(part, ToolResultPart):
                    body = part.content
                    if len(body) > _HEAD_TOOL_RESULT_CAP:
                        omitted = len(part.content) - _HEAD_TOOL_RESULT_CAP
                        body = (
                            body[:_HEAD_TOOL_RESULT_CAP]
                            + f"\n…[truncated {omitted} chars; "
                            f"full result was {len(part.content)} chars]"
                        )
                    matching = tool_call_index.get(part.tool_call_id)
                    if matching is not None:
                        call_name, call_args = matching
                        header = (
                            f"[tool_result for {call_name}({call_args}) "
                            f"id={part.tool_call_id}]"
                        )
                    else:
                        # Orphaned result — keep id so the summarizer
                        # still has something to anchor on.
                        header = f"[tool_result id={part.tool_call_id}]"
                    conversation_text_parts.append(f"{header}: {body}")

        conversation_dump = "\n".join(conversation_text_parts)
        compaction_prompt = (
            "You are summarizing the EARLIER part of an ongoing agent "
            "session. The agent will continue working immediately after "
            "your summary, with the most recent turns kept verbatim — "
            "you only need to compress what came before.\n\n"
            "PRESERVE EXACTLY:\n"
            "  • All file paths, identifiers, URLs, exact names\n"
            "  • All decisions made and the reasoning behind them\n"
            "  • Substantive tool output (file contents, command "
            "stdout, search hits, scraped data) — keep verbatim where "
            "the agent is likely to reference it later\n"
            "  • Errors encountered and how they were resolved\n"
            "  • Anything the user asked for that hasn't been done yet\n\n"
            "OK TO DROP:\n"
            "  • Pleasantries, filler text, retry chatter\n"
            "  • Intermediate thinking / scratch reasoning\n"
            "  • Tool calls whose results are already reflected in "
            "later state (file was read then rewritten — keep the "
            "rewrite, drop the original read)\n\n"
            "Format the summary as a tight briefing, not a transcript. "
            "Verbosity is fine if the data demands it — losing context "
            "is the only failure mode that matters.\n\n"
            "═══ EARLIER SESSION ═══\n"
            f"{conversation_dump}"
        )

        compaction_messages = [
            Message(role="user", parts=[TextPart(text=compaction_prompt)])
        ]
        req = StreamRequest(
            messages=compaction_messages,
            model=self.cfg.model or "",
            system=(
                "You are a precise context-compaction summarizer for an "
                "agent session. Preserve every fact the agent will need "
                "to continue its work. Be thorough — losing detail is "
                "the only thing that hurts here."
            ),
            tools=[],
            max_tokens=self.cfg.max_tokens,
            temperature=0.0,
        )

        summary_buf: list[str] = []
        try:
            async for chunk in self.mesh.stream(req):
                if chunk.kind == ChunkKind.TEXT:
                    summary_buf.append(chunk.text)
                elif chunk.kind == ChunkKind.ERROR:
                    log.warning("Compaction stream error: %s", chunk.error)
                    return messages  # keep original on failure
        except Exception:
            log.warning("Compaction crashed", exc_info=True)
            return messages

        compacted_summary = "".join(summary_buf).strip()
        if not compacted_summary:
            log.warning("Compaction produced empty summary; keeping original")
            return messages

        # Stitch: one user message holding the briefing, then the
        # untouched tail. NO fake assistant ack — the briefing is
        # framed as the agent being told what came before, which is
        # the truth.
        briefing_text = (
            "═══ CONTEXT BRIEFING (earlier in this session) ═══\n"
            f"{compacted_summary}\n"
            "═══ END BRIEFING — recent turns follow verbatim ═══"
        )
        rebuilt: list[Message] = [
            Message(role="user", parts=[TextPart(text=briefing_text)])
        ]
        rebuilt.extend(tail)
        return rebuilt

    async def compact_now(
        self,
        messages: list[Message],
        queue: asyncio.Queue[AgentEvent] | None = None,
    ) -> tuple[list[Message], int, int]:
        """Public manual-compaction entrypoint for ``/compact``.

        Returns ``(new_messages, before_tokens, after_tokens)``. Emits
        an ``AgentCompacted`` event on ``queue`` if provided so the
        TUI can refresh its inspector counters.

        ``AgentCompacted.status`` distinguishes four outcomes for
        UX clarity:
          * ``not_needed`` — buffer ≤ ``_COMPACTION_KEEP_TAIL`` so
            compaction would be a no-op. Consumer should tell the
            user "history is small, nothing to compact" instead of
            the misleading "context compacted (~X → ~X tokens, N → N
            messages)" they got before.
          * ``no_change`` — compaction ran but produced an identical
            buffer (summarizer empty/error fallback). Consumer
            should tell the user "compaction didn't reduce size".
          * ``ok`` — buffer actually shrank.
          * ``busy`` — a concurrent in-run compaction is already
            holding the lock; we refuse rather than racing it. The
            consumer should retry once the run is idle.

        Distinct from the in-loop trigger so the TUI can call this
        from the main coroutine while the agent is idle (no live
        ``run()`` task), without having to fake a turn.
        """
        msgs_before = len(messages)
        before_tokens = self._estimate_prompt_tokens(messages)

        # Pre-flight: compaction is a no-op when the whole buffer
        # already fits in the keep-tail window. Surface that clearly
        # via status='not_needed' instead of doing the work and
        # showing "X → X" deltas.
        if msgs_before <= _COMPACTION_KEEP_TAIL:
            if queue is not None:
                await queue.put(AgentCompacted(
                    before_tokens=before_tokens,
                    after_tokens=before_tokens,
                    messages_before=msgs_before,
                    messages_after=msgs_before,
                    manual=True,
                    status="not_needed",
                ))
            return messages, before_tokens, before_tokens

        # Refuse to race an in-run auto-compaction. The lock is held
        # by run() during _compact_context; if we can't grab it
        # immediately, the next AgentTurnPersist would clobber our
        # snapshot anyway, so bail with a 'busy' status the consumer
        # can surface as "agent busy, try again".
        if self._compact_lock.locked():
            if queue is not None:
                await queue.put(AgentCompacted(
                    before_tokens=before_tokens,
                    after_tokens=before_tokens,
                    messages_before=msgs_before,
                    messages_after=msgs_before,
                    manual=True,
                    status="busy",
                ))
            return messages, before_tokens, before_tokens

        async with self._compact_lock:
            new_messages = await self._compact_context(
                messages, queue or asyncio.Queue()
            )
        after_tokens = self._estimate_prompt_tokens(new_messages)

        # Post-flight: did the work actually do anything? An empty
        # summarizer response or a stream error makes _compact_context
        # return the original buffer unchanged. The user shouldn't
        # see "compacted ~X → ~X" in that case either.
        if (
            len(new_messages) == msgs_before
            and after_tokens == before_tokens
        ):
            status = "no_change"
        else:
            status = "ok"

        if queue is not None:
            await queue.put(AgentCompacted(
                before_tokens=before_tokens,
                after_tokens=after_tokens,
                messages_before=msgs_before,
                messages_after=len(new_messages),
                manual=True,
                status=status,
            ))
        return new_messages, before_tokens, after_tokens

    async def _execute_tool_indexed(
        self,
        index: int,
        tc: ToolCallPart,
        turn: int,
        queue: asyncio.Queue | None = None,
    ) -> tuple[int, str]:
        """Execute a tool and return (index, result) for as_completed matching."""
        result = await self._execute_tool(tc, turn, queue=queue)
        return (index, result)

    async def _execute_tool(
        self,
        tc: ToolCallPart,
        turn: int,
        queue: asyncio.Queue | None = None,
    ) -> str:
        """Execute a single tool call and return its string result.

        Gating
        ------
        When ``self._approval_queue`` is non-None AND
        ``classify_danger`` reports medium/danger AND
        ``self.cfg.yolo_mode`` is False, this method:
          1. Registers a per-call_id Future in ``self._approval_futures``.
          2. Emits an ``AgentApprovalRequest`` on the event queue.
          3. Blocks on ``asyncio.wait_for(fut, timeout=300.0)``.
          4. Cleans up the future in ``finally`` (only if still ours).
        Decisions are routed by ``Agent.submit_approval(call_id, decision)``.

        Returns
        -------
        ``"REJECTED: ..."``
            User rejected, modify-payload malformed, modify-payload not
            a JSON object, approval timed out (300s), or duplicate
            call_id pre-empted by a newer one.
        ``"ERROR: ..."``
            Tool raised, KeyError (unknown tool), TypeError (bad args),
            or 300s execution timeout.
        ``str(result)``
            Happy path — the tool's stringified return value.

        Raises
        ------
        asyncio.CancelledError
            Propagates from ``registry.execute`` when the user
            initiates a /stop or Esc. We deliberately do NOT catch it
            here: catching CancelledError and returning a string would
            let the agent loop happily continue past a cancellation
            into the next turn. The parallel batch caller has a
            ``finally`` block that cancels siblings and synthesizes
            placeholder ToolResultParts to keep the wire shape legal.
        """
        from cogitum.core.builtin_tools import classify_danger

        # F3 fix: short-circuit before any approval / registry call when
        # the streamed arguments JSON failed to parse. Returning the
        # ERROR string here means the model sees an explicit
        # "your arguments were malformed" diagnostic instead of either
        # (a) silently running the tool with an empty {} (truncated
        # stream → unintended ``git status`` etc.) or (b) the older
        # ``_raw=...`` injection that crashed strict-signature tools
        # with TypeError. Once consumed, the entry is popped so a
        # legitimate retry on the same call_id (provider replay) can
        # execute normally.
        parse_err = self._malformed_tool_call_ids.pop(tc.id, None)
        if parse_err:
            return parse_err

        # Default exec args. Approval-modify path may override below;
        # the gating block is the only place that can change this.
        exec_args = tc.arguments

        # Check danger level and request approval if needed.
        early_return, exec_args = await self._acquire_tool_approval(
            tc=tc,
            turn=turn,
            queue=queue,
            exec_args=exec_args,
        )
        if early_return is not None:
            return early_return

        try:
            result = await asyncio.wait_for(
                self.registry.execute(tc.name, exec_args),
                timeout=300.0,  # 5 min max per tool (background can be long)
            )
            # F6 + F7 fix: route every successful return through the
            # central formatter so dict/list/None/empty-string results
            # arrive at the model in predictable text. ``str(result)``
            # used to emit "None", Python repr ({'a': 1}) and "" — all
            # of which the model misreads as "tool produced nothing"
            # and re-calls the same tool.
            return _format_tool_result_for_model(result)
        except asyncio.TimeoutError:
            return f"ERROR: tool '{tc.name}' timed out after 300s. Try mode='background' for long-running commands."
        # NOTE: asyncio.CancelledError MUST propagate. Catching it here
        # would turn a user-initiated /stop or Esc into a normal tool
        # result string, and the agent loop would happily continue into
        # the next turn instead of unwinding. The parallel batch finally
        # block at the call site is responsible for cancelling siblings
        # and synthesizing placeholder ToolResultParts for the wire.
        except KeyError as exc:
            return self._format_tool_error(exc, tc)
        except TypeError as exc:
            return self._format_tool_error(exc, tc)
        except Exception as exc:
            return self._format_tool_error(exc, tc)

    async def _acquire_tool_approval(
        self,
        *,
        tc: ToolCallPart,
        turn: int,
        queue: asyncio.Queue | None,
        exec_args: dict,
    ) -> tuple[str | None, dict]:
        """Run the danger-gate / YOLO-TTL / approval flow for one tool call.

        Extracted from ``_execute_tool``. Returns
        ``(early_return, exec_args)``:
          * ``early_return`` is ``None`` when the call should proceed
            to actual execution. The caller passes ``exec_args`` to
            ``registry.execute``.
          * ``early_return`` is a non-None string when the call must
            short-circuit (rejection, malformed modify payload,
            duplicate-id preempt, approval timeout). The caller
            returns it directly to the agent loop.

        Behaviour mirrors the original inline block:

          * YOLO TTL expiry is checked **before** danger
            classification; an expired window is auto-disabled and
            logged once (``time.monotonic`` so an NTP step-back can't
            silently extend the privileged window).
          * Per-``call_id`` futures: a duplicate id pre-empts the
            in-flight waiter with ``_DUPLICATE_ID_SENTINEL`` so the
            old waiter unwinds with an honest "preempted" diagnostic
            rather than a misleading "user denied" string.
          * The cleanup ``finally`` only pops the futures map slot
            when it still holds **our** future, so a duplicate-id
            replacement stays addressable.
        """
        from cogitum.core.builtin_tools import classify_danger

        # YOLO mode short-circuits the gate entirely — the user has
        # opted into "no questions asked" autonomy for this run.
        # F38: if a TTL was set on /yolo on <minutes>, expire it lazily
        # the first time the gate fires past the deadline. Logged once
        # per expiry so the operator sees the auto-disable in the daemon
        # log even if the chat session itself doesn't surface it. We use
        # time.monotonic() rather than time.time() so an NTP step-back
        # (or DST/clock-edit) can't silently extend a privileged window.
        # yolo_until is set on /yolo on <minutes> from monotonic + ttl
        # and never persisted across restarts, so monotonic semantics
        # are safe (TTL resets on process restart, which is the right
        # security default anyway).
        if (
            self.cfg.yolo_mode
            and self.cfg.yolo_until
            and time.monotonic() > self.cfg.yolo_until
        ):
            self.cfg.yolo_mode = False
            self.cfg.yolo_until = None
            log.info("/yolo expired — TTL elapsed, approval prompts restored")
        danger = classify_danger(tc.name, tc.arguments)
        if not (
            danger in ("medium", "danger")
            and self._approval_queue is not None
            and not self.cfg.yolo_mode
        ):
            # Security guard: silent auto-approve when the gating
            # condition above falls through. The most dangerous case is
            # ``_approval_queue is None`` AND the tool is non-low — that
            # means a headless or mis-wired front-end is executing
            # medium/danger tools without ever showing the user a
            # confirmation. Don't BLOCK (preserves existing batch /
            # script behavior), but make the gap loud in the log so
            # operators notice the missing approval consumer.
            if (
                danger != "low"
                and self._approval_queue is None
                and not self.cfg.yolo_mode
            ):
                log.warning(
                    "No approval queue wired, auto-approving %s tool %s",
                    danger, tc.name,
                )
            return None, exec_args

        # Per-call_id approval: register a Future, emit the
        # request, wait for our specific decision (NOT the next
        # one off a shared queue — that path swapped decisions
        # when the user replied out of order in a parallel batch).
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        # Defensive: collision on call_id MUST be loud, not silent.
        # If a duplicate id arrives (provider replay, MCP server
        # emitting non-unique ids, our own stream parser not
        # filtering dupes), the new entry would otherwise overwrite
        # the live future and the original call would hang for the
        # full 300s timeout. Reject the orphan immediately so it
        # unwinds with a clear REJECTED message instead.
        existing = self._approval_futures.get(tc.id)
        if existing is not None and not existing.done():
            log.warning(
                "approval future collision on call_id=%s — "
                "preempting the stale call so it unwinds",
                tc.id,
            )
            # Use a distinct sentinel so the OLD waiter can render
            # an honest user-facing message ("preempted by duplicate
            # call_id") instead of "user denied execution" — the
            # operator never actually denied anything.
            existing.set_result(_DUPLICATE_ID_SENTINEL)
        self._approval_futures[tc.id] = fut
        # Emit approval request
        if queue:
            await queue.put(AgentApprovalRequest(
                tool_name=tc.name,
                arguments=tc.arguments,
                call_id=tc.id,
                danger_level=danger,
                turn=turn,
            ))
        # Wait for approval decision
        try:
            decision = await asyncio.wait_for(fut, timeout=300.0)
            if decision == _DUPLICATE_ID_SENTINEL:
                # The provider issued a *new* tool_use with the
                # same call_id while this one was still pending.
                # The new call took our slot in
                # ``_approval_futures``. Return an honest
                # diagnostic so the model sees what happened
                # rather than a misleading "user denied" string.
                return (
                    f"ERROR: tool call {tc.name} was preempted "
                    f"by a duplicate call_id from the provider"
                ), exec_args
            if decision == "reject":
                return f"REJECTED: user denied execution of {tc.name}", exec_args
            elif decision.startswith("modify:"):
                # User modified the arguments. We do NOT mutate `tc` in
                # place: it is shared with `messages` and with every
                # AgentTurnPersist snapshot already emitted this turn.
                # Mutating it would retroactively alter audit history.
                # Instead, override exec_args locally for this run only.
                import json as _json
                payload = decision.removeprefix("modify:")
                try:
                    parsed = _json.loads(payload)
                except _json.JSONDecodeError:
                    # Malformed JSON in modify payload. Falling back to
                    # the original args would silently turn "modify"
                    # into "approve" — that's a contract violation:
                    # the user explicitly chose modify, not approve.
                    # Reject loudly so the model retries instead of
                    # executing the original (possibly dangerous) args.
                    log.warning(
                        "submit_approval modify payload had invalid "
                        "JSON for call_id=%s; rejecting rather than "
                        "silently approving original args",
                        tc.id,
                    )
                    return (
                        f"REJECTED: modify payload for {tc.name} was malformed JSON",
                        exec_args,
                    )
                if not isinstance(parsed, dict):
                    # Modify payload must be an object — anything else
                    # (list, scalar) is malformed and reaches the same
                    # contract violation as bad JSON.
                    log.warning(
                        "submit_approval modify payload was not a "
                        "dict for call_id=%s (got %s); rejecting",
                        tc.id, type(parsed).__name__,
                    )
                    return (
                        f"REJECTED: modify payload for {tc.name} was not a JSON object",
                        exec_args,
                    )
                exec_args = parsed
            # decision == "approve" path: exec_args stays as-is (default).
            # submit_approval already filters anything that's not one
            # of {"approve", "reject", "modify:<json>"} so we can't
            # land here with garbage.
        except asyncio.TimeoutError:
            return f"REJECTED: approval timed out for {tc.name}", exec_args
        finally:
            # Always clean up the future, even on cancel/timeout, so
            # a late `submit_approval` call from a stale UI click
            # cannot resolve a future that's no longer being awaited.
            #
            # IMPORTANT: only pop if the map slot still holds OUR
            # future. A duplicate-id call may have already replaced
            # us with a different future (with the old one resolved
            # to "reject" so it unwinds). Popping unconditionally
            # would orphan that replacement future and the second
            # call would hang forever.
            if self._approval_futures.get(tc.id) is fut:
                self._approval_futures.pop(tc.id, None)

        return None, exec_args

    def _format_tool_error(
        self,
        exc: BaseException,
        tc: ToolCallPart,
    ) -> str:
        """Format a tool execution exception into the agent-facing ``ERROR:`` string.

        Dispatches on ``type(exc)``:
          * ``KeyError`` — unknown tool. Suggests close matches and lists
            the first 20 known tool names.
          * ``TypeError`` — usually wrong/missing kwarg. Extracts the bad
            argument name from the exception message and emits a FIX hint.
          * Any other ``Exception`` — generic failure with type name and
            the args the model passed.

        ``KeyError`` and ``TypeError`` paths preserve the same
        ``log.warning(...)`` calls the inline branches used to make so
        operator log shape is unchanged. ``KeyError`` was previously
        unlogged on the inline path; that's preserved here too — the
        \"unknown tool\" string fed back to the model is the diagnostic.
        """
        if isinstance(exc, KeyError):
            # Suggest similar tool names
            available = list(self.registry._tools.keys()) if hasattr(self.registry, '_tools') else []
            from difflib import get_close_matches
            suggestions = get_close_matches(tc.name, available, n=3, cutoff=0.4)
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            return (
                f"ERROR: unknown tool '{tc.name}'.{hint} "
                f"Available tools: {', '.join(sorted(available)[:20])}. "
                f"DO NOT call this tool again. Pick a real one from the list above."
            )
        if isinstance(exc, TypeError):
            # Most common: wrong argument names
            msg = str(exc)
            hint = ""
            if "unexpected keyword argument" in msg:
                # Extract bad arg name
                import re
                m = re.search(r"unexpected keyword argument '([^']+)'", msg)
                if m:
                    bad = m.group(1)
                    hint = f"\nFIX: Tool '{tc.name}' has no parameter '{bad}'. Check the tool signature in your system prompt."
            elif "missing" in msg and "required" in msg:
                hint = f"\nFIX: You forgot a required argument for '{tc.name}'."
            log.warning("Tool %s TypeError: %s | args=%s", tc.name, exc, tc.arguments)
            return f"ERROR: TypeError in '{tc.name}': {exc}{hint}\nDO NOT repeat the same call. Read the error and fix the arguments."
        log.warning("Tool %s raised: %s | args=%s", tc.name, exc, tc.arguments)
        return (
            f"ERROR: {type(exc).__name__} in '{tc.name}': {exc}\n"
            f"Args you passed: {tc.arguments}\n"
            f"DO NOT repeat the same call with the same args. "
            f"Either fix the arguments or try a different approach."
        )

    # ------------------------------------------------------------------
    # Legion (recursive 2-level swarm) — supersedes delegate_task
    # ------------------------------------------------------------------

    async def _run_legion(self, payload_json: str) -> str:
        """Drive a Legion run dispatched by the LLM.

        The legion tool returns the sentinel ``LEGION_RUN:<json>`` to
        the agent loop; we strip the prefix and dispatch through the
        global Legion orchestrator. Returns the aggregated summary
        (string) that gets fed back into L0's context as the tool
        result.
        """
        import json
        from .legion import get_legion

        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as e:
            return f"ERROR: invalid legion payload: {e}"

        tasks = payload.get("tasks") or []
        if not isinstance(tasks, list) or not tasks:
            return "ERROR: legion tasks must be a non-empty list"

        root_goal = payload.get("root_goal", "")

        try:
            run = await get_legion().start_run(
                root_goal=root_goal,
                tasks=tasks,
            )
        except (ValueError, RuntimeError) as e:
            return f"ERROR: {e}"

        n_l1 = len(run.l1_nodes())
        n_done = sum(1 for n in run.nodes.values() if n.status.value == "done")
        header = (
            f"Legion {run.run_id} complete: "
            f"{n_done}/{len(run.nodes)} nodes succeeded "
            f"({n_l1} L1 cogitators)\n"
        )
        return header + "\n" + run.summary

    # ------------------------------------------------------------------
    # Delegate task execution
    # ------------------------------------------------------------------

    async def _run_delegate_workers(self, payload_json: str) -> str:
        """Execute parallel worker agents.

        Wrapped in a top-level try/except so any exception leaking out
        of the delegate machinery (JSON edge cases, mesh failures,
        registry import errors, asyncio cancellation paths) becomes an
        ``ERROR: …`` string the dispatcher classifier flags as failure
        instead of bubbling up and killing the run loop. The previous
        behaviour terminated the entire agent run on a single bad
        worker payload — confirmed via M1 audit.
        """
        try:
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
        except asyncio.CancelledError:
            # Cancellation must propagate — the run loop is responsible
            # for cleaning up, never us.
            raise
        except Exception as e:
            log.exception("delegate_workers crashed")
            return f"ERROR: delegate_workers crashed: {type(e).__name__}: {e}"

    async def _run_delegate_experts(self, payload_json: str) -> str:
        """Execute expert review board.

        Same top-level guard as ``_run_delegate_workers``: any exception
        becomes an ``ERROR: …`` string so M1's classifier surfaces it
        and the run loop survives.
        """
        try:
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
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("delegate_experts crashed")
            return f"ERROR: delegate_experts crashed: {type(e).__name__}: {e}"
