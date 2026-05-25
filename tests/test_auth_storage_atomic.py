"""F11+F19: auth.json must be created with mode 0600 atomically.

Goals:
  * The new file's inode mode is exactly 0o600 right after write — no
    chmod-after window where a colocated reader could open it at the
    default 0o644 mode.
  * The parent dir is 0o700.
  * Writes are atomic — interrupted writes don't leak a half-written
    file at the canonical path.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes only")
def test_auth_json_is_0600(tmp_path, monkeypatch):
    """auth.json must end up with exactly mode 0o600."""
    monkeypatch.setenv("COGITUM_CONFIG_DIR", str(tmp_path))
    # Force re-import so storage picks up the env-driven config dir.
    for mod in list(sys.modules):
        if mod.startswith("cogitum"):
            sys.modules.pop(mod, None)

    from cogitum.core.auth import storage
    from cogitum.core.auth.types import OAuthCredentials

    creds = OAuthCredentials(
        access="secret-abc",
        refresh="r-xyz",
        expires=0.0,
    )
    storage.set_("test", creds)

    assert storage._AUTH_PATH.exists()
    mode = storage._AUTH_PATH.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes only")
def test_config_dir_is_0700(tmp_path, monkeypatch):
    monkeypatch.setenv("COGITUM_CONFIG_DIR", str(tmp_path))
    for mod in list(sys.modules):
        if mod.startswith("cogitum"):
            sys.modules.pop(mod, None)

    from cogitum.core.auth import storage
    from cogitum.core.auth.types import OAuthCredentials

    creds = OAuthCredentials(
        access="x",
        refresh="",
        expires=0.0,
    )
    storage.set_("t", creds)

    mode = storage._CONFIG_DIR.stat().st_mode & 0o777
    assert mode == 0o700, f"expected 0o700, got {oct(mode)}"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes only")
def test_auth_json_round_trip(tmp_path, monkeypatch):
    """Read-back must yield the same data."""
    monkeypatch.setenv("COGITUM_CONFIG_DIR", str(tmp_path))
    for mod in list(sys.modules):
        if mod.startswith("cogitum"):
            sys.modules.pop(mod, None)

    from cogitum.core.auth import storage
    from cogitum.core.auth.types import OAuthCredentials

    creds = OAuthCredentials(
        access="tok-1",
        refresh="ref-1",
        expires=0.0,
    )
    storage.set_("rt", creds)
    got = storage.get("rt")
    assert got is not None
    assert got.access == "tok-1"
    assert got.refresh == "ref-1"


def test_auth_json_no_lingering_tmp(tmp_path, monkeypatch):
    """After a successful write the tmp file must be gone."""
    monkeypatch.setenv("COGITUM_CONFIG_DIR", str(tmp_path))
    for mod in list(sys.modules):
        if mod.startswith("cogitum"):
            sys.modules.pop(mod, None)

    from cogitum.core.auth import storage
    from cogitum.core.auth.types import OAuthCredentials

    creds = OAuthCredentials(
        access="x",
        refresh="",
        expires=0.0,
    )
    storage.set_("t2", creds)

    leftovers = list(tmp_path.glob("auth.json.*.tmp"))
    assert leftovers == [], f"tmp file should be cleaned up: {leftovers}"
