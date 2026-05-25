"""
cogitum.core.skills
~~~~~~~~~~~~~~~~~~~
Skills system — agent's procedural memory.

Skills are markdown files stored in ~/.config/cogitum/skills/.
Structure: category/skill-name/SKILL.md (+ optional references/, templates/, scripts/)
Or flat: skill-name.md (for agent-created skills)

The agent has tools:
  - skills(action="list") → see all available skills with descriptions
  - skills(action="read", name="...") → read full skill content
  - skills(action="write", name="...", content="...") → create/update a skill
  - skills(action="delete", name="...") → remove a skill
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from dataclasses import dataclass

from .platform_paths import get_data_dir

log = logging.getLogger(__name__)

_SKILLS_DIR = get_data_dir() / "skills"

# F8: cap how many bytes ``list_skills`` will read from a single
# SKILL.md before truncating. Frontmatter + first description line is
# all we ever need — a skill that legitimately exceeds 256KB has
# bigger problems. Without the cap a single 100MB file could
# materialize the whole listing into memory and stall the TUI.
_LIST_SKILL_READ_CAP = 256_000

# F9: hard ceiling on ``write_skill`` payload size. 1MB is generous
# for any sane procedural-memory entry; anything bigger is almost
# certainly accidental (an LLM dumping a transcript or binary
# blob into the skills store). Reject before we touch disk.
_WRITE_SKILL_MAX_BYTES = 1_048_576

# Reject names containing path separators, parent traversal, glob metachars,
# or control characters. Skill names are allowed to be reasonably free-form
# (we still pass them to write_skill which sanitizes), but for *read* we lock
# them down hard since they flow into glob() patterns.
_NAME_REJECT_RE = re.compile(r"[\x00-\x1f/\\*?\[\]]")


def _name_is_safe(name: str) -> bool:
    """Return True iff `name` is safe to feed into glob/path joins."""
    if not name or not isinstance(name, str):
        return False
    if ".." in name:
        return False
    if _NAME_REJECT_RE.search(name):
        return False
    return True


def _is_within(path: Path, root: Path) -> bool:
    """Return True iff `path` (after realpath) lives under `root` (after realpath)."""
    try:
        rp = Path(os.path.realpath(str(path)))
        rr = Path(os.path.realpath(str(root)))
    except OSError:
        return False
    try:
        rp.relative_to(rr)
        return True
    except ValueError:
        return False

# Default skills shipped with the package
_DEFAULT_SKILLS_PACKAGE = Path(__file__).resolve().parent.parent / "data" / "skills"


def seed_default_skills() -> None:
    """Copy built-in skills to user config dir if it is empty."""
    _SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    existing = list(_SKILLS_DIR.rglob("SKILL.md")) + list(_SKILLS_DIR.glob("*.md"))
    if existing:
        return  # user already has skills, never overwrite
    if not _DEFAULT_SKILLS_PACKAGE.exists():
        return
    for src in _DEFAULT_SKILLS_PACKAGE.rglob("*"):
        if src.is_file():
            rel = src.relative_to(_DEFAULT_SKILLS_PACKAGE)
            dst = _SKILLS_DIR / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


@dataclass
class SkillMeta:
    name: str
    category: str
    description: str
    path: Path


def _ensure_dir() -> None:
    _SKILLS_DIR.mkdir(parents=True, exist_ok=True)


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Extract YAML frontmatter and body from markdown (no yaml dep)."""
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            meta = {}
            for line in parts[1].strip().split("\n"):
                if ":" in line:
                    key, _, val = line.partition(":")
                    meta[key.strip()] = val.strip().strip("'\"")
                    # Handle multi-word values with colons
                    if val.count(":") > 0 and not meta[key.strip()]:
                        meta[key.strip()] = val.strip()
            body = parts[2].strip()
            return meta, body
    return {}, content


