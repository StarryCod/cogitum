"""
cogitum.core.memory
~~~~~~~~~~~~~~~~~~~
Persistent memory — facts that survive across sessions.

Two stores:
  - user.md   — who the user is (preferences, name, style)
  - memory.md — agent's notes (environment, project conventions, lessons)

Injected into system prompt every turn. Keep compact.
"""
from __future__ import annotations

from pathlib import Path

from .platform_paths import get_data_dir

_MEMORY_DIR = get_data_dir() / "memory"
_USER_FILE = _MEMORY_DIR / "user.md"
_MEMORY_FILE = _MEMORY_DIR / "memory.md"

_SEPARATOR = "\n§\n"
_MAX_CHARS = 2200  # per file


def _ensure_dir() -> None:
    _MEMORY_DIR.mkdir(parents=True, exist_ok=True)


def _read(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _write(path: Path, content: str) -> None:
    _ensure_dir()
    path.write_text(content.strip(), encoding="utf-8")


def _entries(path: Path) -> list[str]:
    text = _read(path)
    if not text:
        return []
    return [e.strip() for e in text.split(_SEPARATOR) if e.strip()]


def _save_entries(path: Path, entries: list[str]) -> None:
    _write(path, _SEPARATOR.join(entries))


# ---------------------------------------------------------------------------
# Public API (used by memory tool)
# ---------------------------------------------------------------------------

def memory_add(target: str, content: str) -> str:
    """Add a new entry to memory or user store."""
    path = _USER_FILE if target == "user" else _MEMORY_FILE
    entries = _entries(path)

    # Check size limit
    current = _SEPARATOR.join(entries)
    if len(current) + len(content) + len(_SEPARATOR) > _MAX_CHARS:
        return f"ERROR: memory full ({len(current)}/{_MAX_CHARS} chars). Remove old entries first."

    entries.append(content.strip())
    _save_entries(path, entries)
    return f"OK: added to {target} ({len(content)} chars)"


def memory_replace(target: str, old_text: str, content: str) -> str:
    """Replace an entry identified by old_text substring."""
    path = _USER_FILE if target == "user" else _MEMORY_FILE
    entries = _entries(path)

    # Find entry containing old_text
    matches = [i for i, e in enumerate(entries) if old_text in e]
    if not matches:
        return f"ERROR: no entry containing '{old_text[:40]}' found"
    if len(matches) > 1:
        return f"ERROR: '{old_text[:40]}' matches {len(matches)} entries (must be unique)"

    entries[matches[0]] = content.strip()
    _save_entries(path, entries)
    return f"OK: replaced entry in {target}"


def memory_remove(target: str, old_text: str) -> str:
    """Remove an entry identified by old_text substring."""
    path = _USER_FILE if target == "user" else _MEMORY_FILE
    entries = _entries(path)

    matches = [i for i, e in enumerate(entries) if old_text in e]
    if not matches:
        return f"ERROR: no entry containing '{old_text[:40]}' found"

    for i in sorted(matches, reverse=True):
        entries.pop(i)
    _save_entries(path, entries)
    return f"OK: removed {len(matches)} entry/entries from {target}"


def get_memory_context() -> str:
    """Get formatted memory for injection into system prompt."""
    user = _read(_USER_FILE)
    memory = _read(_MEMORY_FILE)

    parts: list[str] = []
    if memory:
        parts.append(f"══ MEMORY (agent notes) ══\n{memory}")
    if user:
        parts.append(f"══ USER ══\n{user}")

    return "\n\n".join(parts)


def get_memory_stats() -> dict[str, int]:
    """Return char counts for both stores."""
    return {
        "user": len(_read(_USER_FILE)),
        "memory": len(_read(_MEMORY_FILE)),
        "max": _MAX_CHARS,
    }
