"""Tier-4 R2: crash-safe atomic_write_text.

The old ``Path.write_text`` path could leave the index file
zero-sized if the process crashed between truncate-on-open and final
flush — wiping every session's metadata. The new helper writes to a
sibling ``.tmp`` and uses ``os.replace`` for atomicity, plus a parent
directory fsync so the rename itself is durable across power loss.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from cogitum.core.atomic_io import atomic_write_text


def test_atomic_write_creates_target(tmp_path):
    target = tmp_path / "index.json"
    atomic_write_text(target, '{"ok": 1}')
    assert target.read_text() == '{"ok": 1}'


def test_atomic_write_overwrites_existing(tmp_path):
    target = tmp_path / "index.json"
    target.write_text("OLD")
    atomic_write_text(target, "NEW")
    assert target.read_text() == "NEW"


def test_atomic_write_no_stale_tmp_left_on_success(tmp_path):
    target = tmp_path / "index.json"
    atomic_write_text(target, "payload")
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_preserves_original_on_crash(tmp_path, monkeypatch):
    """If the write step raises mid-flight, the target file must keep
    its previous content. The crash-window is the dangerous region we
    care about — ``Path.write_text`` truncates on open which is exactly
    the failure mode we lost data to before."""
    target = tmp_path / "index.json"
    target.write_text("ORIGINAL")

    real_replace = os.replace

    def boom_replace(*a, **kw):
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(os, "replace", boom_replace)

    with pytest.raises(OSError, match="simulated"):
        atomic_write_text(target, "would-be-new")

    # Original survives, no stale tmp lingers.
    monkeypatch.setattr(os, "replace", real_replace)
    assert target.read_text() == "ORIGINAL"
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_creates_parent_dirs(tmp_path):
    target = tmp_path / "deep" / "nested" / "index.json"
    atomic_write_text(target, "ok")
    assert target.read_text() == "ok"


def test_atomic_write_fsync_dir_is_best_effort(tmp_path, monkeypatch):
    """If parent-dir fsync fails (NFS, exotic FS, Windows), the write
    must still succeed — durability is downgraded but data isn't lost."""
    if not hasattr(os, "O_DIRECTORY"):
        pytest.skip("POSIX-only path")

    real_fsync = os.fsync
    fsync_calls = {"count": 0}

    def maybe_fail(fd):
        fsync_calls["count"] += 1
        # Fail only on directory fsync (the second call); first one is
        # the file-content fsync which must pass for atomicity.
        if fsync_calls["count"] >= 2:
            raise OSError("simulated dir fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", maybe_fail)

    target = tmp_path / "index.json"
    # Must not raise.
    atomic_write_text(target, "payload")
    assert target.read_text() == "payload"
