"""Conversation feed — bubbles for user, plain text for agent.

Layout rules:
  · YOU       → bubble (warm surface, gold left bar, padded)
  · AGENT     → no bubble, plain text on canvas, streaming-capable
  · THINKING  → dim italic block, collapsible
  · TOOL CALL → compact card: name + args
  · TOOL RES  → compact card: result (truncated)
  · ERROR     → bubble with rust bar
  · SYSTEM    → dim italic line, no chrome
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static

from ..design import (
    BRONZE,
    COPPER,
    GOLD,
    GOLD_DIM,
    GOLD_HI,
    MUTED,
    RUST,
    TXT,
    TXT_DIM,
)

EntryKind = Literal["user", "agent", "error", "system"]


@dataclass
class FeedEntry:
    kind: EntryKind
    text: str
    meta: str = ""


# ── User bubble ─────────────────────────────────────────────────────────────

class UserBubble(Static):
    DEFAULT_CLASSES = "feed-entry feed-user"

    def __init__(self, text: str, meta: str = "", **kw) -> None:
        super().__init__(self._build(text, meta), **kw)

    @staticmethod
    def _build(text: str, meta: str) -> Text:
        out = Text()
        out.append("YOU", style=f"bold {GOLD_HI}")
        if meta:
            out.append("   ")
            out.append(meta, style=f"italic {GOLD_DIM}")
        out.append("   ")
        out.append(datetime.now().strftime("%H:%M"), style=GOLD_DIM)
        out.append("\n")
        for line in text.splitlines() or [""]:
            out.append(line + "\n", style=TXT)
        if out.plain.endswith("\n"):
            out.right_crop(1)
        return out


# ── Agent block (streaming-capable) ─────────────────────────────────────────

class AgentBlock(Static):
    """Streaming agent response. Call .append_delta() to add text live."""
    DEFAULT_CLASSES = "feed-entry feed-agent"

    def __init__(self, text: str = "", meta: str = "", **kw) -> None:
        self._text = text
        self._meta = meta
        super().__init__(self._build(), **kw)

    def _build(self) -> Text:
        out = Text()
        out.append("AGENT", style=f"bold {GOLD}")
        if self._meta:
            out.append("   ")
            out.append(self._meta, style=f"italic {TXT_DIM}")
        out.append("   ")
        out.append(datetime.now().strftime("%H:%M"), style=GOLD_DIM)
        out.append("\n")
        for line in self._text.splitlines() or [""]:
            out.append(line + "\n", style=TXT)
        result = out
        if result.plain.endswith("\n"):
            result.right_crop(1)
        return result

    def append_delta(self, delta: str) -> None:
        """Append streaming text delta and refresh."""
        self._text += delta
        self.update(self._build())
        self.parent and self.parent.scroll_end(animate=False)  # type: ignore[union-attr]


# ── Thinking block ───────────────────────────────────────────────────────────

class ThinkingBlock(Static):
    """Collapsible reasoning / chain-of-thought block."""
    DEFAULT_CLASSES = "feed-entry feed-agent"

    def __init__(self, **kw) -> None:
        self._text = ""
        self._done = False
        super().__init__(self._build(), **kw)

    def _build(self) -> Text:
        out = Text()
        label = "THINKING" if not self._done else "THOUGHT"
        out.append(f"  {label}", style=f"italic {MUTED}")
        out.append("\n")
        # show last 3 lines only to keep feed compact
        lines = self._text.splitlines()
        visible = lines[-3:] if len(lines) > 3 else lines
        for line in visible:
            out.append(f"  {line}\n", style=f"italic {TXT_DIM}")
        if out.plain.endswith("\n"):
            out.right_crop(1)
        return out

    def append_delta(self, delta: str) -> None:
        self._text += delta
        self.update(self._build())

    def finish(self) -> None:
        self._done = True
        self.update(self._build())


# ── Tool call card ───────────────────────────────────────────────────────────

class ToolCallCard(Static):
    """Compact card shown when agent invokes a tool."""
    DEFAULT_CLASSES = "feed-entry feed-agent"

    def __init__(self, tool_name: str, arguments: dict, call_id: str = "", **kw) -> None:
        self._tool_name = tool_name
        self._arguments = arguments
        self._call_id = call_id
        self._result: str | None = None
        self._error = False
        super().__init__(self._build(), **kw)

    def _build(self) -> Text:
        out = Text()
        # header
        out.append("  ⚙ ", style=BRONZE)
        out.append(self._tool_name, style=f"bold {GOLD}")
        if self._call_id:
            out.append(f"  {self._call_id[:8]}", style=MUTED)
        out.append("\n")
        # args (compact, one line per key)
        for k, v in list(self._arguments.items())[:4]:
            val = str(v)
            if len(val) > 60:
                val = val[:57] + "…"
            out.append(f"    {k}: ", style=TXT_DIM)
            out.append(val + "\n", style=TXT)
        if len(self._arguments) > 4:
            out.append(f"    … +{len(self._arguments) - 4} more args\n", style=MUTED)
        # result
        if self._result is not None:
            color = RUST if self._error else COPPER
            label = "  ✗ error" if self._error else "  ✓ result"
            out.append(label + "\n", style=f"bold {color}")
            for line in self._result.splitlines()[:5]:
                out.append(f"    {line}\n", style=TXT_DIM)
            if len(self._result.splitlines()) > 5:
                out.append("    …\n", style=MUTED)
        else:
            out.append("  ⏳ running…\n", style=MUTED)
        if out.plain.endswith("\n"):
            out.right_crop(1)
        return out

    def set_result(self, result: str, error: bool = False) -> None:
        self._result = result
        self._error = error
        self.update(self._build())


# ── Error bubble ────────────────────────────────────────────────────────────

class ErrorBlock(Static):
    DEFAULT_CLASSES = "feed-entry feed-error"

    def __init__(self, text: str, meta: str = "", **kw) -> None:
        super().__init__(self._build(text, meta), **kw)

    @staticmethod
    def _build(text: str, meta: str) -> Text:
        out = Text()
        out.append("ERROR", style=f"bold {RUST}")
        if meta:
            out.append("   ")
            out.append(meta, style=f"italic {TXT_DIM}")
        out.append("\n")
        for line in text.splitlines() or [""]:
            out.append(line + "\n", style=RUST)
        if out.plain.endswith("\n"):
            out.right_crop(1)
        return out


# ── System line ─────────────────────────────────────────────────────────────

class SystemLine(Static):
    DEFAULT_CLASSES = "feed-entry feed-system"

    def __init__(self, text: str, meta: str = "", **kw) -> None:
        super().__init__(self._build(text, meta), **kw)

    @staticmethod
    def _build(text: str, meta: str) -> Text:
        out = Text(style=f"italic {MUTED}")
        out.append(f"··· {text}")
        if meta:
            out.append(f"   ({meta})", style=MUTED)
        return out


# ── Feed container ──────────────────────────────────────────────────────────

class Feed(VerticalScroll):
    """Append-only conversation feed with streaming support."""

    def append_user(self, text: str, meta: str = "") -> None:
        self.mount(UserBubble(text, meta))
        self.scroll_end(animate=False)

    def append_agent(self, text: str = "", meta: str = "") -> AgentBlock:
        block = AgentBlock(text, meta)
        self.mount(block)
        self.scroll_end(animate=False)
        return block

    def append_thinking(self) -> ThinkingBlock:
        block = ThinkingBlock()
        self.mount(block)
        self.scroll_end(animate=False)
        return block

    def append_tool_call(self, tool_name: str, arguments: dict, call_id: str = "") -> ToolCallCard:
        card = ToolCallCard(tool_name, arguments, call_id)
        self.mount(card)
        self.scroll_end(animate=False)
        return card

    def append_error(self, text: str, meta: str = "") -> None:
        self.mount(ErrorBlock(text, meta))
        self.scroll_end(animate=False)

    def append_system(self, text: str, meta: str = "") -> None:
        self.mount(SystemLine(text, meta))
        self.scroll_end(animate=False)

    def append_card(self, widget) -> None:
        self.mount(widget)
        self.scroll_end(animate=False)

    def clear(self) -> None:
        for child in list(self.children):
            child.remove()

    def append(self, kind: str, text: str, meta: str = "") -> None:
        if kind == "user":    self.append_user(text, meta)
        elif kind == "agent": self.append_agent(text, meta)
        elif kind == "error": self.append_error(text, meta)
        elif kind == "system": self.append_system(text, meta)
