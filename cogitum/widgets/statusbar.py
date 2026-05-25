"""Bottom status bar — keys + breadcrumbs, all in warm palette.

Responsive behaviour:
  · -narrow (≤ 80 cols)  → compact form: drop verbose labels, hide setup/quit
  · -short  (≤ 24 rows)  → unchanged (we already collapse to 1 row)
  · default              → full key list with model + channel breadcrumbs
"""
from __future__ import annotations

from rich.text import Text
from textual.widget import Widget

from ..design import (
    BRONZE,
    GOLD,
    GOLD_DIM,
    TXT_DIM,
)


# Full key list. On -narrow terminals we drop everything past the third
# entry (Esc/Ctrl+S/Ctrl+P live behind shortcuts, the user knows them).
_KEYS = [
    ("⏎",       "send"),
    ("Esc",     "stop"),
    ("Ctrl+P",  "models"),
    ("Ctrl+S",  "setup"),
    ("Ctrl+Q",  "quit"),
]

# Compact subset: bare-minimum hint when horizontal space is tight.
_KEYS_COMPACT = [
    ("⏎",       "send"),
    ("Esc",     "stop"),
    ("Ctrl+Q",  "quit"),
]


def _ellipsize(s: str, max_len: int) -> str:
    """Trim *s* to *max_len* characters, replacing the tail with ``…``."""
    if max_len <= 0:
        return ""
    if len(s) <= max_len:
        return s
    if max_len == 1:
        return "…"
    return s[: max_len - 1] + "…"


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

    # ------------------------------------------------------------------
    # Render dispatch — picks compact vs full based on App's screen-class.
    # ------------------------------------------------------------------

    def _is_narrow(self) -> bool:
        try:
            return bool(self.app.has_class("-narrow"))
        except Exception:
            return False

    def render(self):
        return self._format_compact() if self._is_narrow() else self._format_full()

    def _format_full(self) -> Text:
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

    def _format_compact(self) -> Text:
        """Narrow-terminal layout. Keeps only ⏎/Esc/Ctrl+Q + a trimmed
        model id. Channel breadcrumb is dropped — every column counts."""
        out = Text()
        # Compute available width so we can keep the model id from
        # blowing past the right edge. On the very first paint
        # `self.size.width` may still be 0 — fall back to 80 (the
        # threshold above which `-narrow` won't be set anyway).
        try:
            avail = self.size.width or 80
        except Exception:
            avail = 80

        for i, (k, l) in enumerate(_KEYS_COMPACT):
            if i:
                out.append("  ")
            out.append(k, style=f"bold {GOLD}")
            if l:
                out.append(" ", style=GOLD_DIM)
                out.append(l, style=TXT_DIM)
        out.append("  · ")
        # Reserve ~10 cols for the prefix, ~4 cols for elipsis padding.
        budget = max(8, avail - len(out.plain) - 2)
        out.append(_ellipsize(self._model, budget), style=GOLD)
        return out
