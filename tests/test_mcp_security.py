"""Tests for cogitum.core.mcp.security."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from cogitum.core.mcp.security import (
    filter_env,
    redact_secrets,
    resolve_secret,
    resolve_mapping,
)


def test_filter_env_keeps_baseline(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/x")
    env = filter_env()
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/x"


def test_filter_env_drops_secrets(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-abc")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_abc")
    env = filter_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env


def test_filter_env_keeps_xdg(monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "/x/config")
    monkeypatch.setenv("XDG_DATA_HOME", "/x/data")
    env = filter_env()
    assert env["XDG_CONFIG_HOME"] == "/x/config"
    assert env["XDG_DATA_HOME"] == "/x/data"


def test_filter_env_extra_passes_through(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")
    env = filter_env({"GITHUB_TOKEN": "explicit-value"})
    assert env["GITHUB_TOKEN"] == "explicit-value"
    assert "ANTHROPIC_API_KEY" not in env


def test_resolve_secret_passthrough():
    assert resolve_secret("plain") == "plain"


def test_resolve_secret_env(monkeypatch):
    monkeypatch.setenv("MY_KEY", "secret-value")
    assert resolve_secret("env:MY_KEY") == "secret-value"


def test_resolve_secret_env_missing(monkeypatch):
    monkeypatch.delenv("DOES_NOT_EXIST", raising=False)
    with pytest.raises(KeyError):
        resolve_secret("env:DOES_NOT_EXIST")


def test_resolve_secret_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    secrets = tmp_path / "cogitum" / "secrets.env"
    secrets.parent.mkdir(parents=True)
    secrets.write_text('MY_KEY="hello"\nOTHER=plain\n', encoding="utf-8")
    assert resolve_secret("vault:MY_KEY") == "hello"
    assert resolve_secret("vault:OTHER") == "plain"


def test_resolve_secret_vault_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(KeyError):
        resolve_secret("vault:NOPE")


def test_resolve_mapping():
    assert resolve_mapping({"a": "x", "b": "y"}) == {"a": "x", "b": "y"}


def test_redact_bearer():
    out = redact_secrets("Authorization: Bearer ghp_abc123def456ghi789jklmnop")
    assert "ghp_" not in out or "ghp_***" in out
    assert "Bearer ***" in out or "Bearer " not in out


def test_redact_openai_key():
    out = redact_secrets("Failed with key sk-1234567890abcdefghij not valid")
    assert "sk-1234567890abcdefghij" not in out
    assert "sk-***" in out


def test_redact_anthropic_key():
    out = redact_secrets("ANTHROPIC sk-ant-1234567890abcdefghij here")
    assert "sk-ant-1234567890abcdefghij" not in out


def test_redact_aws():
    out = redact_secrets("AKIAIOSFODNN7EXAMPLE creds")
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "AKIA***" in out


def test_redact_generic_password():
    out = redact_secrets('error: password="secret123" failed')
    assert "secret123" not in out


def test_redact_idempotent():
    text = "no secrets here"
    assert redact_secrets(text) == text


def test_redact_handles_non_string():
    assert redact_secrets(None) is None  # type: ignore[arg-type]
