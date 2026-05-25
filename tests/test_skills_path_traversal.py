"""
Path-traversal / glob-injection guard for cogitum.core.skills.read_skill.

Hardening rules:
  - reject names containing path separators, parent traversal, glob
    metachars, or control chars
  - reject matches whose realpath escapes the skills root via symlink
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


def _isolate(tmp_path, monkeypatch):
    """Point COGITUM_DATA_DIR (and friends) at a tmp dir, reload skills."""
    monkeypatch.setenv("COGITUM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COGITUM_CONFIG_DIR", str(tmp_path / "cfg"))
    import cogitum.core.platform_paths as pp
    importlib.reload(pp)
    import cogitum.core.skills as sk
    importlib.reload(sk)
    return sk


def _seed_skill(skills_dir: Path, category: str, name: str, body: str) -> Path:
    p = skills_dir / category / name / "SKILL.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


@pytest.mark.parametrize("bad", [
    "../../etc/passwd",
    "../foo",
    "*/../*",
    "abc*",
    "abc?def",
    "[abc]",
    "foo\\bar",
    "foo/bar",
    "ctrl\x01char",
    "..",
])
def test_read_skill_rejects_unsafe_names(tmp_path, monkeypatch, bad, caplog):
    sk = _isolate(tmp_path, monkeypatch)
    # Seed at least one valid skill so the dir exists.
    _seed_skill(sk._SKILLS_DIR, "cat", "good", "ok body")
    caplog.set_level("WARNING", logger="cogitum.core.skills")
    out = sk.read_skill(bad)
    assert out is None, f"unsafe name {bad!r} must return None, got {out!r}"
    # Ensure the warning was logged.
    msgs = [r.getMessage() for r in caplog.records]
    assert any("rejected unsafe name" in m for m in msgs), msgs


def test_read_skill_blocks_symlink_escape(tmp_path, monkeypatch, caplog):
    sk = _isolate(tmp_path, monkeypatch)
    # Create a victim file outside the skills dir.
    victim = tmp_path / "outside_secret.md"
    victim.write_text("SECRET", encoding="utf-8")

    # Seed a skill dir, then replace SKILL.md with a symlink pointing
    # outside the skills root.
    skill_dir = sk._SKILLS_DIR / "cat" / "evil"
    skill_dir.mkdir(parents=True, exist_ok=True)
    link = skill_dir / "SKILL.md"
    if link.exists() or link.is_symlink():
        link.unlink()
    try:
        os.symlink(victim, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this filesystem")

    caplog.set_level("WARNING", logger="cogitum.core.skills")
    out = sk.read_skill("evil")
    # Either rejected outright (None) or — if the implementation falls
    # back to fuzzy match on a different path — never returns the
    # secret content.
    assert out is None or "SECRET" not in out


def test_read_skill_returns_legit_content(tmp_path, monkeypatch):
    sk = _isolate(tmp_path, monkeypatch)
    _seed_skill(sk._SKILLS_DIR, "cat", "good", "legit body content")
    out = sk.read_skill("good")
    assert out == "legit body content"


def test_read_skill_safe_alphanumeric_passes(tmp_path, monkeypatch):
    sk = _isolate(tmp_path, monkeypatch)
    _seed_skill(sk._SKILLS_DIR, "cat", "safe-name_123", "ok")
    assert sk.read_skill("safe-name_123") == "ok"
