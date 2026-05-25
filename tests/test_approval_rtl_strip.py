"""F15: TG approval rendering must strip Cf-category Unicode from args."""
from __future__ import annotations

import inspect


def test_telegram_approval_strips_cf_chars():
    """The TG gateway must strip Cf-category Unicode before rendering args."""
    from cogitum.gateway import telegram

    # The Cf-strip lives in the event-drain helper now (extracted from
    # _run_agent during the F1 refactor). Check both so the test stays
    # accurate if it ever moves back.
    src = inspect.getsource(telegram.CogitumBot._drain_event_queue)
    src += inspect.getsource(telegram.CogitumBot._run_agent)
    assert "category(ch) != \"Cf\"" in src or 'category(ch) != "Cf"' in src, (
        "approval render must filter Cf-category Unicode out of description"
    )


def test_strip_cf_filter_logic():
    """Functional check: an RTL override mid-string is stripped."""
    import unicodedata

    raw = "rm -rf /\u202etest"
    stripped = "".join(ch for ch in raw if unicodedata.category(ch) != "Cf")
    assert "\u202e" not in stripped
    assert stripped == "rm -rf /test"


def test_strip_cf_keeps_ascii_intact():
    import unicodedata

    raw = "echo hello"
    stripped = "".join(ch for ch in raw if unicodedata.category(ch) != "Cf")
    assert stripped == raw


def test_strip_cf_handles_zwsp_in_command():
    """ZWSP between letters is invisible to operator — must be stripped."""
    import unicodedata

    raw = "r\u200bm -rf /"
    stripped = "".join(ch for ch in raw if unicodedata.category(ch) != "Cf")
    assert stripped == "rm -rf /"
