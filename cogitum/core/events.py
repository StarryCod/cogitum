"""
Domain types — the lingua franca between engine and TUI.

Everything that crosses the boundary between agent loop, LLM mesh, tool
executor and the Textual app uses these types. Keep them dumb, immutable
where possible, and JSON-serializable.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


# ---------------------------------------------------------------------------
# IDs
# ---------------------------------------------------------------------------

def _id() -> str:
    """Short, sortable id (timestamp prefix + random suffix)."""
    return f"{int(time.time() * 1000):x}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Roles & content
# ---------------------------------------------------------------------------

Role = Literal["system", "user", "assistant", "tool", "function", "developer"]


@dataclass(slots=True)
class TextPart:
    text: str
    kind: Literal["text"] = "text"


@dataclass(slots=True)
class ThinkingPart:
    """Reasoning / chain-of-thought block.

    `signature` carries provider-specific opaque data needed to round-trip
    thinking back into a follow-up request (Anthropic uses it; OpenAI's
    o-series passes it through `reasoning_content`).
    """
    text: str
    signature: str | None = None
    kind: Literal["thinking"] = "thinking"


@dataclass(slots=True)
class ImagePart:
    """Vision input. Either url or base64 data."""
    url: str | None = None
    data: str | None = None       # base64
    mime: str = "image/png"
    kind: Literal["image"] = "image"


@dataclass(slots=True)
class ToolCallPart:
    id: str
    name: str
    arguments: dict[str, Any]
    kind: Literal["tool_call"] = "tool_call"


@dataclass(slots=True)
class ToolResultPart:
    tool_call_id: str
    content: str                  # always stringified for transport
    is_error: bool = False
    kind: Literal["tool_result"] = "tool_result"


ContentPart = TextPart | ThinkingPart | ImagePart | ToolCallPart | ToolResultPart


# ---------------------------------------------------------------------------
# Messages & turns
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Message:
    role: Role
    parts: list[ContentPart] = field(default_factory=list)
    id: str = field(default_factory=_id)
    timestamp: float = field(default_factory=time.time)
    # Provider/model that produced an assistant message — useful for the UI.
    provider: str | None = None
    model: str | None = None

    @property
    def text(self) -> str:
        """Plain text concatenation of TextParts (skips thinking/tool)."""
        return "".join(p.text for p in self.parts if isinstance(p, TextPart))

    @property
    def tool_calls(self) -> list[ToolCallPart]:
        return [p for p in self.parts if isinstance(p, ToolCallPart)]


# ---------------------------------------------------------------------------
# Streaming chunks
# ---------------------------------------------------------------------------

class ChunkKind(str, Enum):
    TEXT = "text"
    THINKING = "thinking"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_CALL_DONE = "tool_call_done"
    USAGE = "usage"
    STOP = "stop"
    ERROR = "error"


@dataclass(slots=True)
class StreamChunk:
    """Single delta from a streaming completion.

    Different chunk kinds populate different fields. Keep all optional and
    let the consumer pattern-match on `kind`.
    """
    kind: ChunkKind
    text: str = ""
    thinking: str = ""
    thinking_signature: str | None = None
    tool_call_id: str | None = None
    tool_call_name: str | None = None
    tool_call_args_delta: str | None = None     # raw JSON string fragment
    tool_call_args: dict[str, Any] | None = None
    usage: Usage | None = None
    stop_reason: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Usage / accounting
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def merge(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )


# ---------------------------------------------------------------------------
# Tool calls (resolved, post-stream)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ToolResult:
    tool_call_id: str
    output: str
    is_error: bool = False
    duration_ms: float = 0.0


# ---------------------------------------------------------------------------
# Turn — one round of (user → assistant → tools → assistant ...)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Turn:
    """A single conversation turn from user input to a stop_reason='end'."""
    id: str = field(default_factory=_id)
    messages: list[Message] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    stop_reason: str | None = None

    def append(self, msg: Message) -> None:
        self.messages.append(msg)


# ---------------------------------------------------------------------------
# Stop reasons (normalized)
# ---------------------------------------------------------------------------

class StopReason(str, Enum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"
    INTERRUPTED = "interrupted"
