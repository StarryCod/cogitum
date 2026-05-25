"""F18: _load_offset corruption recovery — assert sentinel + log warning.

Without these tests, a corrupt tg_offset file silently coerces to 0 and
Telegram replays up to 24h of buffered updates after every restart.
"""
from __future__ import annotations

import logging

import pytest

from cogitum.gateway.telegram import CogitumBot
from cogitum.gateway.tg_config import TelegramConfig


@pytest.fixture
def isolated_offset(tmp_path, monkeypatch):
    fake = tmp_path / "tg_offset"
    monkeypatch.setattr(CogitumBot, "_offset_path", staticmethod(lambda: fake))
    return fake


def _make_bot() -> CogitumBot:
    return CogitumBot(TelegramConfig(bot_token="fake", allowed_user_id=1))


def test_load_offset_returns_zero_when_no_file(isolated_offset):
    """Fresh install: file doesn't exist → 0 (replay-safe baseline)."""
    bot = _make_bot()
    assert bot._offset == 0


def test_load_offset_returns_persisted_value(isolated_offset):
    isolated_offset.write_text("9876")
    bot = _make_bot()
    assert bot._offset == 9876


def test_load_offset_corrupt_file_returns_sentinel(isolated_offset, caplog):
    """ValueError on int(): emit warning AND return -1 sentinel."""
    isolated_offset.write_text("not-a-number-xyz")
    with caplog.at_level(logging.WARNING, logger="cogitum.gateway.telegram"):
        bot = _make_bot()
    assert bot._offset == -1
    # Warning surfaces the path and the exception type for ops debugging.
    msgs = " ".join(rec.message for rec in caplog.records)
    assert "tg_offset" in msgs
    assert "ValueError" in msgs


def test_load_offset_empty_file_returns_sentinel(isolated_offset, caplog):
    """Empty file → int('') ValueError → -1 sentinel + warning."""
    isolated_offset.write_text("")
    with caplog.at_level(logging.WARNING, logger="cogitum.gateway.telegram"):
        bot = _make_bot()
    assert bot._offset == -1


def test_load_offset_unreadable_returns_sentinel(isolated_offset, monkeypatch, caplog):
    """OSError on read → -1 sentinel + warning (not silent 0)."""
    isolated_offset.write_text("12345")

    real_read_text = type(isolated_offset).read_text

    def boom(self, *a, **kw):
        if str(self) == str(isolated_offset):
            raise PermissionError("simulated EACCES")
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(type(isolated_offset), "read_text", boom)
    with caplog.at_level(logging.WARNING, logger="cogitum.gateway.telegram"):
        bot = _make_bot()
    assert bot._offset == -1
    msgs = " ".join(rec.message for rec in caplog.records)
    assert "PermissionError" in msgs or "OSError" in msgs


def test_load_offset_no_file_does_not_log(isolated_offset, caplog):
    """First-run path is not noisy — silent zero is correct here."""
    with caplog.at_level(logging.WARNING, logger="cogitum.gateway.telegram"):
        bot = _make_bot()
    assert bot._offset == 0
    # No "tg_offset" warnings on the happy first-run path.
    for rec in caplog.records:
        assert "tg_offset" not in rec.message
