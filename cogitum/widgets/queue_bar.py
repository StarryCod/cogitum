"""Pinned queue bar — fixed widget between feed and composer.

Shows queued messages that will be sent to agent after current turn.
Does NOT scroll with feed. Always visible. Click row or press arrow-up
on empty composer to pop the latest message back for editing.
"""
from __future__ import annotations

from rich.text import Text
from textual.widget import Widget

from ..design import GOLD, GOLD_DIM, MUTED, TXT_DIM


class QueueBar(Widget):
    """Pinned bar showing queued messages above composer."""

    DEFAULT_CSS = """
    QueueBar {
        height: auto;
        max-height: 5;
        width: 100%;
        background: #161410;
        border-top: tall #2A2620;
        padding: 0 2;
        display: none;
    }
    QueueBar.has-items {
        display: block;
    }
    """

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self._items: list[str] = []

    def add(self, text: str) -> None:
        self._items.append(text)
        self._refresh()

    def pop_last(self) -> str | None:
        """Remove last queued message and return its text."""
        if self._items:
            text = self._items.pop()
            self._refresh()
            return text
        return None

    def pop_first(self) -> str | None:
        """Remove first queued message (for sending) and return its text."""
        if self._items:
            text = self._items.pop(0)
            self._refresh()
            return text
        return None

    def clear(self) -> None:
        self._items.clear()
        self._refresh()

    @property
    def count(self) -> int:
        return len(self._items)

    def _refresh(self) -> None:
        if self._items:
            self.add_class("has-items")
        else:
            self.remove_class("has-items")
        self.refresh()

    def render(self) -> Text:
        if not self._items:
            return Text("")
        out = Text()
        out.append("◇ ", style=GOLD)
        out.append(f"queue ({len(self._items)})", style=f"bold {GOLD_DIM}")
        out.append("  ↑ to edit last", style=MUTED)
        out.append("\n")
        # Show up to 3 most recent items
        visible = self._items[-3:]
        if len(self._items) > 3:
            out.append(f"  · …{len(self._items) - 3} earlier\n", style=MUTED)
        for i, item in enumerate(visible):
            display = item[:90] + ("…" if len(item) > 90 else "")
            out.append("  · ", style=GOLD_DIM)
            out.append(display, style=TXT_DIM)
            if i < len(visible) - 1:
                out.append("\n")
        return out
