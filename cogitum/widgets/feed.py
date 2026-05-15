"""Conversation feed — bubbles for user, plain text for agent.

Layout rules:
  · YOU       → bubble (warm surface, gold left bar, padded)
  · AGENT     → no bubble, plain text on canvas, label above
  · TOOL      → specialised card (handled by widgets/cards.py)
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
        # tiny header
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


# ── Agent plain ─────────────────────────────────────────────────────────────

class AgentBlock(Static):
    DEFAULT_CLASSES = "feed-entry feed-agent"

    def __init__(self, text: str, meta: str = "", **kw) -> None:
        super().__init__(self._build(text, meta), **kw)

    @staticmethod
    def _build(text: str, meta: str) -> Text:
        out = Text()
        out.append("AGENT", style=f"bold {GOLD}")
        if meta:
            out.append("   ")
            out.append(meta, style=f"italic {TXT_DIM}")
        out.append("   ")
        out.append(datetime.now().strftime("%H:%M"), style=GOLD_DIM)
        out.append("\n")
        for line in text.splitlines() or [""]:
            out.append(line + "\n", style=TXT)
        if out.plain.endswith("\n"):
            out.right_crop(1)
        return out


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
    """Append-only conversation feed."""

    def append_user(self, text: str, meta: str = "") -> None:
        self.mount(UserBubble(text, meta))
        self.scroll_end(animate=False)

    def append_agent(self, text: str, meta: str = "") -> None:
        self.mount(AgentBlock(text, meta))
        self.scroll_end(animate=False)

    def append_error(self, text: str, meta: str = "") -> None:
        self.mount(ErrorBlock(text, meta))
        self.scroll_end(animate=False)

    def append_system(self, text: str, meta: str = "") -> None:
        self.mount(SystemLine(text, meta))
        self.scroll_end(animate=False)

    def append_card(self, widget) -> None:
        """Insert one of the specialised tool cards."""
        self.mount(widget)
        self.scroll_end(animate=False)

    def clear(self) -> None:
        """Remove all feed entries."""
        for child in list(self.children):
            child.remove()

    # convenience for the demo
    def append(self, kind: str, text: str, meta: str = "") -> None:
        if kind == "user":   self.append_user(text, meta)
        elif kind == "agent": self.append_agent(text, meta)
        elif kind == "error": self.append_error(text, meta)
        elif kind == "system": self.append_system(text, meta)