def list_skills(category: str = "") -> list[SkillMeta]:
    """List all available skills. Optionally filter by category."""
    _ensure_dir()
    skills = []

    # Find all SKILL.md files (nested structure)
    for path in sorted(_SKILLS_DIR.rglob("SKILL.md")):
        rel = path.relative_to(_SKILLS_DIR)
        parts = rel.parts  # e.g. ('github', 'github-pr-workflow', 'SKILL.md')

        if len(parts) == 3:
            cat, name = parts[0], parts[1]
        elif len(parts) == 2:
            cat, name = parts[0], parts[0]
        else:
            continue

        if category and cat != category:
            continue

        # F8: cap reads at ``_LIST_SKILL_READ_CAP`` so a malicious or
        # accidentally-huge SKILL.md can't OOM the listing pass.
        try:
            with open(path, "r", encoding="utf-8") as fh:
                content = fh.read(_LIST_SKILL_READ_CAP)
        except OSError:
            continue
        meta, body = _parse_frontmatter(content)
        description = meta.get("description", "")
        if not description:
            for line in body.split("\n"):
                line = line.strip().lstrip("#").strip()
                if line:
                    description = line[:100]
                    break

        skills.append(SkillMeta(name=name, category=cat, description=description, path=path))

    # Also find flat .md files (agent-created skills)
    for path in sorted(_SKILLS_DIR.glob("*.md")):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                content = fh.read(_LIST_SKILL_READ_CAP)
        except OSError:
            continue
        meta, body = _parse_frontmatter(content)
        name = path.stem
        description = meta.get("description", "")
        if not description:
            for line in body.split("\n"):
                line = line.strip().lstrip("#").strip()
                if line:
                    description = line[:100]
                    break
        skills.append(SkillMeta(name=name, category="custom", description=description, path=path))

    return skills


def _read_text_nofollow(path: Path) -> str | None:
    """Read ``path`` rejecting any symlink along the final component.

    Closes the TOCTOU between an ``os.path.realpath`` check and a
    subsequent ``read_text``: an attacker who wins the race could
    swap the file for a symlink between check and open. ``O_NOFOLLOW``
    makes the kernel refuse to follow a terminal-component symlink at
    open time (returns ELOOP / errno 40), so we either get the real
    file or nothing. Falls back to a plain read on platforms missing
    the flag (Windows). Tier-4 hardening.
    """
    o_nofollow = getattr(os, "O_NOFOLLOW", None)
    if o_nofollow is None:
        # Windows etc. — symlinks are rare and read_text() suffices.
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None
    try:
        fd = os.open(str(path), os.O_RDONLY | o_nofollow)
    except OSError as e:
        # ELOOP (40) means the final component was a symlink — refuse
        # silently so the caller can log + continue. Other errors
        # (ENOENT, EACCES) propagate as None too — we never crashed
        # the agent on a missing skill.
        log.warning("read_skill: O_NOFOLLOW open rejected %s (%s)", path, e)
        return None
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def read_skill(name: str) -> str | None:
    """Read full skill content by name (searches nested and flat).

    Searches user dir first (so user-edited skills win over package
    defaults), then falls back to the package-bundled skills. The
    fallback matters because seed_default_skills() only seeds on the
    very first run — users who installed Cogitum before a new
    package skill was added wouldn't see it otherwise.
    """
    _ensure_dir()

    if not _name_is_safe(name):
        log.warning("read_skill: rejected unsafe name %r", name)
        return None

    for root in (_SKILLS_DIR, _DEFAULT_SKILLS_PACKAGE):
        if not root.exists():
            continue

        # Try exact nested match: category/name/SKILL.md
        for path in root.rglob("SKILL.md"):
            rel = path.relative_to(root)
            if len(rel.parts) >= 2 and rel.parts[-2] == name:
                if not _is_within(path, root):
                    log.warning(
                        "read_skill: skipping symlink-escape %s -> %s",
                        path, os.path.realpath(str(path)),
                    )
                    continue
                content = _read_text_nofollow(path)
                if content is not None:
                    return content

        # Try flat match
        flat = root / f"{name}.md"
        if flat.exists():
            if not _is_within(flat, root):
                log.warning(
                    "read_skill: skipping symlink-escape %s -> %s",
                    flat, os.path.realpath(str(flat)),
                )
                continue
            content = _read_text_nofollow(flat)
            if content is not None:
                return content

    # Fuzzy match — only on user dir (avoid picking package skills
    # by accident when the user clearly meant their own).
    candidates = []
    for path in _SKILLS_DIR.rglob("SKILL.md"):
        rel = path.relative_to(_SKILLS_DIR)
        if len(rel.parts) >= 2 and name in rel.parts[-2]:
            if _is_within(path, _SKILLS_DIR):
                candidates.append(path)
    if not candidates:
        for p in _SKILLS_DIR.glob(f"*{name}*.md"):
            if _is_within(p, _SKILLS_DIR):
                candidates.append(p)

    if candidates:
        # F7: route the fuzzy fallback through the same NOFOLLOW reader
        # the exact-match path uses. Without this, an attacker could
        # plant a symlink ``red-tea.md → /etc/shadow`` and a fuzzy
        # search for "red" would happily resolve and read the target.
        # Returns None when the candidate is a symlink.
        return _read_text_nofollow(candidates[0])

    return None


