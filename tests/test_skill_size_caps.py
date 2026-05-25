"""F8+F9: list_skills must cap reads, write_skill must cap content size."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


def _fresh_skills_module(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    for mod in list(sys.modules):
        if mod.startswith("cogitum"):
            sys.modules.pop(mod, None)
    from cogitum.core import skills as skills_mod
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(exist_ok=True)
    skills_mod._SKILLS_DIR = skills_dir
    skills_mod._DEFAULT_SKILLS_PACKAGE = tmp_path / "nonexistent-defaults"
    return skills_mod


def test_list_skills_caps_huge_skill_md(tmp_path, monkeypatch):
    """A 5MB SKILL.md must not OOM list_skills."""
    skills_mod = _fresh_skills_module(tmp_path, monkeypatch)

    huge = skills_mod._SKILLS_DIR / "huge" / "SKILL.md"
    huge.parent.mkdir(parents=True)
    # 5 MB of body
    huge.write_text("---\ndescription: huge-skill\n---\n" + ("x" * 5_000_000))

    items = skills_mod.list_skills()
    names = [s.name for s in items]
    assert "huge" in names
    item = next(s for s in items if s.name == "huge")
    assert item.description == "huge-skill"


def test_write_skill_rejects_oversize(tmp_path, monkeypatch):
    skills_mod = _fresh_skills_module(tmp_path, monkeypatch)

    big = "x" * (skills_mod._WRITE_SKILL_MAX_BYTES + 100)
    result = skills_mod.write_skill("toobig", big)
    assert "ERROR" in result and "too large" in result.lower()
    # File must not have been written.
    candidates = list(skills_mod._SKILLS_DIR.rglob("SKILL.md"))
    assert candidates == [], f"oversize write must not touch disk; found {candidates}"


def test_write_skill_accepts_normal_size(tmp_path, monkeypatch):
    skills_mod = _fresh_skills_module(tmp_path, monkeypatch)
    payload = "# Hello\nbody " * 100
    result = skills_mod.write_skill("ok-skill", payload)
    assert "OK" in result
    files = list(skills_mod._SKILLS_DIR.rglob("SKILL.md"))
    assert len(files) == 1


def test_write_skill_at_exact_cap(tmp_path, monkeypatch):
    """Content of exactly _WRITE_SKILL_MAX_BYTES bytes is allowed."""
    skills_mod = _fresh_skills_module(tmp_path, monkeypatch)
    cap = skills_mod._WRITE_SKILL_MAX_BYTES
    payload = "y" * cap
    result = skills_mod.write_skill("at-cap", payload)
    assert "OK" in result, result
