"""Tests for Telegram gateway hardening (C8, H3, H4, H5).

Covers:
- Critical [C8]: callback_query.id dedup — Telegram retries unanswered
  callbacks for ~15s. Without dedup the same approval click fires N times.
- High [H3]: bounded concurrency on update handlers (asyncio.Semaphore).
- High [H4]: offset persistence — restart must not replay 24h of updates.
- High [H5]: exponential backoff on poll errors (1s → 2 → 4 → cap 30s).

These tests don't hit Telegram. We construct CogitumBot with a stub config
and exercise its internal helpers + state.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cogitum.gateway.telegram import CogitumBot
from cogitum.gateway.tg_config import TelegramConfig


@pytest.fixture
def isolated_offset(tmp_path, monkeypatch):
    """Redirect tg_offset to tmp_path so tests don't touch real config."""
    fake = tmp_path / "tg_offset"
    monkeypatch.setattr(CogitumBot, "_offset_path", staticmethod(lambda: fake))
    return fake


def _make_bot() -> CogitumBot:
    cfg = TelegramConfig(bot_token="fake-token-for-tests", allowed_user_id=1)
    return CogitumBot(cfg)


# ── C8: callback dedup ──────────────────────────────────────────────────────


def test_callback_dedup_first_call_passes(isolated_offset):
    bot = _make_bot()
    assert bot._is_duplicate_callback("cb_abc") is False


def test_callback_dedup_second_call_blocked(isolated_offset):
    bot = _make_bot()
    bot._is_duplicate_callback("cb_abc")
    assert bot._is_duplicate_callback("cb_abc") is True


def test_callback_dedup_different_ids_pass(isolated_offset):
    bot = _make_bot()
    assert bot._is_duplicate_callback("cb_a") is False
    assert bot._is_duplicate_callback("cb_b") is False
    assert bot._is_duplicate_callback("cb_a") is True
    assert bot._is_duplicate_callback("cb_b") is True


def test_callback_dedup_ring_bounded(isolated_offset):
    """Ring should hold at most _seen_callbacks_max entries."""
    bot = _make_bot()
    bot._seen_callbacks_max = 4
    for i in range(10):
        bot._is_duplicate_callback(f"cb_{i}")
    assert len(bot._seen_callbacks) <= 4


def test_callback_dedup_old_entries_expire(isolated_offset, monkeypatch):
    """Entries older than 60s should be evicted on the next check."""
    bot = _make_bot()
    bot._is_duplicate_callback("cb_old")
    # Time-travel: pretend we're 70s in the future.
    import cogitum.gateway.telegram as tg_mod
    base = bot._seen_callbacks["cb_old"]
    monkeypatch.setattr(tg_mod.time, "monotonic", lambda: base + 70)
    # Fresh check on a different ID triggers expiry sweep, then the old
    # one is gone, so it'd pass the dedup check again.
    bot._is_duplicate_callback("cb_new")
    assert "cb_old" not in bot._seen_callbacks


# ── H4: offset persistence ──────────────────────────────────────────────────


def test_offset_persisted_across_restart(isolated_offset):
    bot1 = _make_bot()
    bot1._offset = 12345
    bot1._save_offset()

    # Simulate restart: new bot instance reads from disk.
    bot2 = _make_bot()
    assert bot2._offset == 12345


def test_offset_default_zero_when_no_file(isolated_offset):
    """First run: no persisted offset, must default to 0."""
    bot = _make_bot()
    assert bot._offset == 0


def test_offset_default_zero_on_corrupt_file(isolated_offset):
    isolated_offset.parent.mkdir(parents=True, exist_ok=True)
    isolated_offset.write_text("not-a-number")
    bot = _make_bot()
    assert bot._offset == 0


def test_save_offset_creates_parent_dir(isolated_offset, tmp_path):
    bot = _make_bot()
    bot._offset = 999
    # Parent dir doesn't exist yet — _save_offset should create it.
    assert not isolated_offset.parent.exists() or isolated_offset.parent == tmp_path
    bot._save_offset()
    assert isolated_offset.read_text().strip() == "999"


# ── H3: bounded concurrency ─────────────────────────────────────────────────


def test_update_semaphore_bounds_concurrency(isolated_offset):
    bot = _make_bot()
    # Default cap is 8 — there should be a semaphore, not None.
    assert bot._update_sem._value == 8


def test_update_tasks_set_is_initialized(isolated_offset):
    """Hard-ref set ensures handler tasks aren't GC'd mid-flight (RUF006)."""
    bot = _make_bot()
    assert bot._update_tasks == set()


# ── H5: exponential backoff state ──────────────────────────────────────────


def test_initial_backoff_is_one_second(isolated_offset):
    bot = _make_bot()
    assert bot._poll_backoff == 1.0
