"""Tests for the responsive StatusBar.

Audit fix #4: the bar built a single Rich Text containing 5 keys + a
verbose «model: <id>» + a channel breadcrumb, easily 100+ chars. On
80-col terminals the right side clipped without an `…` indicator.

We added `_format_full()` and `_format_compact()`. The render path
picks the compact variant when the App carries the `-narrow` class.
"""
from __future__ import annotations

from cogitum.widgets.statusbar import StatusBar, _ellipsize, _KEYS, _KEYS_COMPACT


def test_ellipsize_truncates_with_marker():
    assert _ellipsize("abcdef", 4) == "abc…"


def test_ellipsize_passthrough_when_short_enough():
    assert _ellipsize("abc", 5) == "abc"


def test_ellipsize_zero_max():
    assert _ellipsize("abcdef", 0) == ""


def test_ellipsize_one_char_returns_marker_only():
    assert _ellipsize("abcdef", 1) == "…"


def test_full_format_includes_all_five_keys():
    """Full formatting carries the entire key list."""
    sb = StatusBar(model="m")
    plain = sb._format_full().plain
    for label in ("send", "stop", "models", "setup", "quit"):
        assert label in plain


def test_compact_format_drops_verbose_keys():
    """Compact form only keeps send / stop / quit."""
    sb = StatusBar(model="m")
    plain = sb._format_compact().plain
    assert "models" not in plain
    assert "setup" not in plain
    # Still keeps the essentials
    assert "send" in plain
    assert "quit" in plain


def test_compact_format_does_not_include_channel():
    """The 'local' breadcrumb is dropped on narrow screens."""
    sb = StatusBar(model="m", channel="local")
    plain = sb._format_compact().plain
    assert "local" not in plain


def test_compact_format_truncates_long_model_id(monkeypatch):
    """A 30+ char model id is elided to fit the compact budget."""
    sb = StatusBar(model="canopywave/moonshotai/kimi-k2.6")
    # `size` is a Textual property — patch the underlying attribute via
    # a fake `Size` object on the type. We just need `.width` to work.
    class _FakeSize:
        width = 40
    monkeypatch.setattr(type(sb), "size", _FakeSize(), raising=False)
    plain = sb._format_compact().plain
    # Should not contain the FULL id when budget is small
    assert len(plain) <= 50
    # The model name should be elided with `…`
    assert "…" in plain


def test_compact_keyset_is_subset_of_full():
    """Every compact key must exist in the full keyset (no drift)."""
    full_codes = {k for k, _ in _KEYS}
    compact_codes = {k for k, _ in _KEYS_COMPACT}
    assert compact_codes.issubset(full_codes)
