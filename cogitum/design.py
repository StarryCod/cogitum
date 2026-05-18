"""Cogitum design tokens.

Imperial Fists colorway — gold/bronze/copper on charcoal.
All warm tones, no blue signal. Single source of truth: change
a hex here, the whole TUI moves.
"""
from __future__ import annotations

import os
import sys

# ── Palette ─────────────────────────────────────────────────────────────────
# Warm tonal ramp: dark steel → bronze → gold → high gold.
GOLD_HI    = "#F5C24A"   # primary accent (banner, focus, caret)
GOLD       = "#D9A23B"   # mid gold (titles, important values)
BRONZE     = "#A8732D"   # tool calls, secondary accents (replaces blue signal)
COPPER     = "#8C5A22"   # rules, dividers, mid surfaces accents
GOLD_DIM   = "#7A5A1A"   # subdued gold (frames, labels)
RUST       = "#9B3A2A"   # errors / heresy (warmer than pure red)
OK         = "#9B8B3A"   # confirmations (olive-gold, in-palette)
OLIVE      = "#5C5430"   # idle state

BG         = "#0E0E11"   # base canvas
BG_SOFT    = "#161618"   # panel background
SURFACE    = "#1C1C1F"   # tool card surface
SURFACE_HI = "#22221F"   # active surface
RULE       = "#2A2620"   # hairline separators (warm-tinted)

TXT        = "#E6E1CF"   # primary text (parchment)
TXT_DIM    = "#9C957D"   # secondary text
MUTED      = "#5A5648"   # tertiary / scrollback


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
