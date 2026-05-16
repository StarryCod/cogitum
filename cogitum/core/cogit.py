"""
cogitum.core.cogit
~~~~~~~~~~~~~~~~~~
Cogit — smart checkpoints for agent work.

Lighter than git: stores file snapshots at checkpoint time.
Agent auto-saves before dangerous operations. User can restore any checkpoint.

Key feature: SCOPE — agent can checkpoint a specific directory or file list,
not the entire project. This keeps checkpoints fast and small.

Storage: ~/.config/cogitum/cogits/{session_id}/
  - 0001.json  — {label, timestamp, scope, snapshot: [{path, content}]}
  - 0002.json  — ...
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_COGIT_DIR = Path("~/.config/cogitum/cogits").expanduser()

# Directories and extensions to always skip
_SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache", ".pytest_cache", "dist", "build"}
_SKIP_EXTS = {".pyc", ".pyo", ".so", ".o", ".a", ".dylib", ".whl", ".egg-info"}
_MAX_FILE_SIZE = 500_000  # 500KB per file max


@dataclass
class Checkpoint:
    index: int
    label: str
    timestamp: float
    file_count: int
    scope: str


class CogitStore:
    """Manages checkpoints for a session within a project directory."""

    def __init__(self, session_id: str, project_dir: str | Path) -> None:
        self.session_id = session_id
        self.project_dir = Path(project_dir).resolve()
        self.store_dir = _COGIT_DIR / session_id
        self.store_dir.mkdir(parents=True, exist_ok=True)

    def _collect_files(self, scope: str | None = None) -> list[Path]:
        """Collect files to checkpoint.
        
        scope: relative path to a directory or file within project.
               None = entire project (with smart filtering).
               Can be: "src/", "cogitum/core/", "main.py", etc.
        """
        if scope:
            target = self.project_dir / scope
            if target.is_file():
                return [target]
            elif target.is_dir():
                base = target
            else:
                # Try glob
                matches = list(self.project_dir.glob(scope))
                return [m for m in matches if m.is_file()]
        else:
            base = self.project_dir

        files = []
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            # Skip by directory
            try:
                parts = path.relative_to(self.project_dir).parts
            except ValueError:
                continue
            if any(p in _SKIP_DIRS for p in parts):
                continue
            # Skip by extension
            if path.suffix in _SKIP_EXTS:
                continue
            # Skip large files
            try:
                if path.stat().st_size > _MAX_FILE_SIZE:
                    continue
            except (OSError, PermissionError):
                continue
            files.append(path)
        return files

    def save(self, label: str = "", scope: str | None = None) -> Checkpoint:
        """Save a checkpoint.
        
        label: Human-readable description (e.g. 'before refactor auth')
        scope: Relative path to checkpoint. None = whole project.
               Examples: "cogitum/core/", "src/main.py", "*.py"
        """
        files = self._collect_files(scope)

        # Build snapshot
        snapshot: list[dict[str, str]] = []
        for path in files:
            try:
                rel = str(path.relative_to(self.project_dir))
                content = path.read_text(encoding="utf-8")
                snapshot.append({"path": rel, "content": content})
            except (UnicodeDecodeError, OSError):
                continue

        # Determine index
        existing = sorted(self.store_dir.glob("[0-9]*.json"))
        index = len(existing) + 1

        # Save
        checkpoint_data = {
            "index": index,
            "label": label or f"checkpoint {index}",
            "timestamp": time.time(),
            "scope": scope or ".",
            "snapshot": snapshot,
        }
        cp_path = self.store_dir / f"{index:04d}.json"
        cp_path.write_text(
            json.dumps(checkpoint_data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

        return Checkpoint(
            index=index,
            label=checkpoint_data["label"],
            timestamp=checkpoint_data["timestamp"],
            file_count=len(snapshot),
            scope=scope or ".",
        )

    def list_checkpoints(self) -> list[Checkpoint]:
        """List all checkpoints."""
        checkpoints = []
        for path in sorted(self.store_dir.glob("[0-9]*.json")):
            try:
                data = json.loads(path.read_text())
                checkpoints.append(Checkpoint(
                    index=data["index"],
                    label=data["label"],
                    timestamp=data["timestamp"],
                    file_count=len(data.get("snapshot", data.get("diffs", []))),
                    scope=data.get("scope", "."),
                ))
            except (json.JSONDecodeError, KeyError):
                continue
        return checkpoints

    def restore(self, index: int) -> str:
        """Restore project files from a checkpoint."""
        cp_path = self.store_dir / f"{index:04d}.json"
        if not cp_path.exists():
            return f"ERROR: checkpoint #{index} not found"

        data = json.loads(cp_path.read_text())
        snapshot = data.get("snapshot", [])

        if not snapshot:
            return f"ERROR: checkpoint #{index} has no snapshot data"

        # Restore files from snapshot
        restored = 0
        for entry in snapshot:
            rel = entry["path"]
            target = self.project_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(entry["content"], encoding="utf-8")
            restored += 1

        label = data.get("label", f"#{index}")
        return f"OK: restored checkpoint #{index} '{label}' ({restored} files)"

    def diff(self, index: int) -> str:
        """Show what changed since a checkpoint (compared to current state)."""
        cp_path = self.store_dir / f"{index:04d}.json"
        if not cp_path.exists():
            return f"ERROR: checkpoint #{index} not found"

        data = json.loads(cp_path.read_text())
        snapshot = data.get("snapshot", [])
        scope = data.get("scope")

        # Build map of checkpoint state
        cp_state: dict[str, str] = {}
        for entry in snapshot:
            cp_state[entry["path"]] = entry["content"]

        # Current state (same scope)
        current_files = self._collect_files(scope)
        current_state: dict[str, str] = {}
        for path in current_files:
            try:
                rel = str(path.relative_to(self.project_dir))
                current_state[rel] = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue

        # Compare
        added = set(current_state.keys()) - set(cp_state.keys())
        removed = set(cp_state.keys()) - set(current_state.keys())
        modified = []
        for rel in set(cp_state.keys()) & set(current_state.keys()):
            if cp_state[rel] != current_state[rel]:
                modified.append(rel)

        if not added and not removed and not modified:
            return f"No changes since checkpoint #{index}"

        lines = [f"Changes since checkpoint #{index} '{data.get('label', '')}':"]
        if added:
            lines.append(f"\n  Added ({len(added)}):")
            for f in sorted(added)[:20]:
                lines.append(f"    + {f}")
        if removed:
            lines.append(f"\n  Removed ({len(removed)}):")
            for f in sorted(removed)[:20]:
                lines.append(f"    - {f}")
        if modified:
            lines.append(f"\n  Modified ({len(modified)}):")
            for f in sorted(modified)[:20]:
                lines.append(f"    ~ {f}")

        return "\n".join(lines)

    def cleanup(self, keep_last: int = 10) -> int:
        """Remove old checkpoints, keeping the last N."""
        existing = sorted(self.store_dir.glob("[0-9]*.json"))
        if len(existing) <= keep_last:
            return 0
        to_remove = existing[:-keep_last]
        for path in to_remove:
            path.unlink()
        return len(to_remove)
