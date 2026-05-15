"""Cogitum design tokens.

Imperial Fists colorway — gold/bronze/copper on charcoal.
All warm tones, no blue signal. Single source of truth: change
a hex here, the whole TUI moves.
"""
from __future__ import annotations

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

# ── Glyphs (no emoji) ───────────────────────────────────────────────────────
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
