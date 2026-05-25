"""Tests for banner narrow-threshold + short-mode behaviour.

Audit fix #5: `_NARROW_THRESHOLD = max(_LOGO_WIDTH, 60)` was 70 in
practice (figlet width = 70), so terminals 60–69 cols painted an
overflowing figlet. The fix: subtract a 10-col safety margin so the
fallback trips well before the figlet would clip.
"""
from __future__ import annotations

from cogitum.widgets import banner as banner_mod


def test_narrow_threshold_below_logo_width():
    """The threshold must be strictly below the actual logo width so
    the figlet never paints when it would clip."""
    assert banner_mod._NARROW_THRESHOLD < banner_mod._LOGO_WIDTH


def test_narrow_threshold_has_safety_margin():
    """We subtract 10 cols from logo width as a buffer against
    Align.center padding + Textual border."""
    expected = max(banner_mod._LOGO_WIDTH - 10, 40)
    assert banner_mod._NARROW_THRESHOLD == expected


def test_narrow_threshold_floor_at_40():
    """Even if the logo somehow shrinks below 50, we still floor at 40
    so the compact banner has room to breathe."""
    # Sanity: the threshold is at least 40
    assert banner_mod._NARROW_THRESHOLD >= 40


def test_logo_width_unchanged():
    """Logo source/width hasn't drifted — sanity check for the constant."""
    assert banner_mod._LOGO_WIDTH > 0
    # The COGITUM ansi_shadow figlet is somewhere in the 50-90 col range
    # depending on pyfiglet version. Just assert a sane bracket.
    assert 50 <= banner_mod._LOGO_WIDTH <= 90
