"""
Warning surface when a `plain:` secret_ref is written.

Two paths are checked:
  1. resolve_secret_ref('plain:foo') logs a WARNING via cogitum.core.llm.discovery
  2. cli._patch_provider_secret(provider, 'plain:...') logs a WARNING via cogitum.cli
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest


def test_resolve_secret_ref_plain_logs_warning(caplog):
    from cogitum.core.llm.discovery import resolve_secret_ref

    caplog.set_level(logging.WARNING, logger="cogitum.core.llm.discovery")
    val = resolve_secret_ref("plain:my-key-value")
    assert val == "my-key-value"
    msgs = [r.getMessage() for r in caplog.records]
    # Expect a clear, actionable hint mentioning safer schemes.
    assert any("plain:" in m for m in msgs), msgs
    assert any("env:" in m or "keyring:" in m for m in msgs), msgs


def test_resolve_secret_ref_env_no_warning(caplog, monkeypatch):
    from cogitum.core.llm.discovery import resolve_secret_ref

    monkeypatch.setenv("MY_GREEN_KEY", "ok")
    caplog.set_level(logging.WARNING, logger="cogitum.core.llm.discovery")
    val = resolve_secret_ref("env:MY_GREEN_KEY")
    assert val == "ok"
    msgs = [r.getMessage() for r in caplog.records]
    # Should NOT warn about plain: usage when scheme is env:
    assert not any("plain:" in m for m in msgs), msgs


def test_patch_provider_secret_warns_on_plain(tmp_path, monkeypatch, caplog, capsys):
    """The CLI helper that writes providers.toml must warn on plain:."""
    # Isolate providers.toml location.
    providers_dir = tmp_path / "cfg" / "cogitum"
    providers_dir.mkdir(parents=True, exist_ok=True)
    providers_path = providers_dir / "providers.toml"
    providers_path.write_text(
        '[providers.openai.keys.k1]\nsecret_ref = "env:OPENAI_API_KEY"\n',
        encoding="utf-8",
    )

    # Patch the cli module's _PROVIDERS_PATH constant.
    import cogitum.cli as cli_mod
    monkeypatch.setattr(cli_mod, "_PROVIDERS_PATH", providers_path)

    caplog.set_level(logging.WARNING, logger="cogitum.cli")
    cli_mod._patch_provider_secret("openai", "plain:hunter2")

    # Stdout warning is unmissable.
    out = capsys.readouterr().out
    assert "PLAINTEXT" in out

    # Logger warning fired.
    msgs = [r.getMessage() for r in caplog.records]
    assert any("plain:" in m.lower() for m in msgs), msgs

    # File actually got the new value.
    body = providers_path.read_text(encoding="utf-8")
    assert "plain:hunter2" in body
