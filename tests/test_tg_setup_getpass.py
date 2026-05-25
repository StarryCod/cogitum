"""F21: cog tg setup must read the bot token via getpass, not input.

The bot token is the bot's full credential; reading via input() echoes
plaintext to the terminal, leaks into shell history, screen capture
buffers, tmux scrollback. Fix: getpass.getpass so the token never
shows up in any of those.

We monkeypatch getpass.getpass + input + httpx + save_tg_config and
drive _tg_command(setup) directly. The test:
  - asserts getpass.getpass was called at least once
  - asserts input() was NOT called for the token (still fine for user_id)
"""
from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch


def test_tg_setup_uses_getpass_for_bot_token(monkeypatch, capsys):
    from cogitum import cli

    # Track how many times each input primitive is hit.
    getpass_calls: list[str] = []
    input_calls: list[str] = []

    def fake_getpass(prompt: str = "") -> str:
        getpass_calls.append(prompt)
        return "1234567890:ABCDEFGHIJKLMNOPQRSTUVWX_yz_"

    def fake_input(prompt: str = "") -> str:
        input_calls.append(prompt)
        # First non-token prompt is the user id; second is the
        # "start daemon now?" prompt — answer "n" to skip.
        if "user ID" in prompt:
            return "12345"
        return "n"

    monkeypatch.setattr(cli.getpass, "getpass", fake_getpass)
    monkeypatch.setattr("builtins.input", fake_input)

    # Stub out network + persistence + service start.
    fake_resp = MagicMock()
    fake_resp.json.return_value = {"ok": True, "result": {"username": "fake_bot"}}

    fake_save = MagicMock()
    with patch.object(cli, "_format_bot_token_display", return_value="fake"), \
         patch("httpx.get", return_value=fake_resp), \
         patch("cogitum.gateway.tg_config.save_tg_config", fake_save), \
         patch("cogitum.gateway.daemon.enable_service", return_value="ok"), \
         patch("cogitum.gateway.daemon.start_service", return_value="ok"):
        rc = cli._tg_command(argparse.Namespace(tg_action="setup"))

    assert rc == 0, "setup must succeed when token+id valid"
    assert getpass_calls, "bot token MUST be read via getpass.getpass"
    # The token prompt itself should NOT have been an input() call.
    for prompt in input_calls:
        assert "Bot token" not in prompt, (
            f"input() was used for bot token: {prompt!r}"
        )
    # And: the prompt label mentions 'hidden' so the user knows it
    # won't echo.
    assert any("hidden" in p.lower() for p in getpass_calls)
