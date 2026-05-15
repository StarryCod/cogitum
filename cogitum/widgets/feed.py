"""Conversation feed — bubbles for user, plain text for agent.

Layout rules:
  · YOU       → bubble (warm surface, gold left bar, padded)
  · AGENT     → no bubble, Rich Markdown on canvas, streaming-capable
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

from rich.markdown import Markdown
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
    """Streaming agent response. Call .append_delta() to add text live.
    Renders Rich Markdown in real-time (debounced) during streaming."""
    DEFAULT_CLASSES = "feed-entry feed-agent"

    # Debounce interval in seconds for markdown re-renders during streaming
    _RENDER_INTERVAL = 0.1

    def __init__(self, text: str = "", meta: str = "", **kw) -> None:
        self._text = text
        self._meta = meta
        self._streaming = True
        self._render_pending = False
        self._render_timer = None
        super().__init__(self._build_markdown(), **kw)

    def _header(self) -> Text:
        out = Text()
        out.append("AGENT", style=f"bold {GOLD}")
        if self._meta:
            out.append("   ")
            out.append(self._meta, style=f"italic {TXT_DIM}")
        out.append("   ")
        out.append(datetime.now().strftime("%H:%M"), style=GOLD_DIM)
        return out

    def _build_markdown(self):
        """Render accumulated text as Rich Markdown with header."""
        from rich.console import Group
        header = self._header()
        if not self._text.strip():
            return header
        md = Markdown(self._text, code_theme="monokai")
        return Group(header, md)

    def _do_render(self) -> None:
        """Perform the actual markdown render and reset debounce state."""
        self._render_pending = False
        self._render_timer = None
        self.update(self._build_markdown())
        if self.parent:
            self.parent.scroll_end(animate=False)

    def append_delta(self, delta: str) -> None:
        """Append streaming text delta and schedule a debounced re-render."""
        self._text += delta
        if not self._render_pending:
            self._render_pending = True
            self._render_timer = self.set_timer(
                self._RENDER_INTERVAL, self._do_render
            )

    def finish_streaming(self) -> None:
        """Final render — cancel any pending debounce and render immediately."""
        self._streaming = False
        if self._render_timer is not None:
            self._render_timer.stop()
            self._render_timer = None
            self._render_pending = False
        self.update(self._build_markdown())


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

    def __init__(self, tool_name: str, arguments: dict, call_id: str = "", preparing: bool = False, **kw) -> None:
        self._tool_name = tool_name
        self._arguments = arguments
        self._call_id = call_id
        self._result: str | None = None
        self._error = False
        self._preparing = preparing
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
        if self._preparing and not self._arguments:
            out.append("    preparing…\n", style=MUTED)
        else:
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

    def set_arguments(self, arguments: dict) -> None:
        """Update arguments (e.g. after preliminary card gets full args)."""
        self._arguments = arguments
        self._preparing = False
        self.update(self._build())

    def set_result(self, result: str, error: bool = False) -> None:
        self._result = result
        self._error = error
        self._preparing = False
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


# ── Waiting animation (TTFS) ─────────────────────────────────────────────────

class WaitingIndicator(Static):
    """Animated waiting indicator while model is thinking (TTFS)."""
    DEFAULT_CLASSES = "feed-entry feed-waiting"

    FRAMES = [
        "◇ · · ·",
        "· ◆ · ·",
        "· · ◆ ·",
        "· · · ◆",
        "· · ◆ ·",
        "· ◆ · ·",
    ]

    def __init__(self, **kw) -> None:
        super().__init__("", **kw)
        self._frame = 0
        self._timer = None

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.2, self._tick)
        self._render_frame()

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(self.FRAMES)
        self._render_frame()

    def _render_frame(self) -> None:
        out = Text()
        out.append("  ", style=GOLD_DIM)
        frame = self.FRAMES[self._frame]
        for ch in frame:
            if ch == "◆":
                out.append(ch, style=f"bold {GOLD_HI}")
            elif ch == "◇":
                out.append(ch, style=GOLD)
            else:
                out.append(ch, style=MUTED)
        out.append("  thinking…", style=f"italic {TXT_DIM}")
        self.update(out)

    def stop(self) -> None:
        if self._timer:
            self._timer.stop()
            self._timer = None
        self.remove()


# ── Queued message (pending while agent works) ───────────────────────────────

class QueuedMessage(Static):
    """Grey indicator showing a queued message that will be sent next."""
    DEFAULT_CLASSES = "feed-entry feed-queued"

    def __init__(self, text: str, **kw) -> None:
        self._text = text
        super().__init__(self._build(text), **kw)

    @property
    def queued_text(self) -> str:
        return self._text

    @staticmethod
    def _build(text: str) -> Text:
        out = Text()
        out.append("  ⏳ ", style=MUTED)
        out.append("queued", style=f"italic {MUTED}")
        out.append("  ")
        display = text[:80] + ("…" if len(text) > 80 else "")
        out.append(display, style=f"italic {TXT_DIM}")
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

    def append_tool_call(self, tool_name: str, arguments: dict, call_id: str = "", preparing: bool = False) -> ToolCallCard:
        card = ToolCallCard(tool_name, arguments, call_id, preparing=preparing)
        self.mount(card)
        self.scroll_end(animate=False)
        return card

    def append_waiting(self) -> "WaitingIndicator":
        indicator = WaitingIndicator()
        self.mount(indicator)
        self.scroll_end(animate=False)
        return indicator

    def append_queued(self, text: str) -> QueuedMessage:
        msg = QueuedMessage(text)
        self.mount(msg)
        self.scroll_end(animate=False)
        return msg

    def pop_queued(self) -> str | None:
        """Remove the last queued message and return its text (for editing)."""
        queued = list(self.query("QueuedMessage"))
        if queued:
            last = queued[-1]
            text = last.queued_text
            last.remove()
            return text
        return None

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
