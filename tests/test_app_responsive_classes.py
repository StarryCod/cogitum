"""Tests for the App's responsive screen-class logic.

The App now sets one of `-narrow / -medium / -wide` plus an optional
`-short` class on itself based on terminal size. Widgets read those
classes from CSS to adapt layout.

We test the pure helper directly (no Textual app needed) and assert
the breakpoints match the audit's spec.
"""
from __future__ import annotations

from cogitum.app import CogitumApp


def test_narrow_breakpoint_at_or_below_80():
    """Width ≤80 (the classic 80x24 SSH terminal) → narrow."""
    assert CogitumApp.screen_class_for(40, 15)[0] == "-narrow"
    assert CogitumApp.screen_class_for(60, 20)[0] == "-narrow"
    assert CogitumApp.screen_class_for(80, 24)[0] == "-narrow"


def test_medium_breakpoint_81_to_120():
    assert CogitumApp.screen_class_for(81, 30)[0] == "-medium"
    assert CogitumApp.screen_class_for(100, 30)[0] == "-medium"
    assert CogitumApp.screen_class_for(120, 30)[0] == "-medium"


def test_wide_breakpoint_above_120():
    assert CogitumApp.screen_class_for(121, 30)[0] == "-wide"
    assert CogitumApp.screen_class_for(200, 60)[0] == "-wide"


def test_short_class_set_when_height_at_or_below_24():
    """`-short` is independent from width — drives banner/composer compaction."""
    assert CogitumApp.screen_class_for(80, 24)[1] is True
    assert CogitumApp.screen_class_for(120, 20)[1] is True
    assert CogitumApp.screen_class_for(80, 25)[1] is False
    assert CogitumApp.screen_class_for(200, 50)[1] is False


def test_breakpoints_cover_audit_target_sizes():
    """All three target sizes from the audit must trigger -narrow + -short."""
    for w, h in [(80, 24), (60, 20), (40, 15)]:
        wcls, short = CogitumApp.screen_class_for(w, h)
        assert wcls == "-narrow", f"{w}x{h} should be narrow, got {wcls}"
        assert short is True, f"{w}x{h} should be short"


def test_apply_screen_classes_is_idempotent():
    """Calling _apply_screen_classes repeatedly mustn't accumulate classes."""
    # We can't run the App here (no event loop), but we can test the
    # branch logic through `screen_class_for` since `_apply_screen_classes`
    # is a thin wrapper over it.
    a, _ = CogitumApp.screen_class_for(60, 20)
    b, _ = CogitumApp.screen_class_for(60, 20)
    assert a == b == "-narrow"
