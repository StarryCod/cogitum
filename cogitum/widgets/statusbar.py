"""Bottom status bar — keys + breadcrumbs, all in warm palette."""
from __future__ import annotations

from rich.text import Text
from textual.widget import Widget

from ..design import (
    BRONZE,
    GOLD,
    GOLD_DIM,
    TXT_DIM,
)


_KEYS = [
    ("⏎",       "send"),
    ("Esc",     "stop"),
    ("Ctrl+P",  "models"),
    ("Ctrl+S",  "setup"),
    ("Ctrl+Q",  "quit"),
]


class StatusBar(Widget):
    def __init__(
        self,
        channel: str = "local",
        model: str = "—",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._channel = channel
        self._model = model

    def set_model(self, model: str) -> None:
        self._model = model
        self.refresh()

    def set_channel(self, channel: str) -> None:
        self._channel = channel
        self.refresh()

    def render(self):
        out = Text()
        for i, (k, l) in enumerate(_KEYS):
            if i:
                out.append("   ")
            out.append(k, style=f"bold {GOLD}")
            out.append(" ", style=GOLD_DIM)
            out.append(l, style=TXT_DIM)
        out.append("    ·   ")
        out.append("model: ", style=GOLD_DIM)
        out.append(self._model, style=GOLD)
        out.append("   ·   ")
        out.append(self._channel, style=BRONZE)
        return out
