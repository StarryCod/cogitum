"""
cogitum.themes
~~~~~~~~~~~~~~

WH40K-themed colour presets. All warm/in-canon, no off-universe palettes.

Each theme is a flat dict mapping the design-token names from
:mod:`cogitum.design` to hex colours. The active theme is selected
via ``settings.toml`` under ``[experimental] theme = "..."`` and read
once at import (changes require a restart for the CSS file to be
regenerated).

Adding a theme:
  1. Append a TOKEN_NAMES-keyed dict under ``THEMES``.
  2. The dict must define every name in ``TOKEN_NAMES`` — the loader
     refuses partial themes so a missing key never silently falls
     through to a wrong colour.
  3. Add a one-line lore caption under ``THEME_BLURBS`` so the
     setup wizard can show it on the theme card.

Naming: snake_case canonical id (used in settings.toml) + a display
name in :data:`THEME_DISPLAY_NAMES` for the UI.
"""
from __future__ import annotations

from typing import Mapping


# Token names every theme must define. Keep in sync with the constants
# exported from :mod:`cogitum.design`.
TOKEN_NAMES: tuple[str, ...] = (
    "GOLD_HI", "GOLD", "BRONZE", "COPPER", "GOLD_DIM",
    "RUST", "OK", "OLIVE",
    "BG", "BG_SOFT", "SURFACE", "SURFACE_HI", "RULE",
    "TXT", "TXT_DIM", "MUTED",
)


# ─── Imperial Fists (default) ───────────────────────────────────────────────
# Bright sons of Dorn. Gold/bronze/copper on charcoal, parchment text.
# This is Cogitum's original colourway — warm, high-contrast, ceremonial.
IMPERIAL_FISTS = {
    "GOLD_HI":    "#F5C24A",
    "GOLD":       "#D9A23B",
    "BRONZE":     "#A8732D",
    "COPPER":     "#8C5A22",
    "GOLD_DIM":   "#7A5A1A",
    "RUST":       "#9B3A2A",
    "OK":         "#9B8B3A",
    "OLIVE":      "#5C5430",
    "BG":         "#0E0E11",
    "BG_SOFT":    "#161618",
    "SURFACE":    "#1C1C1F",
    "SURFACE_HI": "#22221F",
    "RULE":       "#2A2620",
    "TXT":        "#E6E1CF",
    "TXT_DIM":    "#9C957D",
    "MUTED":      "#5A5648",
}


# ─── Salamanders ────────────────────────────────────────────────────────────
# Vulkan's sons. Forest-green plate with brass trim, ember undertones.
# Easier on eyes that find pure gold too bright. Olive-on-charcoal,
# warm enough to stay in WH40K register, dark enough to be calm.
SALAMANDERS = {
    "GOLD_HI":    "#7DA055",   # ember-green primary accent
    "GOLD":       "#5E8A3D",   # mid green
    "BRONZE":     "#3F6A2D",   # deep moss
    "COPPER":     "#2D4A22",   # dark forest
    "GOLD_DIM":   "#3A4F2A",   # frame green
    "RUST":       "#A04A2A",   # ember-red (errors)
    "OK":         "#7A8A3A",   # olive-green confirmations
    "OLIVE":      "#3A4030",   # idle state
    "BG":         "#0D110E",   # near-black green-tinted
    "BG_SOFT":    "#141A14",
    "SURFACE":    "#1A1F1A",
    "SURFACE_HI": "#1F2620",
    "RULE":       "#26302A",
    "TXT":        "#D9DCC4",   # bone-white parchment
    "TXT_DIM":    "#8A9078",
    "MUTED":      "#525846",
}