def write_skill(name: str, content: str, category: str = "custom") -> str:
    """Create or update a skill."""
    _ensure_dir()
    # F9: reject pathologically-large content. 1MB ceiling is generous
    # for a procedural-memory entry; anything bigger is an LLM
    # accident (transcript dump, base64-encoded blob) and writing it
    # would just waste disk + slow every subsequent ``list_skills``.
    if isinstance(content, str) and len(content.encode("utf-8")) > _WRITE_SKILL_MAX_BYTES:
        return (
            f"ERROR: skill content too large "
            f"({len(content.encode('utf-8'))} > {_WRITE_SKILL_MAX_BYTES} bytes)"
        )
    # Sanitize name
    name = re.sub(r"[^a-z0-9_-]", "-", name.lower())
    category = re.sub(r"[^a-z0-9_-]", "-", category.lower()) if category else "custom"

    # F12: use atomic_write_text to eliminate the torn-write window where
    # a Ctrl+C / power loss mid-write would leave SKILL.md half-written
    # (and corrupt every subsequent read until the user manually fixes it).
    from .atomic_io import atomic_write_text

    # Check if skill already exists (update in place)
    for path in _SKILLS_DIR.rglob("SKILL.md"):
        rel = path.relative_to(_SKILLS_DIR)
        if len(rel.parts) >= 2 and rel.parts[-2] == name:
            atomic_write_text(path, content)
            return f"OK: skill '{name}' updated ({len(content)} chars)"

    # Create new in category/name/SKILL.md
    skill_dir = _SKILLS_DIR / category / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    atomic_write_text(path, content)
    return f"OK: skill '{category}/{name}' created ({len(content)} chars)"


def delete_skill(name: str) -> str:
    """Delete a skill (removes entire skill directory)."""
    # F11: name validation gate. Without it, a name like "../../../etc"
    # hit ``_SKILLS_DIR.rglob("SKILL.md")`` (which only walks under the
    # skills root, harmless) BUT the discovered ``skill_dir = path.parent``
    # could be a symlink pointing outside the skills root, and then
    # ``shutil.rmtree`` happily followed it. Refuse unsafe names up
    # front and verify the resolved directory is contained before
    # rmtree.
    if not _name_is_safe(name):
        return f"ERROR: invalid skill name '{name}'"

    # Find nested
    for path in _SKILLS_DIR.rglob("SKILL.md"):
        rel = path.relative_to(_SKILLS_DIR)
        if len(rel.parts) >= 2 and rel.parts[-2] == name:
            skill_dir = path.parent
            if not _is_within(skill_dir, _SKILLS_DIR):
                # Symlink-escape: refuse to rmtree something outside
                # the skills directory.
                log.warning(
                    "delete_skill: refusing rmtree %s (escapes %s)",
                    skill_dir, _SKILLS_DIR,
                )
                return f"ERROR: skill '{name}' resolves outside skills root"
            shutil.rmtree(skill_dir)
            return f"OK: skill '{name}' deleted"

    # Try flat
    flat = _SKILLS_DIR / f"{name}.md"
    if flat.exists():
        if not _is_within(flat, _SKILLS_DIR):
            log.warning(
                "delete_skill: refusing unlink %s (escapes %s)",
                flat, _SKILLS_DIR,
            )
            return f"ERROR: skill '{name}' resolves outside skills root"
        flat.unlink()
        return f"OK: skill '{name}' deleted"

    return f"ERROR: skill '{name}' not found"


def list_categories() -> list[str]:
    """List all skill categories."""
    _ensure_dir()
    cats = set()
    for path in _SKILLS_DIR.rglob("SKILL.md"):
        rel = path.relative_to(_SKILLS_DIR)
        if len(rel.parts) >= 3:
            cats.add(rel.parts[0])
    return sorted(cats)


def skill_summary() -> str:
    """Generate a compact summary of all skills for system prompt injection.
    Cached for 60s to avoid re-reading 73+ files on every LLM call."""
    now = __import__("time").time()
    if hasattr(skill_summary, "_cache") and (now - skill_summary._cache_time) < 60:
        return skill_summary._cache

    skills = list_skills()
    if not skills:
        skill_summary._cache = ""
        skill_summary._cache_time = now
        return ""

    # Group by category
    by_cat: dict[str, list[SkillMeta]] = {}
    for s in skills:
        by_cat.setdefault(s.category, []).append(s)

    lines = ["Available skills (use skills tool to read/write):"]
    for cat in sorted(by_cat):
        lines.append(f"  [{cat}]")
        for s in by_cat[cat]:
            desc = s.description[:60] + "…" if len(s.description) > 60 else s.description
            lines.append(f"    • {s.name}: {desc}")

    result = "\n".join(lines)
    skill_summary._cache = result
    skill_summary._cache_time = now
    return result
