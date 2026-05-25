"""F11/F12: skill safety + atomic writes."""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def skills_root(tmp_path, monkeypatch):
    """Point _SKILLS_DIR at a tmp dir for the duration of each test."""
    import cogitum.core.skills as sk

    root = tmp_path / "skills"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sk, "_SKILLS_DIR", root)
    return root


def test_write_skill_uses_atomic_write_text(skills_root, monkeypatch):
    """F12: write_skill must call atomic_write_text — no torn writes."""
    import cogitum.core.skills as sk
    import cogitum.core.atomic_io as atomic_io

    calls = []
    real = atomic_io.atomic_write_text

    def spy(path, content, **kw):
        calls.append((Path(path), len(content)))
        return real(path, content, **kw)

    # The skills module imports atomic_write_text inside the function,
    # so patch the source module.
    monkeypatch.setattr(atomic_io, "atomic_write_text", spy)

    out = sk.write_skill("test-skill", "Hello world\n")
    assert "OK" in out
    # At least one atomic write happened, into the skills root.
    assert calls, "atomic_write_text was not called"
    assert any(skills_root in p.parents for p, _ in calls)


def test_delete_skill_rejects_unsafe_name(skills_root):
    """F11: delete_skill must refuse names that look like a path-escape."""
    import cogitum.core.skills as sk

    # Unsafe names: contain '..' or path separators.
    assert "ERROR" in sk.delete_skill("../../etc")
    assert "ERROR" in sk.delete_skill("evil/skill")
    assert "ERROR" in sk.delete_skill("\x00null")


def test_delete_skill_refuses_symlink_escape(skills_root, tmp_path):
    """F11: a symlink that resolves outside skills root is refused."""
    import cogitum.core.skills as sk

    # Create a real "victim" directory outside the skills root.
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "important.txt").write_text("dont touch")

    # Create a category with a symlinked skill name pointing to victim.
    cat = skills_root / "evil"
    cat.mkdir()
    bad_link = cat / "trojan"
    try:
        bad_link.symlink_to(victim)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")

    # Plant a SKILL.md inside the symlinked dir so rglob picks it up.
    (victim / "SKILL.md").write_text("# evil\n")

    # delete_skill('trojan') would historically rmtree(victim) — the
    # F11 fix refuses because the resolved skill_dir escapes _SKILLS_DIR.
    res = sk.delete_skill("trojan")
    assert "ERROR" in res
    # Victim must still exist.
    assert (victim / "important.txt").exists()


def test_write_skill_then_delete_normal_flow(skills_root):
    """Sanity: legitimate names still round-trip cleanly."""
    import cogitum.core.skills as sk

    sk.write_skill("nice-skill", "# nice\n", category="custom")
    found = list(skills_root.rglob("SKILL.md"))
    assert any("nice-skill" in str(p) for p in found)

    res = sk.delete_skill("nice-skill")
    assert "OK" in res
    assert not list(skills_root.rglob("SKILL.md"))