# ─── Iron Warriors ──────────────────────────────────────────────────────────
# Perturabo's siegers. Gunmetal grey + hazard yellow stripes.
# Cold but technically still Imperial era pre-Heresy → in canon.
# The closest theme to "muted greyscale" while staying WH40K.
IRON_WARRIORS = {
    "GOLD_HI":    "#C9A436",   # hazard-yellow accent (sparingly)
    "GOLD":       "#8E7A30",   # mid hazard
    "BRONZE":     "#7A7A7A",   # gunmetal mid
    "COPPER":     "#5C5C5C",   # darker steel
    "GOLD_DIM":   "#4A4A4A",   # frame steel
    "RUST":       "#A85030",   # rust (their favourite weathering)
    "OK":         "#8E8A56",   # tarnished brass
    "OLIVE":      "#3A3A3A",
    "BG":         "#0F0F0F",   # void grey
    "BG_SOFT":    "#161616",
    "SURFACE":    "#1C1C1C",
    "SURFACE_HI": "#222222",
    "RULE":       "#2A2A2A",
    "TXT":        "#D6D6D0",   # off-white
    "TXT_DIM":    "#90908A",
    "MUTED":      "#5A5A56",
}


# ─── Death Korps of Krieg ───────────────────────────────────────────────────
# Trench guardsmen. Mud, gas masks, parchment orders, gunmetal rifles.
# Khaki-on-mud palette. Reads as "weathered" and "old paper".
DEATH_KORPS = {
    "GOLD_HI":    "#B8A872",   # khaki-bone primary
    "GOLD":       "#9A8A5A",   # mid khaki
    "BRONZE":     "#7A6E48",   # mud-brass
    "COPPER":     "#5E5638",   # deep mud
    "GOLD_DIM":   "#4A4530",
    "RUST":       "#8A4030",   # blood / weathered red
    "OK":         "#8A8260",   # parchment olive
    "OLIVE":      "#3F3D2A",
    "BG":         "#10100C",   # near-black khaki-tinted
    "BG_SOFT":    "#181814",
    "SURFACE":    "#1C1C18",
    "SURFACE_HI": "#23231E",
    "RULE":       "#2A2820",
    "TXT":        "#D8D0BC",   # ration-parchment
    "TXT_DIM":    "#8E8770",
    "MUTED":      "#56523F",
}


# ─── Adeptus Mechanicus ─────────────────────────────────────────────────────
# Mars red robes + brass cybernetics + stark white binary cant.
# More red-saturated than the others — for users who want the
# Cogitum-Primus persona reflected in the chrome itself.
ADEPTUS_MECHANICUS = {
    "GOLD_HI":    "#D14A38",   # Mars red primary accent
    "GOLD":       "#B03A2C",   # mid red
    "BRONZE":     "#8E2E22",   # dark red
    "COPPER":     "#6E251D",   # deep red-brown
    "GOLD_DIM":   "#5A1F18",
    "RUST":       "#D14A38",   # errors share the primary (intentional)
    "OK":         "#A8782A",   # brass for confirmations (in-canon)
    "OLIVE":      "#3A2820",
    "BG":         "#100808",   # near-black red-tinted
    "BG_SOFT":    "#180E0E",
    "SURFACE":    "#1F1414",
    "SURFACE_HI": "#261818",
    "RULE":       "#2E1E1A",
    "TXT":        "#E2D6C4",   # parchment with red wash
    "TXT_DIM":    "#9E8E78",
    "MUTED":      "#5E4A3E",
}


# ─── Black Templars ─────────────────────────────────────────────────────────
# Dorn's zealous splinter. Black armour with bone-white shoulders.
# Minimum colour, maximum contrast. For users who want it darker
# and starker than Imperial Fists without leaving the canon.
BLACK_TEMPLARS = {
    "GOLD_HI":    "#E8DCC4",   # bone-white primary
    "GOLD":       "#C2B898",   # off-bone
    "BRONZE":     "#8A8470",   # weathered ivory
    "COPPER":     "#5E5A4A",   # dark grey-bone
    "GOLD_DIM":   "#3E3C32",
    "RUST":       "#A03A2A",   # crusader red (errors / accents only)
    "OK":         "#7A7660",   # tarnished bone
    "OLIVE":      "#2E2C24",
    "BG":         "#080808",   # near-pure black
    "BG_SOFT":    "#0F0F0F",
    "SURFACE":    "#141414",
    "SURFACE_HI": "#1C1C1A",
    "RULE":       "#222220",
    "TXT":        "#D8D0B8",
    "TXT_DIM":    "#8E8770",
    "MUTED":      "#4E4A3E",
}


