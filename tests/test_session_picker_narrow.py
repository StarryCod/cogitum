"""Tests for the SessionPicker modal — narrow-terminal adaptation.

Audit fix #2: the picker had `width: 90; height: 28` which clipped on
the classic 80x24 SSH window. We replaced those with adaptive sizing
plus `App.-narrow` overrides that drop the modal frame and hide the
preview pane.
"""
from __future__ import annotations

from cogitum.widgets.session_picker import SessionPicker


def test_default_css_uses_max_width_percentage():
    """Modal should never exceed terminal width — max-width 95% catches that."""
    css = SessionPicker.DEFAULT_CSS
    assert "max-width: 95%" in css
    assert "max-height: 90%" in css


def test_default_css_has_min_width_floor():
    """A min-width keeps the modal usable on tiny terminals."""
    css = SessionPicker.DEFAULT_CSS
    assert "min-width: 40" in css
    assert "min-height: 14" in css


def test_narrow_class_drops_modal_frame():
    """On `-narrow`, picker-box becomes full-screen with no border."""
    css = SessionPicker.DEFAULT_CSS
    assert "App.-narrow SessionPicker #session-picker-box" in css
    # Look for the full-width override block
    assert "border: none" in css


def test_narrow_class_hides_preview_pane():
    """On narrow terminals only the list survives — preview is hidden."""
    css = SessionPicker.DEFAULT_CSS
    assert "App.-narrow SessionPicker #session-preview-pane" in css
    assert "display: none" in css
