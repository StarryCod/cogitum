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
    except Exception:
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
    """
    before_tokens: int
    after_tokens: int
    messages_before: int
    messages_after: int
    manual: bool = False


@dataclass
class AgentError:
    message: str
    exc: BaseException | None = None


AgentEvent = AgentText | AgentThinking | AgentRetry | AgentRetryConfirm | AgentToolCall | AgentApprovalRequest | AgentToolResult | AgentInjected | AgentDone | AgentCompacted | AgentError

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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        user_message: str,
        history: list[Message] | None = None,
        queue: asyncio.Queue[AgentEvent] | None = None,
        inject_queue: asyncio.Queue[str] | None = None,
        approval_queue: asyncio.Queue[str] | None = None,
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
            If provided, medium/danger tool calls emit AgentApprovalRequest
            and wait for a decision string: ``"approve"``, ``"reject"``,
            or ``"modify:<new_args_json>"``. The call_id is NOT part of
            the payload — each agent run owns one queue and decisions are
            consumed FIFO, so ordering pairs them with the corresponding
            request automatically. If None, all tools execute without
            approval.

        Returns
        -------
        list[Message]
            Updated history including the new messages from this run.
        """
        q = queue or asyncio.Queue()
        self._approval_queue = approval_queue
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
                    asyncio.create_task(self._execute_tool_indexed(i, tc, iteration, queue=q))
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

                        # Handle async-dispatched tool sentinels.
                        # legion is the only one going forward; the
                        # DELEGATE_* sentinels are kept ONLY because some
                        # third-party MCP tools or legacy skills may still
                        # emit them — new code should not.
                        if content.startswith("LEGION_RUN:"):
                            content = await self._run_legion(content[11:])
                            is_error = content.startswith("ERROR:")
                        elif content.startswith("DELEGATE_WORKERS:"):
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
                        await q.put(AgentInjected(text=injected_text, turn=iteration))

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
                        total_chars += len(json.dumps(part.arguments))
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
        _HEAD_TOOL_RESULT_CAP = 16_000
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
                    conversation_text_parts.append(
                        f"[{role}]: tool_call({part.name}, "
                        f"{json.dumps(part.arguments, ensure_ascii=False)})"
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
                    conversation_text_parts.append(f"[tool_result]: {body}")

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

        Distinct from the in-loop trigger so the TUI can call this
        from the main coroutine while the agent is idle (no live
        ``run()`` task), without having to fake a turn.
        """
        msgs_before = len(messages)
        before_tokens = self._estimate_prompt_tokens(messages)
        new_messages = await self._compact_context(
            messages, queue or asyncio.Queue()
        )
        after_tokens = self._estimate_prompt_tokens(new_messages)
        if queue is not None:
            await queue.put(AgentCompacted(
                before_tokens=before_tokens,
                after_tokens=after_tokens,
                messages_before=msgs_before,
                messages_after=len(new_messages),
                manual=True,
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
        
        If approval_queue is set and tool is medium/danger, waits for approval.
        """
        from cogitum.core.builtin_tools import classify_danger

        # Check danger level and request approval if needed.
        # YOLO mode short-circuits the gate entirely — the user has
        # opted into "no questions asked" autonomy for this run.
        danger = classify_danger(tc.name, tc.arguments)
        if (danger in ("medium", "danger")
                and self._approval_queue is not None
                and not self.cfg.yolo_mode):
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
                decision = await asyncio.wait_for(
                    self._approval_queue.get(), timeout=300.0  # 5 min to decide
                )
                if decision == "reject":
                    return f"REJECTED: user denied execution of {tc.name}"
                elif decision.startswith("modify:"):
                    # User modified the arguments
                    import json as _json
                    try:
                        tc.arguments = _json.loads(decision[7:])
                    except _json.JSONDecodeError:
                        pass  # keep original args
                # "approve" or modified — proceed with execution
            except asyncio.TimeoutError:
                return f"REJECTED: approval timed out for {tc.name}"

        try:
            result = await asyncio.wait_for(
                self.registry.execute(tc.name, tc.arguments),
                timeout=300.0,  # 5 min max per tool (background can be long)
            )
            return str(result)
        except asyncio.TimeoutError:
            return f"ERROR: tool '{tc.name}' timed out after 300s. Try mode='background' for long-running commands."
        except asyncio.CancelledError:
            return "ERROR: tool execution cancelled by user"
        except KeyError:
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
        except TypeError as exc:
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
        except Exception as exc:
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
