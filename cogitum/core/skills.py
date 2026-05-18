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

import re
import shutil
from pathlib import Path
from dataclasses import dataclass

from .platform_paths import get_data_dir

_SKILLS_DIR = get_data_dir() / "skills"

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

        content = path.read_text(encoding="utf-8")
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
        content = path.read_text(encoding="utf-8")
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


def read_skill(name: str) -> str | None:
    """Read full skill content by name (searches nested and flat).

    Searches user dir first (so user-edited skills win over package
    defaults), then falls back to the package-bundled skills. The
    fallback matters because seed_default_skills() only seeds on the
    very first run — users who installed Cogitum before a new
    package skill was added wouldn't see it otherwise.
    """
    _ensure_dir()

    for root in (_SKILLS_DIR, _DEFAULT_SKILLS_PACKAGE):
        if not root.exists():
            continue

        # Try exact nested match: category/name/SKILL.md
        for path in root.rglob("SKILL.md"):
            rel = path.relative_to(root)
            if len(rel.parts) >= 2 and rel.parts[-2] == name:
                return path.read_text(encoding="utf-8")

        # Try flat match
        flat = root / f"{name}.md"
        if flat.exists():
            return flat.read_text(encoding="utf-8")

    # Fuzzy match — only on user dir (avoid picking package skills
    # by accident when the user clearly meant their own).
    candidates = []
    for path in _SKILLS_DIR.rglob("SKILL.md"):
        rel = path.relative_to(_SKILLS_DIR)
        if len(rel.parts) >= 2 and name in rel.parts[-2]:
            candidates.append(path)
    if not candidates:
        candidates = list(_SKILLS_DIR.glob(f"*{name}*.md"))

    if candidates:
        return candidates[0].read_text(encoding="utf-8")

    return None


def write_skill(name: str, content: str, category: str = "custom") -> str:
    """Create or update a skill."""
    _ensure_dir()
    # Sanitize name
    name = re.sub(r"[^a-z0-9_-]", "-", name.lower())
    category = re.sub(r"[^a-z0-9_-]", "-", category.lower()) if category else "custom"

    # Check if skill already exists (update in place)
    for path in _SKILLS_DIR.rglob("SKILL.md"):
        rel = path.relative_to(_SKILLS_DIR)
        if len(rel.parts) >= 2 and rel.parts[-2] == name:
            path.write_text(content, encoding="utf-8")
            return f"OK: skill '{name}' updated ({len(content)} chars)"

    # Create new in category/name/SKILL.md
    skill_dir = _SKILLS_DIR / category / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(content, encoding="utf-8")
    return f"OK: skill '{category}/{name}' created ({len(content)} chars)"


def delete_skill(name: str) -> str:
    """Delete a skill (removes entire skill directory)."""
    # Find nested
    for path in _SKILLS_DIR.rglob("SKILL.md"):
        rel = path.relative_to(_SKILLS_DIR)
        if len(rel.parts) >= 2 and rel.parts[-2] == name:
            skill_dir = path.parent
            shutil.rmtree(skill_dir)
            return f"OK: skill '{name}' deleted"

    # Try flat
    flat = _SKILLS_DIR / f"{name}.md"
    if flat.exists():
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
