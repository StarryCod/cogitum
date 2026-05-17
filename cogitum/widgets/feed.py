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
from typing import ClassVar, Literal

from rich.markdown import Markdown
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static
from textual.screen import ModalScreen
from textual.widgets import TextArea
from textual.containers import Vertical
from textual.binding import Binding

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
    BG,
    BG_SOFT,
)

EntryKind = Literal["user", "agent", "error", "system"]


# ── Message Viewer (copy popup) ──────────────────────────────────────────────

class MessageViewer(ModalScreen):
    """Read-only popup for selecting and copying message text."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss", "close"),
        Binding("ctrl+c", "copy_selected", "copy", priority=True),
        Binding("ctrl+a", "select_all", "select all", priority=True),
    ]

    DEFAULT_CSS = f"""
    MessageViewer {{
        align: center middle;
    }}
    MessageViewer > Vertical {{
        width: 90%;
        height: 80%;
        background: {BG_SOFT};
        border: solid {GOLD_DIM};
        padding: 1 2;
    }}
    MessageViewer > Vertical > Static {{
        height: 1;
        color: {GOLD};
        text-style: bold;
        margin-bottom: 1;
    }}
    MessageViewer > Vertical > TextArea {{
        height: 1fr;
        background: {BG};
        color: {TXT};
    }}
    MessageViewer > Vertical > #viewer-hint {{
        height: 1;
        color: {TXT_DIM};
        text-style: italic;
        margin-top: 1;
        text-align: center;
    }}
    """

    def __init__(self, text: str, **kw) -> None:
        self._text = text
        super().__init__(**kw)

    def compose(self):
        with Vertical():
            yield Static("MESSAGE VIEWER")
            yield TextArea(self._text, read_only=True, id="viewer-area")
            yield Static("select text + Ctrl+C to copy · Ctrl+A select all · Esc close", id="viewer-hint")

    def action_copy_selected(self) -> None:
        area = self.query_one("#viewer-area", TextArea)
        selected = area.selected_text
        if selected:
            self.app.copy_to_clipboard(selected)
            self.app.notify("Copied!", timeout=1.5)
        else:
            # Copy all if nothing selected
            self.app.copy_to_clipboard(self._text)
            self.app.notify("Copied all!", timeout=1.5)

    def action_select_all(self) -> None:
        area = self.query_one("#viewer-area", TextArea)
        area.select_all()


# ── Feed entries ─────────────────────────────────────────────────────────────


@dataclass
class FeedEntry:
    kind: EntryKind
    text: str
    meta: str = ""


# ── User bubble ─────────────────────────────────────────────────────────────

class UserBubble(Static):
    DEFAULT_CLASSES = "feed-entry feed-user"

    def __init__(self, text: str, meta: str = "", **kw) -> None:
        self._raw_text = text
        super().__init__(self._build(text, meta), **kw)

    def on_click(self, event) -> None:
        """Open text viewer popup on click."""
        if self._raw_text.strip():
            self.app.push_screen(MessageViewer(self._raw_text))

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
        if not self._text:
            return header
        md = Markdown(self._text, code_theme="monokai")
        return Group(header, md)

    @staticmethod
    def _markdown_to_text(md_source: str, width: int = 120) -> Text:
        """Convert markdown to Rich Text preserving styles."""
        from rich.console import Console
        console = Console(width=width, highlight=False)
        md = Markdown(md_source, code_theme="monokai")
        segments = list(console.render(md))
        text = Text()
        for seg in segments:
            if seg.text:
                text.append(seg.text, style=seg.style)
        # Strip trailing whitespace
        while text.plain.endswith("\n"):
            text.right_crop(1)
        return text

    def _do_render(self) -> None:
        """Perform the actual markdown render and reset debounce state."""
        self._render_pending = False
        self._render_timer = None
        self.update(self._build_markdown())
        if self.parent:
            self.parent.scroll_end(animate=False)

    def append_delta(self, delta: str) -> None:
        """Append streaming text delta and schedule a debounced re-render.

        The first delta renders immediately so single-character responses
        (or very short ones) don't stay invisible until the debounce fires.
        """
        was_empty = not self._text
        self._text += delta
        if was_empty:
            # First delta — render right away so the user sees something
            self._do_render()
            return
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

    def on_click(self, event) -> None:
        """Open text viewer popup on click."""
        if self._text.strip():
            self.app.push_screen(MessageViewer(self._text))


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
    """Compact card shown when agent invokes a tool.
    Special visual treatment for heavy tools (delegate_task, cogit, experts)."""
    DEFAULT_CLASSES = "feed-entry feed-agent"

    # Tools that get special card styling
    _SPECIAL_TOOLS: ClassVar[dict[str, tuple[str, str, str]]] = {
        "delegate_task": ("⚔", "DELEGATE", "Spawning sub-agents…"),
        "cogit": ("◆", "COGIT", "Checkpoint…"),
        "memory": ("◈", "MEMORY", "Updating memory…"),
        "skills": ("◉", "SKILLS", ""),
        "terminal": ("▸", "TERMINAL", ""),
        "fetch_url": ("⊕", "FETCH", ""),
        "web_search": ("⊛", "SEARCH", "Searching…"),
        "browser": ("⊙", "BROWSER", ""),
    }

    def __init__(self, tool_name: str, arguments: dict, call_id: str = "", preparing: bool = False, **kw) -> None:
        self._tool_name = tool_name
        self._arguments = arguments
        self._call_id = call_id
        self._result: str | None = None
        self._error = False
        self._preparing = preparing
        super().__init__(self._build(), **kw)

    def _build(self) -> Text:
        # MCP tools: render a generic card with the [MCP server.tool] header
        if self._tool_name.startswith("mcp_"):
            return self._build_mcp()
        special = self._SPECIAL_TOOLS.get(self._tool_name)
        if special:
            return self._build_special(special)
        return self._build_generic()

    def _build_mcp(self) -> Text:
        """Render a Cogitum-styled card for an MCP tool call."""
        # Pull `server` and `tool` out of the registered name
        # (mcp_<server>_<tool>). Fall back to raw if parsing fails.
        server = "mcp"
        bare_tool = self._tool_name
        try:
            from cogitum.core.mcp.discovery import parse_tool_id
            parsed = parse_tool_id(self._tool_name)
            if parsed:
                server, bare_tool = parsed
        except Exception:
            pass

        out = Text()
        out.append("  ┌─", style=COPPER)
        out.append(" ⬢ ", style=f"bold {GOLD_HI}")
        out.append("MCP", style=f"bold {GOLD}")
        out.append(f"  {server}", style=GOLD_HI)
        out.append(f".{bare_tool}", style=GOLD)
        if self._call_id:
            out.append(f"  {self._call_id[:8]}", style=MUTED)
        out.append("\n")

        # Args (compact)
        self._render_args(out)

        # Result / status
        if self._result is not None:
            self._render_result(out)
        elif self._preparing:
            out.append("  │ ", style=COPPER)
            out.append("preparing…", style=MUTED)
            out.append("\n")
        else:
            out.append("  │ ", style=COPPER)
            out.append("⏳ running…", style=MUTED)
            out.append("\n")

        out.append("  └─", style=COPPER)
        if out.plain.endswith("\n"):
            out.right_crop(1)
        return out

    def _build_special(self, spec: tuple) -> Text:
        glyph, label, status_text = spec
        out = Text()

        # Top border
        out.append("  ┌─", style=COPPER)
        out.append(f" {glyph} ", style=f"bold {GOLD_HI}")
        out.append(label, style=f"bold {GOLD}")

        # Contextual subtitle
        subtitle = self._get_subtitle()
        if subtitle:
            out.append(f"  {subtitle}", style=TXT_DIM)
        out.append("\n")

        # Body — depends on tool
        if self._tool_name == "delegate_task":
            self._render_delegate_body(out)
        elif self._tool_name == "cogit":
            self._render_cogit_body(out)
        elif self._tool_name == "terminal":
            self._render_terminal_body(out)
        elif self._tool_name == "memory":
            self._render_memory_body(out)
        elif self._tool_name == "skills":
            self._render_skills_body(out)
        else:
            self._render_args(out)

        # Result / status
        if self._result is not None:
            self._render_result(out)
        elif self._preparing:
            out.append("  │ ", style=COPPER)
            out.append(status_text or "preparing…", style=MUTED)
            out.append("\n")
        else:
            out.append("  │ ", style=COPPER)
            out.append("⏳ running…", style=MUTED)
            out.append("\n")

        # Bottom border
        out.append("  └─", style=COPPER)

        if out.plain.endswith("\n"):
            out.right_crop(1)
        return out

    def _build_generic(self) -> Text:
        out = Text()
        out.append("  ◇ ", style=BRONZE)
        out.append(self._tool_name, style=f"bold {GOLD}")
        if self._call_id:
            out.append(f"  {self._call_id[:8]}", style=MUTED)
        out.append("\n")
        # args
        if self._preparing and not self._arguments:
            out.append("    preparing…\n", style=MUTED)
        else:
            self._render_args(out)
        # result
        if self._result is not None:
            self._render_result(out)
        else:
            out.append("    ⏳ running…\n", style=MUTED)
        if out.plain.endswith("\n"):
            out.right_crop(1)
        return out

    def _get_subtitle(self) -> str:
        if self._tool_name == "delegate_task":
            mode = self._arguments.get("mode", "workers")
            tasks = self._arguments.get("tasks", [])
            if isinstance(tasks, list):
                return f"{mode} · {len(tasks)} task(s)"
            return mode
        elif self._tool_name == "cogit":
            action = self._arguments.get("action", "")
            label = self._arguments.get("label", "")
            return f"{action}: {label}" if label else action
        elif self._tool_name == "terminal":
            cmd = self._arguments.get("command", "")
            return cmd[:50] + "…" if len(cmd) > 50 else cmd
        elif self._tool_name == "memory":
            return self._arguments.get("action", "")
        elif self._tool_name == "skills":
            action = self._arguments.get("action", "")
            name = self._arguments.get("name", "")
            return f"{action}: {name}" if name else action
        elif self._tool_name == "web_search":
            query = self._arguments.get("query", "")
            return query[:50] + "…" if len(query) > 50 else query
        elif self._tool_name == "browser":
            action = self._arguments.get("action", "")
            url = self._arguments.get("url", "")
            selector = self._arguments.get("selector", "")
            if action == "open" and url:
                return f"open: {url[:45]}{'…' if len(url) > 45 else ''}"
            elif action in ("click", "type") and selector:
                return f"{action}: {selector[:40]}"
            return action
        return ""

    def _render_delegate_body(self, out: Text) -> None:
        tasks = self._arguments.get("tasks", [])
        mode = self._arguments.get("mode", "workers")
        if isinstance(tasks, list):
            for i, t in enumerate(tasks[:6]):
                goal = t.get("goal", "") if isinstance(t, dict) else str(t)
                goal = goal[:65] + "…" if len(goal) > 65 else goal
                out.append("  │ ", style=COPPER)
                out.append(f"  {i+1}. ", style=GOLD_DIM)
                out.append(goal + "\n", style=TXT)
            if len(tasks) > 6:
                out.append("  │ ", style=COPPER)
                out.append(f"  … +{len(tasks) - 6} more\n", style=MUTED)
        elif mode == "experts":
            content = self._arguments.get("content", "")
            preview = content[:80] + "…" if len(content) > 80 else content
            out.append("  │ ", style=COPPER)
            out.append("  reviewing: ", style=TXT_DIM)
            out.append(preview + "\n", style=TXT)

    def _render_cogit_body(self, out: Text) -> None:
        action = self._arguments.get("action", "")
        label = self._arguments.get("label", "")
        if action == "save":
            out.append("  │ ", style=COPPER)
            out.append("  saving checkpoint", style=TXT_DIM)
            if label:
                out.append(f" [{label}]", style=GOLD_DIM)
            out.append("\n")
        elif action == "restore":
            out.append("  │ ", style=COPPER)
            out.append("  restoring to ", style=TXT_DIM)
            out.append(label or "latest", style=GOLD_DIM)
            out.append("\n")
        elif action == "list":
            out.append("  │ ", style=COPPER)
            out.append("  listing checkpoints\n", style=TXT_DIM)

    def _render_terminal_body(self, out: Text) -> None:
        cmd = self._arguments.get("command", "")
        if cmd:
            # Show command with shell-like styling
            lines = cmd.splitlines()
            for line in lines[:3]:
                out.append("  │ ", style=COPPER)
                out.append("  $ ", style=GOLD_DIM)
                display = line[:70] + "…" if len(line) > 70 else line
                out.append(display + "\n", style=TXT)
            if len(lines) > 3:
                out.append("  │ ", style=COPPER)
                out.append(f"  … +{len(lines) - 3} lines\n", style=MUTED)

    def _render_memory_body(self, out: Text) -> None:
        action = self._arguments.get("action", "")
        content = self._arguments.get("content", "")
        target = self._arguments.get("target", "memory")
        out.append("  │ ", style=COPPER)
        out.append(f"  [{target}] ", style=GOLD_DIM)
        if action == "add":
            preview = content[:60] + "…" if len(content) > 60 else content
            out.append(f"+ {preview}\n", style=TXT)
        elif action == "replace":
            out.append("updating entry\n", style=TXT)
        elif action == "remove":
            out.append("removing entry\n", style=TXT)

    def _render_skills_body(self, out: Text) -> None:
        action = self._arguments.get("action", "")
        name = self._arguments.get("name", "")
        if action == "read" and name:
            out.append("  │ ", style=COPPER)
            out.append(f"  loading: {name}\n", style=TXT)
        elif action == "write" and name:
            out.append("  │ ", style=COPPER)
            out.append(f"  saving: {name}\n", style=TXT)
        elif action == "list":
            cat = self._arguments.get("category", "")
            out.append("  │ ", style=COPPER)
            if cat:
                out.append(f"  browsing [{cat}]\n", style=TXT)
            else:
                out.append("  browsing all\n", style=TXT)

    def _render_args(self, out: Text) -> None:
        """Generic args rendering."""
        for k, v in list(self._arguments.items())[:4]:
            val = str(v)
            if len(val) > 60:
                val = val[:57] + "…"
            if self._tool_name in self._SPECIAL_TOOLS:
                out.append("  │ ", style=COPPER)
            out.append(f"    {k}: ", style=TXT_DIM)
            out.append(val + "\n", style=TXT)
        if len(self._arguments) > 4:
            if self._tool_name in self._SPECIAL_TOOLS:
                out.append("  │ ", style=COPPER)
            out.append(f"    … +{len(self._arguments) - 4} more\n", style=MUTED)

    def _render_result(self, out: Text) -> None:
        """Render tool result.

        We show up to ~12 lines of preview (was 4) — enough to see most
        terminal/tool outputs without forcing the user to dig into the
        message viewer for trivial calls. Truncated lines get a hint with
        the full count so it's obvious there's more.
        """
        color = RUST if self._error else COPPER
        glyph = "✗" if self._error else "✓"
        if self._tool_name in self._SPECIAL_TOOLS:
            out.append("  │ ", style=COPPER)
            out.append(f"  {glyph} ", style=f"bold {color}")
        else:
            out.append(f"    {glyph} ", style=f"bold {color}")

        # Compact result preview
        result_lines = self._result.splitlines() if self._result else []
        max_preview = 12  # was 4 — too aggressive, hid useful output
        if not result_lines:
            out.append("done\n", style=color)
        elif len(result_lines) == 1:
            line = result_lines[0][:80]
            out.append(line + "\n", style=TXT_DIM)
        else:
            out.append(result_lines[0][:80] + "\n", style=TXT_DIM)
            for line in result_lines[1:max_preview]:
                prefix = "  │ " if self._tool_name in self._SPECIAL_TOOLS else "    "
                out.append(prefix, style=COPPER)
                out.append(f"  {line[:78]}\n", style=TXT_DIM)
            if len(result_lines) > max_preview:
                prefix = "  │ " if self._tool_name in self._SPECIAL_TOOLS else "    "
                out.append(prefix, style=COPPER)
                out.append(
                    f"  … +{len(result_lines) - max_preview} more line(s)\n",
                    style=MUTED,
                )

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

    def is_pending(self) -> bool:
        """True if this card has not yet received a result."""
        return self._result is None

    def mark_interrupted(self, reason: str = "(interrupted — no result received)") -> None:
        """Force-finalize a pending card with an error state.

        Idempotent: cards that already have a result are left alone, so this
        is safe to call from a final-sweep at end of drain even if a result
        arrived just before.
        """
        if self._result is None:
            self.set_result(reason, error=True)


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

    FRAMES: ClassVar[list[str]] = [
        "◇ · · ·",
        "· ◆ · ·",
        "· · ◆ ·",
        "· · · ◆",
        "· · ◆ ·",
        "· ◆ · ·",
    ]

    # Friendly status messages cycled during retry
    _RETRY_LABELS: ClassVar[list[str]] = [
        "thinking…",
        "connecting…",
        "preparing…",
        "warming up…",
        "almost ready…",
    ]

    def __init__(self, **kw) -> None:
        super().__init__("", **kw)
        self._frame = 0
        self._timer = None
        self._status: str = "thinking…"

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.2, self._tick)
        self._render_frame()

    def set_status(self, status: str) -> None:
        """Update the displayed status text."""
        self._status = status
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
        out.append(f"  {self._status}", style=f"italic {TXT_DIM}")
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
