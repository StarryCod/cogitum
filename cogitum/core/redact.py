"""Shared token-redaction helpers.

Centralises the bot-token display format used by CLI status output and
the setup wizard so both surfaces stay in lockstep. Keeping this in a
single module also means future scrubbing changes (e.g. moving from
``bot_id`` to a fully opaque hash) are one-edit affairs.
"""

from __future__ import annotations


def format_bot_token_display(bot_token: str | None) -> str:
    """Render a Telegram bot token for human-readable status output.

    The legacy ``token[:8]...token[-4:]`` form leaks the first 8 chars
    (= the public numeric bot id) AND the trailing 4 chars of the
    secret half. Surface the public numeric id only and redact the
    rest.

    Format: ``<bot_id> (token redacted)`` or ``? (token redacted)``
    when the colon separator is missing (malformed config).
    """
    if not bot_token:
        return "(unset)"
    bot_id = bot_token.split(":")[0] if ":" in bot_token else "?"
    return f"{bot_id} (token redacted)"
