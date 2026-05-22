"""
cogitum.widgets.approval
~~~~~~~~~~~~~~~~~~~~~~~~~
Tool approval widget — shows when agent wants to execute medium/danger commands.

User navigates with ↑/↓ arrows and confirms with Enter.
40K-styled glyphs (no consumer emoji): ◈ Sanction · ✕ Forbid.
"""
from __future__ import annotations

from typing import ClassVar

from dataclasses import dataclass

from textual import events
from textual.app import ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static
from rich.text import Text

from ..design import (
    GOLD,
    GOLD_HI,
    GOLD_DIM,
    BRONZE,
    COPPER,
    TXT_DIM,
    RUST,
    SURFACE,
)
import logging

log = logging.getLogger(__name__)


# ── Danger level glyphs (Imperial Fists / 40K aesthetic) ─────────────────────

_DANGER_COLORS = {
    "low": BRONZE,
    "medium": COPPER,
    "danger": RUST,
}

# No emoji. Geometric runes only.
_DANGER_RUNES = {
    "low": "◇",       # hollow diamond — minor
    "medium": "◈",    # diamond with mark — caution
    "danger": "▲",    # triangle — alert
}

# Action glyphs
_RUNE_ALLOW = "◈"   # sanctioned
_RUNE_DENY = "✕"    # forbidden


