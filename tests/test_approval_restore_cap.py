"""F27 hardening: ``_restore_approval_tokens`` caps loaded entries.

Without a cap, a bloated tg_approvals.json (an attacker who can write
to the data dir, or simply a long-running bot that accumulated
thousands of stale entries) forces the OrderedDict to balloon on
every restart. We pin the contract: anything beyond
``_APPROVAL_RESTORE_CAP`` (1024) is dropped, keeping the most
recently inserted entries (JSON object order is insertion order on
Py3.7+).
"""
from __future__ import annotations

import collections
import json
import logging

import pytest


def _make_bot(tmp_path):
    from cogitum.gateway.telegram import CogitumBot

    bot = CogitumBot.__new__(CogitumBot)
    bot._approval_token_to_call_id = collections.OrderedDict()
    bot._approval_token_max = 64
    bot._approval_persist_path = tmp_path / "tg_approvals.json"
    return bot


def test_restore_under_cap_loads_all(tmp_path):
    """Below the cap → every entry must be restored."""
    bot = _make_bot(tmp_path)
    payload = {f"tok{i:04d}": f"call_{i}" for i in range(50)}
    bot._approval_persist_path.write_text(
        json.dumps(payload), encoding="utf-8",
    )
    bot._restore_approval_tokens()
    assert len(bot._approval_token_to_call_id) == 50


def test_restore_above_cap_truncates_to_cap(tmp_path, caplog):
    """Above the cap → only the last N inserted are kept, with a warning."""
    from cogitum.gateway.telegram import CogitumBot

    cap = CogitumBot._APPROVAL_RESTORE_CAP
    overflow = cap + 250

    # Insertion order: tok0000 (oldest) … tok{overflow-1} (newest).
    payload = collections.OrderedDict(
        (f"tok{i:06d}", f"call_{i}") for i in range(overflow)
    )

    bot = _make_bot(tmp_path)
    bot._approval_persist_path.write_text(
        json.dumps(payload), encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        bot._restore_approval_tokens()

    # Cap enforced.
    assert len(bot._approval_token_to_call_id) == cap

    # Newest preserved (last inserted = highest index).
    last_key = f"tok{overflow - 1:06d}"
    assert last_key in bot._approval_token_to_call_id

    # Oldest dropped.
    first_key = "tok000000"
    assert first_key not in bot._approval_token_to_call_id

    # Boundary: tok at index (overflow - cap) MUST be present (newest cap entries).
    boundary_key = f"tok{(overflow - cap):06d}"
    assert boundary_key in bot._approval_token_to_call_id

    # Boundary: tok at index (overflow - cap - 1) MUST be absent.
    just_dropped = f"tok{(overflow - cap - 1):06d}"
    assert just_dropped not in bot._approval_token_to_call_id

    # And a warning was logged so ops can see it.
    assert any(
        "exceeds cap" in rec.message or "tg_approvals" in rec.message
        for rec in caplog.records
    ), f"expected a cap-warning, got {[r.message for r in caplog.records]}"


def test_restore_skips_non_string_entries(tmp_path):
    """Type-safety: non-string keys/values must be silently skipped."""
    bot = _make_bot(tmp_path)
    payload = {
        "good": "call_x",
        "alsogood": "call_y",
    }
    raw = json.dumps(payload)
    # Manually splice a malformed entry — json.dumps won't emit non-string
    # keys, so we doctor the JSON text.
    raw_bad = raw[:-1] + ', "bad": 42}'
    bot._approval_persist_path.write_text(raw_bad, encoding="utf-8")

    bot._restore_approval_tokens()
    assert "good" in bot._approval_token_to_call_id
    assert "alsogood" in bot._approval_token_to_call_id
    assert "bad" not in bot._approval_token_to_call_id


def test_cap_constant_is_1024():
    """Pin the documented cap so changing it requires updating the test."""
    from cogitum.gateway.telegram import CogitumBot
    assert CogitumBot._APPROVAL_RESTORE_CAP == 1024
