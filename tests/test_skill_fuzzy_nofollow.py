"""F7: read_skill fuzzy fallback must reject symlink attacks.

A skill name fuzzy-matches a candidate file. Without NOFOLLOW, an
attacker could plant ``red-tea.md → /etc/shadow`` and a fuzzy search
for ``red`` would happily resolve and read the target.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform == "win32", reason="symlink test is POSIX")
def test_fuzzy_fallback_rejects_symlink(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    # Force the module to recompute the data dir.
    for mod in list(sys.modules):
        if mod.startswith("cogitum"):
            sys.modules.pop(mod, None)

    from cogitum.core import skills as skills_mod

    # Override _SKILLS_DIR to our tmp; some tests rely on this attribute.
    skills_mod._SKILLS_DIR = skills_dir
    skills_mod._DEFAULT_SKILLS_PACKAGE = tmp_path / "nonexistent-defaults"

    # Plant a "secret" file outside the skills root.
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-CONTENT")

    # The fuzzy candidate is a *symlink* whose realpath escapes _SKILLS_DIR.
    bad = skills_dir / "red-tea.md"
    os.symlink(str(secret), str(bad))

    # Fuzzy search for "red" — without _is_within filtering it would
    # have followed and dumped /etc/shadow-equivalent content.
    result = skills_mod.read_skill("red")
    assert result is None or "TOP-SECRET" not in result, (
        "fuzzy fallback must NOT follow symlinks out of the skills tree"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="symlink test is POSIX")
def test_fuzzy_fallback_rejects_internal_symlink(tmp_path, monkeypatch):
    """Even a symlink inside the skills root is rejected by NOFOLLOW."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    for mod in list(sys.modules):
        if mod.startswith("cogitum"):
            sys.modules.pop(mod, None)

    from cogitum.core import skills as skills_mod

    skills_mod._SKILLS_DIR = skills_dir
    skills_mod._DEFAULT_SKILLS_PACKAGE = tmp_path / "nonexistent-defaults"

    real = skills_dir / "real-skill.md"
    real.write_text("# real content\n")
    link = skills_dir / "fuzzy-target.md"
    os.symlink(str(real), str(link))

    # Fuzzy search "fuzzy" — the only candidate is a symlink, which
    # _read_text_nofollow rejects via O_NOFOLLOW (returns None).
    result = skills_mod.read_skill("fuzzy")
    assert result is None, "symlink in fuzzy fallback must be refused"


def test_fuzzy_fallback_returns_real_file(tmp_path, monkeypatch):
    """Sanity: a real (non-symlink) fuzzy match still returns content."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    for mod in list(sys.modules):
        if mod.startswith("cogitum"):
            sys.modules.pop(mod, None)

    from cogitum.core import skills as skills_mod

    skills_mod._SKILLS_DIR = skills_dir
    skills_mod._DEFAULT_SKILLS_PACKAGE = tmp_path / "nonexistent-defaults"

    real = skills_dir / "fuzzy-real.md"
    real.write_text("# legitimate content\nbody")

    result = skills_mod.read_skill("fuzzy")
    assert result is not None and "legitimate" in result
