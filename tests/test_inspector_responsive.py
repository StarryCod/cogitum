"""Tests for the inspector's responsive widths.

Audit fix #6: the context bar was hard-coded at width=18 and the
last_error truncation at [:80], independent of the inspector pane's
actual width. The fix adds adaptive helpers and an `on_resize` that
re-renders.
"""
from __future__ import annotations

from cogitum.widgets.inspector import Inspector, _bar


def test_adaptive_bar_width_default_pane():
    """36-col inspector pane → 9-col bar (¼ rule)."""
    assert Inspector._adaptive_bar_width(36) == 9


def test_adaptive_bar_width_floor_at_eight():
    """Even tiny panes get an 8-col bar so it stays a recognisable bar."""
    assert Inspector._adaptive_bar_width(20) == 8
    assert Inspector._adaptive_bar_width(8) == 8


def test_adaptive_bar_width_caps_at_24():
    """Wide panes don't get a runaway bar — capped at 24 cols."""
    assert Inspector._adaptive_bar_width(120) == 24
    assert Inspector._adaptive_bar_width(400) == 24


def test_adaptive_bar_width_zero_falls_back():
    """A 0-width pane (no size data yet) returns the legacy default 18."""
    assert Inspector._adaptive_bar_width(0) == 18


def test_adaptive_error_budget_default():
    assert Inspector._adaptive_error_budget(36) == 40  # floors at 40


def test_adaptive_error_budget_scales_up_on_wide():
    assert Inspector._adaptive_error_budget(200) == 196  # pane - 4


def test_adaptive_error_budget_floor_at_40():
    assert Inspector._adaptive_error_budget(20) == 40


def test_adaptive_error_budget_zero_falls_back_to_legacy_80():
    """0-width pane keeps the original [:80] cap."""
    assert Inspector._adaptive_error_budget(0) == 80


def test_bar_clamps_zero_width_safely():
    """_bar must not crash when width=0 (pre-mount, etc.)."""
    out = _bar(0.5, width=0)
    # Width is clamped to 1 internally, so we still get a single cell.
    assert len(out.plain) == 1


def test_bar_renders_full_at_pct_one():
    out = _bar(1.0, width=10)
    # Filled glyphs only — count via the GLYPH_BAR_FULL char
    from cogitum.design import GLYPH_BAR_FULL
    assert out.plain.count(GLYPH_BAR_FULL) == 10


def test_bar_renders_empty_at_pct_zero():
    from cogitum.design import GLYPH_BAR_EMPTY
    out = _bar(0.0, width=10)
    assert out.plain.count(GLYPH_BAR_EMPTY) == 10
