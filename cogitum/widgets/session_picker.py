"""
cogitum.widgets.session_picker
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Modal for /resume — browse and search past sessions with preview panel.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from ..core.sessions import SessionMeta, get_store
from ..design import BRONZE, GOLD, GOLD_HI, MUTED, TXT, TXT_DIM, TXT_FAINT, BG, SURFACE


class SessionItem(Static):
    """One session row in the list."""

    def __init__(self, meta: SessionMeta, **kw) -> None:
        super().__init__(**kw)
        self.meta = meta
        self._selected = False

    def render(self):
        m = self.meta
        t = Text()
        prefix = "▸ " if self._selected else "  "
        title_style = f"bold {GOLD_HI}" if self._selected else GOLD
        t.append(prefix, style=GOLD_HI if self._selected else MUTED)
        t.append(m.title[:35], style=title_style)
        # Time ago
        ago = _time_ago(m.updated_at)
        t.append(f"  {ago}", style=TXT_DIM)
        # Message count
        t.append(f"  {m.count}✉", style=MUTED)
        return t

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.refresh()


class SessionPicker(ModalScreen):
    """Modal screen for browsing/searching sessions with preview."""

    DEFAULT_CSS = f"""
    SessionPicker {{
        align: center middle;
    }}
    #session-picker-box {{
        width: 90;
        height: 28;
        background: {BG};
        border: round {BRONZE};
        padding: 1 2;
    }}
    #session-header {{
        width: 100%;
        height: 1;
        margin-bottom: 1;
    }}
    #session-search {{
        width: 100%;
        margin-bottom: 1;
        background: {SURFACE};
        color: {TXT};
        border: tall {BRONZE};
    }}
    #session-search:focus {{
        border: tall {GOLD_HI};
    }}
    #session-content {{
        height: 1fr;
        width: 100%;
    }}
    #session-list-pane {{
        width: 40;
        height: 100%;
        overflow-y: auto;
    }}
    #session-preview-pane {{
        width: 1fr;
        height: 100%;
        border-left: tall {BRONZE};
        padding: 0 1;
        overflow-y: auto;
    }}
    #session-empty {{
        color: {TXT_FAINT};
        text-align: center;
        padding: 2;
    }}
    #preview-header {{
        color: {GOLD_HI};
        text-style: bold;
        height: 1;
        margin-bottom: 1;
    }}
    #preview-body {{
        color: {TXT};
        height: auto;
    }}
    .preview-role-user {{
        color: {GOLD_HI};
    }}
    .preview-role-assistant {{
        color: {BRONZE};
    }}
    .preview-role-system {{
        color: {TXT_FAINT};
    }}
    SessionItem {{
        height: 1;
        width: 100%;
    }}
    """

    @dataclass
    class Selected(Message):
        session_id: str

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self._items: list[SessionMeta] = []
        self._selected_idx: int = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="session-picker-box"):
            yield Static(
                Text.assemble(
                    ("⟳ Resume Session", f"bold {GOLD_HI}"),
                    ("  ↑↓ navigate  Enter select  Esc cancel", TXT_DIM),
                ),
                id="session-header",
            )
            yield Input(placeholder="search sessions...", id="session-search")
            with Horizontal(id="session-content"):
                yield Vertical(id="session-list-pane")
                yield Vertical(id="session-preview-pane")

    async def on_mount(self) -> None:
        await self._load_sessions("")
        self.query_one("#session-search", Input).focus()

    @on(Input.Changed, "#session-search")
    async def _on_search_changed(self, event: Input.Changed) -> None:
        await self._load_sessions(event.value)

    async def _load_sessions(self, query: str) -> None:
        store = get_store()
        if query.strip():
            self._items = store.search(query.strip(), limit=20)
        else:
            self._items = store.list_sessions(limit=20)
        self._selected_idx = 0
        await self._render_list()
        await self._render_preview()

    async def _render_list(self) -> None:
        container = self.query_one("#session-list-pane", Vertical)
        await container.remove_children()
        if not self._items:
            await container.mount(Static("no sessions found"))
            return
        items: list[SessionItem] = []
        for i, meta in enumerate(self._items):
            item = SessionItem(meta, id=f"session-{i}")
            if i == self._selected_idx:
                item._selected = True
            items.append(item)
        if items:
            await container.mount(*items)

    async def _render_preview(self) -> None:
        """Show last messages from selected session."""
        preview_pane = self.query_one("#session-preview-pane", Vertical)
        await preview_pane.remove_children()

        if not self._items:
            await preview_pane.mount(Static("no session selected"))
            return

        meta = self._items[self._selected_idx]
        store = get_store()

        # Header with model info
        header_parts = [
            (meta.title[:40], f"bold {GOLD_HI}"),
        ]
        if meta.model:
            short_model = meta.model.split("/")[-1] if "/" in meta.model else meta.model
            header_parts.append((f"  {short_model}", BRONZE))
        header_parts.append((f"  {meta.count} msgs", TXT_DIM))
        await preview_pane.mount(Static(Text.assemble(*header_parts)))

        # Load last N messages for preview
        messages = store.load_session(meta.id)
        # Show last 8 messages
        preview_msgs = messages[-8:] if len(messages) > 8 else messages

        if not preview_msgs:
            await preview_pane.mount(Static("(empty session)"))
            return

        lines: list[Text] = []
        for msg in preview_msgs:
            role = msg.role
            # Get text content
            text_content = ""
            for part in msg.parts:
                if hasattr(part, "text") and part.kind == "text":
                    text_content = part.text
                    break
                elif hasattr(part, "name") and part.kind == "tool_call":
                    text_content = f"⚙ {part.name}(...)"
                    break
                elif hasattr(part, "content") and part.kind == "tool_result":
                    text_content = f"→ {part.content[:60]}"
                    break

            if not text_content:
                continue

            # Truncate to fit preview
            preview_text = text_content.replace("\n", " ")[:70]
            if len(text_content) > 70:
                preview_text += "…"

            line = Text()
            if role == "user":
                line.append("YOU ", style=f"bold {GOLD_HI}")
            elif role == "assistant":
                line.append("AI  ", style=f"bold {BRONZE}")
            elif role == "tool":
                line.append("⚙   ", style=MUTED)
            else:
                line.append("SYS ", style=MUTED)
            line.append(preview_text, style=TXT if role != "tool" else TXT_DIM)
            lines.append(line)

        # Render all lines as one Static with newlines
        combined = Text("\n").join(lines)
        await preview_pane.mount(Static(combined))

    async def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)
            event.prevent_default()
            event.stop()
        elif event.key == "up":
            if self._items and self._selected_idx > 0:
                self._selected_idx -= 1
                self._update_selection()
                await self._render_preview()
            event.prevent_default()
            event.stop()
        elif event.key == "down":
            if self._items and self._selected_idx < len(self._items) - 1:
                self._selected_idx += 1
                self._update_selection()
                await self._render_preview()
            event.prevent_default()
            event.stop()
        elif event.key == "enter":
            if self._items:
                meta = self._items[self._selected_idx]
                self.dismiss(meta.id)
            event.prevent_default()
            event.stop()
        elif event.key == "delete":
            # Delete selected session
            if self._items:
                meta = self._items[self._selected_idx]
                get_store().delete_session(meta.id)
                await self._load_sessions(
                    self.query_one("#session-search", Input).value
                )
            event.prevent_default()
            event.stop()

    def _update_selection(self) -> None:
        container = self.query_one("#session-list-pane", Vertical)
        for i, child in enumerate(container.children):
            if isinstance(child, SessionItem):
                child.set_selected(i == self._selected_idx)


def _time_ago(ts: float) -> str:
    """Human-readable time ago."""
    diff = time.time() - ts
    if diff < 60:
        return "now"
    elif diff < 3600:
        m = int(diff // 60)
        return f"{m}m"
    elif diff < 86400:
        h = int(diff // 3600)
        return f"{h}h"
    else:
        d = int(diff // 86400)
        return f"{d}d"
