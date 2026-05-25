"""Tests for the feed _truncate / _truncate_for_screen helpers.

Audit fix #8: every ToolCallCard body had hardcoded `[:50]`, `[:65]`,
`[:78]`, `[:80]` truncations regardless of terminal width. We replaced
them with one helper that scales with `app.size.width`.
"""
from __future__ import annotations

from cogitum.widgets.feed import _truncate, _truncate_for_screen


# ── _truncate (pure) ─────────────────────────────────────────────────────

def test_truncate_passthrough_for_short_text():
    assert _truncate("hi", 99) == "hi"


def test_truncate_replaces_tail_with_ellipsis():
    assert _truncate("hello world", 5) == "hell…"


def test_truncate_handles_empty_string():
    assert _truncate("", 5) == ""
    assert _truncate(None, 5) == ""  # type: ignore[arg-type]


def test_truncate_zero_max_returns_empty():
    assert _truncate("hello", 0) == ""


def test_truncate_one_returns_only_marker():
    assert _truncate("hello", 1) == "…"


def test_truncate_negative_max_treated_as_zero():
    assert _truncate("hello", -3) == ""


# ── _truncate_for_screen (app-aware) ─────────────────────────────────────

class _FakeApp:
    """Stand-in for a Textual App with a known size."""
    def __init__(self, width: int) -> None:
        class _S:
            pass
        self.size = _S()
        self.size.width = width


def test_truncate_for_screen_no_app_uses_base_width():
    """No app reachable → base_width is used as the cap."""
    long_text = "x" * 200
    out = _truncate_for_screen(long_text, base_width=80, app=None)
    assert len(out) == 80


def test_truncate_for_screen_scales_down_on_narrow_terminal():
    """40-col terminal → 0.5x scaling → 40-char base instead of 80."""
    long_text = "x" * 200
    out = _truncate_for_screen(long_text, base_width=80, app=_FakeApp(40))
    # 80 * (40/80) = 40 chars
    assert len(out) == 40


def test_truncate_for_screen_scales_up_on_wide_terminal():
    """200-col terminal → 2.5x → 200 chars (since text is exactly 200)."""
    long_text = "x" * 250
    out = _truncate_for_screen(long_text, base_width=80, app=_FakeApp(200))
    # 80 * (200/80) = 200 chars
    assert len(out) == 200


def test_truncate_for_screen_caps_factor_at_4x():
    """Absurdly wide terminals don't get an unbounded budget."""
    long_text = "x" * 1000
    out = _truncate_for_screen(long_text, base_width=80, app=_FakeApp(2000))
    # Capped at 4x → 320
    assert len(out) == 320


def test_truncate_for_screen_floor_at_8_chars():
    """Even 1-col terminal can't truncate below 8 chars."""
    out = _truncate_for_screen("aaaaaaaaaaaaaaaaaaaaaa", base_width=80, app=_FakeApp(1))
    # Width is clamped to 20 internally, then 80*(20/80)=20, but check floor still works
    assert len(out) >= 8


def test_truncate_for_screen_short_text_passes_through():
    """Text already under the budget is returned untouched."""
    out = _truncate_for_screen("short", base_width=80, app=_FakeApp(40))
    assert out == "short"


def test_truncate_for_screen_app_with_no_size_falls_back():
    """Bad app object with no `.size` attr → falls back to base_width."""
    class _Broken:
        pass
    out = _truncate_for_screen("x" * 200, base_width=50, app=_Broken())
    assert len(out) == 50
