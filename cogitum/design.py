"""Cogitum design tokens.

Theme-aware: at import time, palette constants are populated from
the active theme registered in :mod:`cogitum.themes`. The default
theme is Imperial Fists (gold/bronze/copper on charcoal); other
WH40K-canon presets (Salamanders, Iron Warriors, Death Korps,
Adeptus Mechanicus, Black Templars) live in ``themes.py`` and are
swapped via the Setup wizard's Themes section (or directly by
writing ``[experimental] theme = "..."`` to settings.toml + restart).

The constants below are guaranteed to exist for all themes via the
``TOKEN_NAMES`` contract in ``themes.py`` — a partial theme dict is
rejected at the loader level so a missing key never silently falls
through to a wrong colour.

Single source of truth: change a hex inside a theme dict (or pick a
different theme), the whole TUI moves on next restart. CSS literals
that live in ``cogitum.tcss`` bake in at startup; Python-side Text
styles re-read tokens dynamically and refresh on the next render.
"""
from __future__ import annotations

import os
import sys

from .themes import get_active_theme

# ── Palette ─────────────────────────────────────────────────────────────────
# Resolved from the active theme at module load. Importers that do
# `from .design import GOLD_HI` continue to work unchanged; the value
# they get is the theme-correct hex.
_THEME = get_active_theme()

GOLD_HI    = _THEME["GOLD_HI"]
GOLD       = _THEME["GOLD"]
BRONZE     = _THEME["BRONZE"]
COPPER     = _THEME["COPPER"]
GOLD_DIM   = _THEME["GOLD_DIM"]
RUST       = _THEME["RUST"]
OK         = _THEME["OK"]
OLIVE      = _THEME["OLIVE"]

BG         = _THEME["BG"]
BG_SOFT    = _THEME["BG_SOFT"]
SURFACE    = _THEME["SURFACE"]
SURFACE_HI = _THEME["SURFACE_HI"]
RULE       = _THEME["RULE"]

TXT        = _THEME["TXT"]
TXT_DIM    = _THEME["TXT_DIM"]
MUTED      = _THEME["MUTED"]


# ── Unicode capability detection ────────────────────────────────────────────
#
# On Windows the legacy cmd.exe and older PowerShell hosts often run on
# CP866 / CP1251 codepages where Box-Drawing characters render as blanks.
# Newer Windows Terminal handles UTF-8 fine, but a substantial fraction
# of users we want to support are still on the old hosts.
#
# Detection strategy (cheap, no probe-print):
#   1. Honour explicit COGITUM_ASCII=1 — bail-out for users who hate
#      the unicode glyphs even on capable terminals.
#   2. On any platform, if stdout encoding is not utf-8, fall back to
#      ASCII. This catches CP866/CP1251/CP437 cmd.exe sessions before
#      they paint the screen with question marks.
#   3. Otherwise, use the Unicode set.
#
# We do NOT just check sys.platform == "win32" — Windows Terminal with
# UTF-8 codepage is fine, and we want to use the pretty glyphs there.

def _is_unicode_capable() -> bool:
    if os.environ.get("COGITUM_ASCII"):
        return False
    enc = (getattr(sys.stdout, "encoding", None) or "").lower()
    if not enc:
        return False
    # Treat any UTF-* encoding as capable (utf-8, utf-16-le, etc.).
    return "utf" in enc


_UNICODE = _is_unicode_capable()


# ── Glyphs (no emoji) ───────────────────────────────────────────────────────
#
# All glyphs ship in two forms — the rich Unicode shape and a 7-bit
# ASCII fallback that renders correctly on any codepage. The active
# choice is locked at module import time per _is_unicode_capable().

if _UNICODE:
    GLYPH_PROMPT      = "▶"
    GLYPH_TOOL        = "◇"
    GLYPH_OK          = "✓"
    GLYPH_FAIL        = "✗"
    GLYPH_RUN         = "◆"
    GLYPH_WAIT        = "·"
    GLYPH_BULLET      = "·"
    GLYPH_ARROW       = "→"
    GLYPH_DIVIDER_H   = "─"
    GLYPH_DIVIDER_V   = "│"
    GLYPH_BAR_FULL    = "▰"
    GLYPH_BAR_EMPTY   = "▱"
else:
    GLYPH_PROMPT      = ">"
    GLYPH_TOOL        = "*"
    GLYPH_OK          = "v"
    GLYPH_FAIL        = "x"
    GLYPH_RUN         = "*"
    GLYPH_WAIT        = "."
    GLYPH_BULLET      = "."
    GLYPH_ARROW       = "->"
    GLYPH_DIVIDER_H   = "-"
    GLYPH_DIVIDER_V   = "|"
    GLYPH_BAR_FULL    = "#"
    GLYPH_BAR_EMPTY   = "."
