"""Tests for composer + command-menu short-mode CSS rules.

Audit fix #9: Composer.max-height was 20 rows and CommandMenu.max-height
was 12. On a 24-row terminal that meant the dropdown alone could eat
half the screen. We collapse both to ≤5 rows under `App.-short`.
"""
from __future__ import annotations

from cogitum.widgets.composer import CommandMenu, Composer


def test_composer_collapses_on_short():
    """Short terminals get a 5-row composer max."""
    css = Composer.DEFAULT_CSS
    assert "App.-short Composer" in css
    # max-height 5 (or less) appears under -short
    assert "max-height: 5" in css


def test_composer_area_collapses_on_short():
    """ComposerArea height also collapses so the textarea doesn't push
    the composer past the screen-class cap."""
    css = Composer.DEFAULT_CSS
    assert "App.-short ComposerArea" in css
    assert "max-height: 3" in css


def test_command_menu_shrinks_on_short():
    """Command dropdown collapses to 4 rows on short screens."""
    css = CommandMenu.DEFAULT_CSS
    assert "App.-short CommandMenu" in css
    assert "max-height: 4" in css


def test_default_command_menu_max_height_unchanged_for_full_screen():
    """We didn't break the default — full screens still get 12 rows."""
    css = CommandMenu.DEFAULT_CSS
    # The default block still sets 12; the App.-short block sets 4.
    assert "max-height: 12" in css
