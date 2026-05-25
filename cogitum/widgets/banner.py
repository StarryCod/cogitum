"""Banner — layered ansi_shadow figlet.

The shadow font already includes integrated depth (the glyphs have
double walls). We render it once in high gold and tint the heavy
shadow lines a darker bronze so it reads as embossed.

In narrow terminals (< 60 cols) the figlet logo would overflow and
mangle the layout, so we fall back to a compact single-line title.
"""
from __future__ import annotations

import pyfiglet
from rich.align import Align
from rich.console import Group
from rich.text import Text
from textual.widget import Widget

from ..design import (
    BRONZE,
    COPPER,
    GOLD,
    GOLD_DIM,
    GOLD_HI,
    MUTED,
    TXT_DIM,
)


_LOGO_RAW = pyfiglet.figlet_format("COGITUM", font="ansi_shadow", width=200).rstrip("\n")
_LOGO_WIDTH = max((len(line) for line in _LOGO_RAW.splitlines()), default=0)
# Bug fix: previously max(_LOGO_WIDTH, 60) gave 70, so terminals 60–69
# cols wide drew an overflowing figlet (no fallback, no clipping marker).
# Now we trip the fallback well before the figlet would clip — leaving a
# 10-col safety margin around the actual logo width.
_NARROW_THRESHOLD = max(_LOGO_WIDTH - 10, 40)


def _layered(text: str) -> Text:
    """Tint the shadow glyphs warmer than the highlight glyphs.

    ansi_shadow uses '█' for highlight columns and '╗╝╚╔' / '═║' for
    the inner shadow. We colour the highlight in GOLD_HI and the
    shadow runes in COPPER so the eye reads depth, not flat gold.
    """
    out = Text()
    for ch in text:
        if ch == "█":
            out.append(ch, style=GOLD_HI)
        elif ch in "╗╝╚╔":
            out.append(ch, style=COPPER)
        elif ch in "═║":
            out.append(ch, style=BRONZE)
        else:
            out.append(ch, style=GOLD_DIM)
    return out


_CANT = "forge mark vii  ·  agentic cli"


class Banner(Widget):
    def render(self):
        # M14: narrow-terminal fallback. If the figlet logo would overflow
        # the available width (e.g. user split the pane or runs in a small
        # tmux pane), draw a compact single-line title instead. The full
        # logo is gorgeous but un-survivable in <60-col terminals.
        try:
            avail = self.size.width
        except Exception:
            avail = _LOGO_WIDTH

        # Responsive: when the App is on a short or narrow terminal,
        # always use the compact one-line banner. The TCSS template
        # collapses #banner height to 3 in -short so the figlet would
        # be clipped vertically anyway.
        force_compact = False
        try:
            app = self.app
            if app.has_class("-short") or app.has_class("-narrow"):
                force_compact = True
        except Exception:
            pass

        if force_compact or (avail and avail < _NARROW_THRESHOLD):
            compact = Text()
            compact.append("◆ ", style=GOLD_HI)
            compact.append("COGITUM", style=f"bold {GOLD_HI}")
            compact.append("  ·  ", style=MUTED)
            compact.append(_CANT, style=f"italic {GOLD_DIM}")
            return Align.center(compact)

        logo = _layered(_LOGO_RAW)
        cant = Text(_CANT, style=f"italic {GOLD_DIM}")
        return Group(Align.center(logo), Align.center(Text("")), Align.center(cant))


class BannerTags(Widget):
    """Tag strip beneath the banner."""

    def __init__(
        self,
        tags: list[tuple[str, str, str]] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._tags = tags or [
            ("status",  "ready",     GOLD),
            ("model",   "opus 4.7",  GOLD_HI),
            ("agents",  "1 / 4",     BRONZE),
            ("project", "Cogitum",   TXT_DIM),
        ]

    def render(self):
        out = Text(justify="center")
        sep = Text("   ·   ", style=MUTED)
        for i, (label, value, color) in enumerate(self._tags):
            if i:
                out.append_text(sep)
            out.append(label, style=GOLD_DIM)
            out.append("  ")
            out.append(value, style=f"bold {color}")
        return out
