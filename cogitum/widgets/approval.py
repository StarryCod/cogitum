"""
cogitum.widgets.approval
~~~~~~~~~~~~~~~~~~~~~~~~~
Tool approval widget — shows when agent wants to execute medium/danger commands.

User navigates with ↑/↓ arrows and confirms with Enter.
Options: ✅ Allow, ❌ Deny, ✏️ Edit (for advanced users).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from ..design import GOLD, GOLD_DIM, BRONZE, TXT, TXT_DIM, RUST, BG


# ── Danger level colors ───────────────────────────────────────────────────────

_DANGER_COLORS = {
    "medium": BRONZE,
    "danger": RUST,
}

_DANGER_ICONS = {
    "medium": "🟡",
    "danger": "🔴",
}


class ApprovalWidget(Widget):
    """Inline approval prompt for tool calls.
    
    Shows tool name, arguments summary, danger level.
    User picks: Allow / Deny with arrow keys + Enter.
    """

    DEFAULT_CSS = """
    ApprovalWidget {
        height: auto;
        max-height: 8;
        margin: 0 1;
        padding: 1 2;
        border: solid $warning;
        background: $surface;
    }
    ApprovalWidget.danger {
        border: solid $error;
    }
    """

    # ── Messages ──────────────────────────────────────────────────────────────

    @dataclass
    class Decided(Message):
        """User made a decision."""
        call_id: str
        decision: str  # "approve" or "reject"

    # ── State ─────────────────────────────────────────────────────────────────

    selected: reactive[int] = reactive(0)

    def __init__(
        self,
        tool_name: str,
        arguments: dict,
        call_id: str,
        danger_level: str,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.tool_name = tool_name
        self.arguments = arguments
        self.call_id = call_id
        self.danger_level = danger_level
        self._options = ["✅ Allow", "❌ Deny"]
        if danger_level == "danger":
            self.add_class("danger")

    def compose(self) -> ComposeResult:
        icon = _DANGER_ICONS.get(self.danger_level, "🟡")
        color = _DANGER_COLORS.get(self.danger_level, BRONZE)

        # Build description
        desc = self._describe_tool()

        yield Static(
            f"{icon} [bold {color}]Approval required[/] ({self.danger_level})\n"
            f"[bold]{self.tool_name}[/]: {desc}",
            id="approval-header",
        )
        yield Static(self._render_options(), id="approval-options")

    def _describe_tool(self) -> str:
        """Generate compact description of what the tool will do."""
        args = self.arguments
        if self.tool_name == "terminal":
            cmd = args.get("command", "")
            mode = args.get("mode", "normal")
            if mode == "background":
                return f"[bg] {cmd[:80]}"
            return cmd[:100]
        elif self.tool_name == "write_file":
            path = args.get("path", "")
            content = args.get("content", "")
            return f"write {len(content)} chars → {path}"
        elif self.tool_name == "edit_file":
            return f"edit {args.get('path', '')}"
        elif self.tool_name == "cogit":
            return f"{args.get('action', '')} {args.get('label', '')}"
        return str(args)[:80]

    def _render_options(self) -> str:
        """Render option list with selection indicator."""
        parts = []
        for i, opt in enumerate(self._options):
            if i == self.selected:
                parts.append(f"  [bold {GOLD}]▸ {opt}[/]")
            else:
                parts.append(f"    [{TXT_DIM}]{opt}[/]")
        return "\n".join(parts)

    def watch_selected(self) -> None:
        """Update display when selection changes."""
        try:
            options_widget = self.query_one("#approval-options", Static)
            options_widget.update(self._render_options())
        except Exception:
            pass  # Widget not yet composed

    def on_key(self, event: events.Key) -> None:
        """Handle arrow keys and Enter."""
        if event.key in ("up", "k"):
            self.selected = max(0, self.selected - 1)
            event.stop()
        elif event.key in ("down", "j"):
            self.selected = min(len(self._options) - 1, self.selected + 1)
            event.stop()
        elif event.key == "enter":
            decision = "approve" if self.selected == 0 else "reject"
            self.post_message(self.Decided(call_id=self.call_id, decision=decision))
            self.remove()
            event.stop()
        elif event.key == "escape":
            # Escape = deny
            self.post_message(self.Decided(call_id=self.call_id, decision="reject"))
            self.remove()
            event.stop()
        elif event.key in ("y", "Y"):
            # Quick approve
            self.post_message(self.Decided(call_id=self.call_id, decision="approve"))
            self.remove()
            event.stop()
        elif event.key in ("n", "N"):
            # Quick deny
            self.post_message(self.Decided(call_id=self.call_id, decision="reject"))
            self.remove()
            event.stop()