# ─── Registry ───────────────────────────────────────────────────────────────

THEMES: dict[str, dict[str, str]] = {
    "imperial_fists":     IMPERIAL_FISTS,
    "salamanders":        SALAMANDERS,
    "iron_warriors":      IRON_WARRIORS,
    "death_korps":        DEATH_KORPS,
    "adeptus_mechanicus": ADEPTUS_MECHANICUS,
    "black_templars":     BLACK_TEMPLARS,
}


THEME_DISPLAY_NAMES: dict[str, str] = {
    "imperial_fists":     "Imperial Fists",
    "salamanders":        "Salamanders",
    "iron_warriors":      "Iron Warriors",
    "death_korps":        "Death Korps of Krieg",
    "adeptus_mechanicus": "Adeptus Mechanicus",
    "black_templars":     "Black Templars",
}


THEME_BLURBS: dict[str, str] = {
    "imperial_fists":
        "Sons of Dorn. Gold on charcoal — the original Cogitum colourway. "
        "Bright, ceremonial, high-contrast.",
    "salamanders":
        "Vulkan's sons. Forest-green plate with brass trim. Easier on the "
        "eyes than gold; stays warm and in-canon.",
    "iron_warriors":
        "Perturabo's siegers. Gunmetal greys with hazard-yellow stripes. "
        "The closest theme to muted greyscale while remaining 40K.",
    "death_korps":
        "Trench guardsmen. Khaki, mud, weathered parchment. Reads as old "
        "paper and gun-oil.",
    "adeptus_mechanicus":
        "Cult Mechanicus. Mars-red robes over near-black, brass for "
        "confirmations. Chrome that matches Cogitum-Primus.",
    "black_templars":
        "Dorn's zealous splinter. Bone-white on near-black. Minimum colour, "
        "maximum contrast — for stark moods.",
}


DEFAULT_THEME = "imperial_fists"


# ─── Loader ─────────────────────────────────────────────────────────────────


def _read_active_theme_name() -> str:
    """Resolve the active theme name from settings.toml.

    Returns the canonical theme id, falling back to ``DEFAULT_THEME``
    on any read error or when the configured value isn't a known
    preset. We don't import :mod:`cogitum.design` here to keep the
    import cycle clean.
    """
    try:
        from .core.llm.loader import load_settings
        settings = load_settings() or {}
        exp = settings.get("experimental") or {}
        name = str(exp.get("theme") or DEFAULT_THEME).strip().lower()
    except Exception:
        return DEFAULT_THEME
    return name if name in THEMES else DEFAULT_THEME


def get_active_theme() -> Mapping[str, str]:
    """Return the active theme's token dict (resolved at call time)."""
    name = _read_active_theme_name()
    return THEMES[name]


def get_active_theme_name() -> str:
    """Return the active theme's canonical id."""
    return _read_active_theme_name()


def write_active_theme(name: str) -> None:
    """Persist a new active theme to settings.toml.

    The change takes full effect after Cogitum restart (CSS file
    bakes hex literals at app load). UI components that read tokens
    via :mod:`cogitum.design` see the new values immediately on the
    next read, but already-mounted Static widgets still hold their
    rendered styles until refreshed.
    """
    if name not in THEMES:
        raise ValueError(f"unknown theme {name!r}")
    from .core.llm.loader import load_settings, write_settings
    settings = load_settings() or {}
    exp = settings.get("experimental") if isinstance(settings.get("experimental"), dict) else {}
    exp["theme"] = name
    settings["experimental"] = exp
    write_settings(settings)


__all__ = [
    "THEMES",
    "THEME_DISPLAY_NAMES",
    "THEME_BLURBS",
    "TOKEN_NAMES",
    "DEFAULT_THEME",
    "get_active_theme",
    "get_active_theme_name",
    "write_active_theme",
]