class ApprovalWidget(Widget, can_focus=True):
    """Inline approval prompt for tool calls.

    Mounted into the feed. Steals focus on mount so arrow keys + Enter work.
    Shows tool name, arguments summary, danger level.
    User picks: Sanction / Forbid with arrow keys + Enter, or [Y]/[N].
    """

    DEFAULT_CSS = f"""
    ApprovalWidget {{
        height: auto;
        margin: 1 1;
        padding: 1 2;
        border: tall {BRONZE};
        background: {SURFACE};
    }}
    ApprovalWidget.danger {{
        border: tall {RUST};
        background: {SURFACE};
    }}
    ApprovalWidget:focus {{
        border: tall {GOLD_HI};
    }}
    ApprovalWidget #approval-header {{
        height: auto;
        margin-bottom: 1;
    }}
    ApprovalWidget #approval-options {{
        height: auto;
    }}
    ApprovalWidget #approval-hint {{
        height: 1;
        color: {GOLD_DIM};
        margin-top: 1;
    }}
    """

    # ── Messages ──────────────────────────────────────────────────────────────

    @dataclass
    class Decided(Message):
        """User made a decision."""
        call_id: str
        decision: str  # "approve" or "reject"

    # ── State ─────────────────────────────────────────────────────────────────

    selected: reactive[int] = reactive(0)

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("up", "move_up", "up"),
        ("down", "move_down", "down"),
        ("k", "move_up", "up"),
        ("j", "move_down", "down"),
        ("enter", "confirm", "confirm"),
        ("y", "quick_approve", "approve"),
        ("n", "quick_deny", "deny"),
        ("escape", "quick_deny", "deny"),
    ]

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
        self._options = [
            (_RUNE_ALLOW, "Sanction"),
            (_RUNE_DENY, "Forbid"),
        ]
        if danger_level == "danger":
            self.add_class("danger")

    def compose(self) -> ComposeResult:
        rune = _DANGER_RUNES.get(self.danger_level, "◈")
        color = _DANGER_COLORS.get(self.danger_level, BRONZE)

        # ``_describe_tool`` returns user-controlled text — tool args
        # routinely include characters that look like Textual markup
        # (closing tags ``[/]``, attribute brackets ``[link](path)``,
        # regex literals ``[a-z]``, Windows paths). Those crashed
        # ``Static.update`` with ``MarkupError: Expected markup value``
        # because Static parses its first arg as markup unless it
        # receives a Rich ``Text`` instance.
        #
        # Fix: hand-build the header as Rich ``Text`` and append the
        # description verbatim (no markup parsing). Keeps every
        # styled fragment intact while making the user-controlled
        # piece safe to embed.
        desc = self._describe_tool()
        level_label = self.danger_level.upper()

        header = Text()
        header.append(f"{rune}  Sanction required", style=f"bold {color}")
        header.append("  ")
        header.append(f"· {level_label}", style=TXT_DIM)
        header.append("\n")
        header.append(self.tool_name, style=f"bold {GOLD_HI}")
        header.append("  ")
        header.append(desc, style=TXT_DIM)
        yield Static(header, id="approval-header")

        yield Static(self._render_options(), id="approval-options")

        hint = Text()
        hint.append("↑↓ select · enter confirm · ", style=TXT_DIM)
        hint.append("Y", style=GOLD)
        hint.append(" sanction · ", style=TXT_DIM)
        hint.append("N", style=GOLD)
        hint.append("/esc forbid", style=TXT_DIM)
        yield Static(hint, id="approval-hint")

    def on_mount(self) -> None:
        # Take focus immediately so arrow keys work without an extra click.
        self.focus()

    # H13: if the user clicks somewhere else in the TUI while we're waiting
    # for their decision, focus drifts and arrow keys / Y/N stop working.
    # Whenever this widget loses focus, reclaim it on the next tick — the
    # widget removes itself from the tree on decision, so this loop ends
    # naturally when _dispatch() is called.
    def on_blur(self, event: events.Blur) -> None:
        if not self.is_mounted:
            return
        # call_after_refresh to avoid re-entering focus during the same
        # event dispatch cycle.
        self.call_after_refresh(self.focus)

    def _describe_tool(self) -> str:
        """Generate compact description of what the tool will do."""
        args = self.arguments
        if self.tool_name.startswith("mcp_"):
            try:
                from cogitum.core.mcp.discovery import parse_tool_id
                parsed = parse_tool_id(self.tool_name)
                if parsed:
                    server, bare = parsed
                    arg_summary = ", ".join(
                        f"{k}={str(v)[:40]}" for k, v in list(args.items())[:3]
                    )
                    return f"MCP {server}.{bare}  {arg_summary}"[:140]
            except Exception:
                pass
            return f"MCP tool · {str(args)[:80]}"
        if self.tool_name == "terminal":
            cmd = args.get("command", "")
            mode = args.get("mode", args.get("kind", "normal"))
            if mode == "background":
                return f"[bg] {cmd[:80]}"
            if mode == "timed":
                t = args.get("timeout", "?")
                return f"[t={t}s] {cmd[:80]}"
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

    def _render_options(self) -> Text:
        """Build the option list as a Rich ``Text`` so it bypasses
        markup parsing entirely. The string-with-markup form was a
        latent landmine: ``rune``/``label`` are static today but if
        anyone ever feeds a brace-bearing rune through here it would
        crash at runtime exactly like the description path did."""
        out = Text()
        for i, (rune, label) in enumerate(self._options):
            if i:
                out.append("\n")
            if i == self.selected:
                out.append(f"  ▸ {rune}  {label}", style=f"bold {GOLD_HI}")
            else:
                out.append(f"    {rune}  {label}", style=TXT_DIM)
        return out

    def watch_selected(self) -> None:
        try:
            self.query_one("#approval-options", Static).update(self._render_options())
        except Exception:
            log.debug("swallowed exception", exc_info=True)

    # ── Actions (driven by BINDINGS) ──────────────────────────────────────────

    def action_move_up(self) -> None:
        self.selected = max(0, self.selected - 1)

    def action_move_down(self) -> None:
        self.selected = min(len(self._options) - 1, self.selected + 1)

    def action_confirm(self) -> None:
        decision = "approve" if self.selected == 0 else "reject"
        self._dispatch(decision)

    def action_quick_approve(self) -> None:
        self._dispatch("approve")

    def action_quick_deny(self) -> None:
        self._dispatch("reject")

    def _dispatch(self, decision: str) -> None:
        self.post_message(self.Decided(call_id=self.call_id, decision=decision))
        self.remove()

    # Mouse click on an option line confirms it (best-effort).
    def on_click(self, event: events.Click) -> None:
        # Click anywhere on the widget = focus (so kbd works after mouse).
        self.focus()
