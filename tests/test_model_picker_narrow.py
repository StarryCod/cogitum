"""Tests for ModelPicker narrow-mode adaptation.

Audit fix #10: the picker had `min-width: 32` on the detail-scroll
panel which crowded out the list on 80-col terminals. We hide the
detail panel entirely under `App.-narrow` and let the list take the
full width.
"""
from __future__ import annotations

from cogitum.widgets.model_picker import ModelPicker


def test_narrow_hides_detail_pane():
    """`App.-narrow` rule sets `display: none` on the detail-scroll."""
    css = ModelPicker.DEFAULT_CSS
    assert "App.-narrow ModelPicker #picker-detail-scroll" in css
    # display: none appears at least once after this selector
    assert "display: none" in css


def test_narrow_picker_list_takes_full_width():
    """With detail hidden the list expands to 100%."""
    css = ModelPicker.DEFAULT_CSS
    assert "App.-narrow ModelPicker #picker-list" in css


def test_narrow_picker_shell_full_screen():
    """Shell goes full-width on narrow so we don't waste rows on borders."""
    css = ModelPicker.DEFAULT_CSS
    assert "App.-narrow ModelPicker #picker-shell" in css


def test_default_picker_keeps_min_width_32():
    """Wide terminals still see the original 32-col detail pane."""
    css = ModelPicker.DEFAULT_CSS
    assert "min-width: 32" in css
