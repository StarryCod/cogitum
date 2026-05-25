"""Tier-4 R2: shared bot-token redaction helper.

The legacy ``bot_token[:8]...bot_token[-4:]`` form leaked the trailing
4 chars of the secret half across CLI status, setup wizard, and any
other surface that wanted to show "which bot is this?". The fix moves
the formatter to ``cogitum.core.redact`` so both surfaces share one
implementation.
"""
from __future__ import annotations

from cogitum.core.redact import format_bot_token_display


def test_redact_real_token_keeps_only_bot_id():
    # Telegram tokens look like '<bot_id>:<secret>'. Bot id is public.
    out = format_bot_token_display("1234567890:AAGabcdef0123456789-XYZ")
    assert "1234567890" in out
    assert "AAGabcdef" not in out
    assert "XYZ" not in out
    assert "redacted" in out.lower()


def test_redact_empty_token_is_unset():
    assert format_bot_token_display("") == "(unset)"
    assert format_bot_token_display(None) == "(unset)"


def test_redact_malformed_token_no_colon_returns_question_mark():
    out = format_bot_token_display("abcdef-no-colon-here")
    # No bot id can be extracted — the whole token is potentially secret,
    # so we must never echo any of it back. '?' as a placeholder.
    assert "abcdef" not in out
    assert out.startswith("?")
    assert "redacted" in out.lower()


def test_setup_flow_uses_shared_helper():
    """Quality drift guard: setup_flow must import from core.redact, not
    rebuild a local 'token[:8]...token[-4:]' shim. Catches a future
    refactor that re-introduces the leak."""
    from pathlib import Path

    src = Path("cogitum/setup_flow.py").read_text()
    # The legacy slice pattern must NOT be present anywhere in setup_flow.
    assert "bot_token[:8]" not in src
    assert "bot_token[-4:]" not in src
    # And the shared helper must be imported.
    assert "format_bot_token_display" in src
