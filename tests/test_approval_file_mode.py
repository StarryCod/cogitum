"""F27 hardening: persisted approval-token map must be mode 0o600.

The file leaks live tool-call ids of an active agent — an attacker on
the same host could replay them against the future-table inside the
approval window. We force 0o600 right after the atomic write so the
persisted file isn't world-/group-readable.
"""
from __future__ import annotations

import collections
import os
import stat
import sys

import pytest


def _make_bot(tmp_path):
    from cogitum.gateway.telegram import CogitumBot

    bot = CogitumBot.__new__(CogitumBot)
    bot._approval_token_to_call_id = collections.OrderedDict()
    bot._approval_token_max = 64
    bot._approval_persist_path = tmp_path / "tg_approvals.json"
    return bot


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="POSIX file modes — Windows chmod has no useful semantics here",
)
def test_save_approval_tokens_sets_mode_0600(tmp_path):
    """After save, the file's permission bits must be exactly 0o600."""
    bot = _make_bot(tmp_path)
    bot._approval_token_to_call_id["tok"] = "call_x"

    bot._save_approval_tokens_sync()

    path = bot._approval_persist_path
    assert path.exists()
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="POSIX file modes only",
)
def test_save_approval_tokens_resets_mode_on_overwrite(tmp_path):
    """Overwriting via atomic_write_text + chmod must always end at 0o600
    even if a prior version of the file had laxer permissions (the
    atomic rename on POSIX preserves the new file's mode, but we still
    chmod after to be defensive)."""
    bot = _make_bot(tmp_path)
    bot._approval_token_to_call_id["tok"] = "call_x"
    bot._save_approval_tokens_sync()

    # Loosen perms manually, then save again.
    os.chmod(bot._approval_persist_path, 0o644)

    bot._approval_token_to_call_id["tok2"] = "call_y"
    bot._save_approval_tokens_sync()

    mode = stat.S_IMODE(os.stat(bot._approval_persist_path).st_mode)
    assert mode == 0o600, f"expected 0o600 after re-save, got 0o{mode:o}"
