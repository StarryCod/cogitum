"""F10: send_media must refuse to ship sensitive files to Telegram.

Without the path-sandbox check, a misled LLM could be coerced into
calling ``send_media('/home/user/.config/cogitum/auth.json')`` and
exfiltrate OAuth tokens to whoever is on the other end of the bot.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


class _FakeTGAPI:
    """Minimal stub — send_media must not even reach this on a sensitive path."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_document(self, chat_id, path, caption=""):
        self.sent.append(("doc", path))
        return {"ok": True}

    async def send_photo(self, chat_id, path, caption=""):
        self.sent.append(("photo", path))
        return {"ok": True}


def test_send_media_blocks_auth_json(tmp_path, monkeypatch):
    """auth.json under HOME's config dir must be rejected."""
    fake_home = tmp_path / "home"
    (fake_home / ".config" / "cogitum").mkdir(parents=True)
    auth_file = fake_home / ".config" / "cogitum" / "auth.json"
    auth_file.write_text('{"openrouter": {"access_token": "sk-fake"}}')
    monkeypatch.setenv("HOME", str(fake_home))

    from cogitum.core.builtin_tools import _set_tg_context, send_media

    api = _FakeTGAPI()
    tokens = _set_tg_context(api, chat_id=12345)
    try:
        result = asyncio.run(send_media(str(auth_file)))
    finally:
        from cogitum.core.builtin_tools import _clear_tg_context
        _clear_tg_context(tokens)

    assert "ERROR" in result, f"expected rejection, got: {result!r}"
    assert "denied" in result or "sensitive" in result, result
    assert api.sent == [], "auth.json must NOT be transmitted"


def test_send_media_blocks_netrc(tmp_path, monkeypatch):
    """~/.netrc carries cleartext credentials — block."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    netrc = fake_home / ".netrc"
    netrc.write_text("machine api.example.com login me password secret\n")
    monkeypatch.setenv("HOME", str(fake_home))

    from cogitum.core.builtin_tools import _set_tg_context, _clear_tg_context, send_media

    api = _FakeTGAPI()
    tokens = _set_tg_context(api, chat_id=1)
    try:
        result = asyncio.run(send_media(str(netrc)))
    finally:
        _clear_tg_context(tokens)

    assert "ERROR" in result and ("denied" in result or "sensitive" in result)


def test_send_media_classifier_floors_at_medium():
    """classify_danger(send_media) must be at least medium — operator approval."""
    from cogitum.core.builtin_tools import classify_danger

    risk = classify_danger("send_media", {"path": "/tmp/innocuous.png"})
    assert risk in ("medium", "danger"), f"send_media must require approval; got {risk}"


def test_send_media_allows_innocuous_file(tmp_path, monkeypatch):
    """Sanity: a normal /tmp file should still go through."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    target = tmp_path / "ok.txt"
    target.write_text("hi")

    from cogitum.core.builtin_tools import _set_tg_context, _clear_tg_context, send_media

    api = _FakeTGAPI()
    tokens = _set_tg_context(api, chat_id=1)
    try:
        result = asyncio.run(send_media(str(target)))
    finally:
        _clear_tg_context(tokens)

    # Either Sent (real path went through) or another non-sandbox error,
    # but NOT a sandbox rejection.
    assert "denied" not in result and "sensitive" not in result, result
