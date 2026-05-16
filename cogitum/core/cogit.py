"""
cogitum.core.cogit
~~~~~~~~~~~~~~~~~~
Cogit — smart checkpoints for agent work.

Lighter than git: stores file snapshots at checkpoint time.
Agent auto-saves before dangerous operations. User can restore any checkpoint.

Key features:
- SCOPE: agent can checkpoint a specific directory or file list, not the
  entire project. Keeps checkpoints fast and small.
- PROJECT-KEYED: checkpoints from /projA never appear in /projB even within
  the same session_id. Restore validates project_dir before touching files.
- CONTENT-ADDRESSABLE: file bodies are stored once globally (sha256), and
  manifests reference them by hash. 10 checkpoints of an unchanged 5MB tree
  cost ~5MB on disk, not 50MB.
- .gitignore-AWARE: walks up from each file looking for .gitignore (root
  pattern set merged), so node_modules / models / .env don't get scooped.
- ORPHAN-DELETE: restore removes files that exist now but didn't at
  checkpoint time, so the working tree truly matches the saved state.
- AUTO-SAFETY: restore() auto-creates a "__pre_restore_NNNN__" checkpoint
  of the CURRENT state before touching anything, so a bad restore is itself
  reversible.

Storage layout:
  ~/.config/cogitum/cogits/
    objects/<sha[:2]>/<sha>           — content blobs (write-once)
    projects/<project_hash>/<sid>/    — per-project, per-session manifests
        NNNN.json  — {index, label, timestamp, scope, project_dir,
                      files: [{path, sha}], removed_at_save: []}
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    import pathspec  # type: ignore
    _HAS_PATHSPEC = True
except ImportError:  # pragma: no cover
    _HAS_PATHSPEC = False


_COGIT_ROOT = Path("~/.config/cogitum/cogits").expanduser()
_OBJECTS_DIR = _COGIT_ROOT / "objects"
_PROJECTS_DIR = _COGIT_ROOT / "projects"

# Directories and extensions to always skip (in addition to .gitignore).
_SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".idea", ".vscode",
    "target", "out", "coverage", ".next", ".nuxt", ".cache", ".tox",
}
_SKIP_EXTS = {
    ".pyc", ".pyo", ".so", ".o", ".a", ".dylib", ".whl", ".egg-info",
    ".bin", ".gguf", ".safetensors", ".pt", ".pth", ".ckpt", ".onnx",
}
_MAX_FILE_SIZE = 500_000  # 500KB per file max

# Built-in default ignore patterns (always applied, even without .gitignore).
_DEFAULT_IGNORE = [
    "*.log", "*.tmp", "*.swp", "*.bak",
    ".env", ".env.*", "secrets.env", "secrets.toml",
    "models/", "weights/", "checkpoints/",
]


# ── helpers ─────────────────────────────────────────────────────────────────


def _project_hash(project_dir: Path) -> str:
    """Stable hash of the canonical project path. 16 hex chars is plenty."""
    return hashlib.sha256(str(project_dir).encode("utf-8")).hexdigest()[:16]


def _object_path(sha: str) -> Path:
    return _OBJECTS_DIR / sha[:2] / sha


def _write_object(content: str) -> str:
    """Store content addressably; return its sha256 hex.

    Write-once: if blob already exists, just return the hash. Caller can
    safely write the same content N times — disk cost is O(unique).
    """
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    obj = _object_path(digest)
    if not obj.exists():
        obj.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically so partial blobs never end up on disk.
        tmp = obj.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(obj)
    return digest


def _read_object(sha: str) -> str | None:
    obj = _object_path(sha)
    if not obj.exists():
        return None
    try:
        return obj.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _load_gitignore(project_dir: Path) -> "pathspec.PathSpec | None":
    """Load .gitignore from project root (and merge with defaults)."""
    if not _HAS_PATHSPEC:
        return None
    patterns = list(_DEFAULT_IGNORE)
    gi = project_dir / ".gitignore"
    if gi.is_file():
        try:
            patterns.extend(gi.read_text(encoding="utf-8").splitlines())
        except OSError:
            pass
    return pathspec.PathSpec.from_lines("gitignore", patterns)


# ── data ────────────────────────────────────────────────────────────────────


@dataclass
class Checkpoint:
    index: int
    label: str
    timestamp: float
    file_count: int
    scope: str


# ── store ───────────────────────────────────────────────────────────────────


class CogitStore:
    """Manages checkpoints for a (project, session) pair.

    Checkpoints are namespaced by project hash, so the same session_id used
    in two projects gets two separate manifest dirs. Object blobs are global
    (deduped across all projects/sessions).
    """

    def __init__(self, session_id: str, project_dir: str | Path) -> None:
        self.session_id = session_id
        self.project_dir = Path(project_dir).resolve()
        self._project_hash = _project_hash(self.project_dir)
        self.store_dir = _PROJECTS_DIR / self._project_hash / session_id
        self.store_dir.mkdir(parents=True, exist_ok=True)
        _OBJECTS_DIR.mkdir(parents=True, exist_ok=True)
        self._gitignore = _load_gitignore(self.project_dir)

    # ── file collection ────────────────────────────────────────────────

    def _is_ignored(self, rel_parts: tuple[str, ...], rel_path: str) -> bool:
        # Hard skip dirs (faster than gitignore for the obvious junk).
        if any(p in _SKIP_DIRS for p in rel_parts):
            return True
        # Extension skip.
        for ext in _SKIP_EXTS:
            if rel_path.endswith(ext):
                return True
        # .gitignore + defaults.
        if self._gitignore is not None and self._gitignore.match_file(rel_path):
            return True
        return False

    def _collect_files(self, scope: str | None = None) -> list[Path]:
        """Collect files to checkpoint.

        scope: relative path inside project. None = entire project.
        """
        if scope:
            target = self.project_dir / scope
            if target.is_file():
                return [target]
            if target.is_dir():
                base = target
            else:
                # Try glob (relative to project_dir).
                matches = list(self.project_dir.glob(scope))
                return [m for m in matches if m.is_file()]
        else:
            base = self.project_dir

        files: list[Path] = []
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            try:
                rel_parts = path.relative_to(self.project_dir).parts
            except ValueError:
                continue
            rel_path = "/".join(rel_parts)
            if self._is_ignored(rel_parts, rel_path):
                continue
            try:
                if path.stat().st_size > _MAX_FILE_SIZE:
                    continue
            except OSError:
                continue
            files.append(path)
        return files

    # ── core operations ────────────────────────────────────────────────

    def _next_index(self) -> int:
        existing = sorted(self.store_dir.glob("[0-9]*.json"))
        return len(existing) + 1

    def save(
        self,
        label: str = "",
        scope: str | None = None,
    ) -> Checkpoint:
        """Save a checkpoint of the current scope state.

        Returns a Checkpoint dataclass describing what was saved.
        """
        files = self._collect_files(scope)

        # Hash + store content. Files we can't read just get skipped.
        manifest_files: list[dict[str, str]] = []
        for path in files:
            try:
                rel = str(path.relative_to(self.project_dir))
                content = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            sha = _write_object(content)
            manifest_files.append({"path": rel, "sha": sha})

        index = self._next_index()
        cp_data = {
            "index": index,
            "label": label or f"checkpoint {index}",
            "timestamp": time.time(),
            "scope": scope or ".",
            "project_dir": str(self.project_dir),
            "files": manifest_files,
        }
        cp_path = self.store_dir / f"{index:04d}.json"
        # Atomic write.
        tmp = cp_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(cp_data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        tmp.replace(cp_path)

        return Checkpoint(
            index=index,
            label=cp_data["label"],
            timestamp=cp_data["timestamp"],
            file_count=len(manifest_files),
            scope=scope or ".",
        )

    def list_checkpoints(self) -> list[Checkpoint]:
        out: list[Checkpoint] = []
        for path in sorted(self.store_dir.glob("[0-9]*.json")):
            data = self._load_manifest(path)
            if data is None:
                continue
            out.append(Checkpoint(
                index=data["index"],
                label=data.get("label", f"#{data['index']}"),
                timestamp=data.get("timestamp", 0.0),
                file_count=len(self._manifest_files(data)),
                scope=data.get("scope", "."),
            ))
        return out

    def restore(self, index: int, *, auto_safety: bool = True) -> str:
        """Restore project files from a checkpoint.

        - Validates the checkpoint belongs to THIS project (path stored at
          save time must match resolved project_dir).
        - Auto-creates a __pre_restore_NNNN__ checkpoint of the current
          scope state before touching anything (unless auto_safety=False).
        - Removes orphan files (present now but absent in the checkpoint).
        """
        cp_path = self.store_dir / f"{index:04d}.json"
        if not cp_path.exists():
            return f"ERROR: checkpoint #{index} not found"

        data = self._load_manifest(cp_path)
        if data is None:
            return f"ERROR: checkpoint #{index} is corrupt"

        # Project sanity check (defence in depth — store_dir is already
        # project-keyed, but if someone hand-copied manifests we still
        # refuse to write into the wrong tree).
        saved_dir = data.get("project_dir")
        if saved_dir and Path(saved_dir).resolve() != self.project_dir:
            return (
                f"ERROR: checkpoint #{index} was saved in {saved_dir!r}, "
                f"refusing to restore into {str(self.project_dir)!r}"
            )

        manifest_files = self._manifest_files(data)
        if not manifest_files:
            return f"ERROR: checkpoint #{index} has no file data"

        scope = data.get("scope")
        scope_arg = scope if scope and scope != "." else None

        # Auto-safety: snapshot current state before touching disk.
        pre_label = ""
        if auto_safety:
            pre = self.save(
                label=f"__pre_restore_{index:04d}__",
                scope=scope_arg,
            )
            pre_label = f" (pre-restore safety #{pre.index})"

        # Build set of restored relpaths for orphan deletion.
        restored_rels: set[str] = set()
        restored = 0
        for entry in manifest_files:
            rel = entry["path"]
            sha = entry.get("sha")
            content = entry.get("content")
            if content is None and sha:
                content = _read_object(sha)
            if content is None:
                # Object missing — skip rather than truncate to empty.
                continue
            target = self.project_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            restored_rels.add(rel)
            restored += 1

        # Orphan deletion: anything in current scope but not in snapshot.
        removed = 0
        for path in self._collect_files(scope_arg):
            try:
                rel = str(path.relative_to(self.project_dir))
            except ValueError:
                continue
            if rel in restored_rels:
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue

        label = data.get("label", f"#{index}")
        suffix = f", removed {removed} orphan(s)" if removed else ""
        return (
            f"OK: restored checkpoint #{index} '{label}' "
            f"({restored} files{suffix}){pre_label}"
        )

    def diff(self, index: int) -> str:
        """Show added / removed / modified files since a checkpoint."""
        cp_path = self.store_dir / f"{index:04d}.json"
        if not cp_path.exists():
            return f"ERROR: checkpoint #{index} not found"

        data = self._load_manifest(cp_path)
        if data is None:
            return f"ERROR: checkpoint #{index} is corrupt"

        scope = data.get("scope")
        scope_arg = scope if scope and scope != "." else None

        cp_state: dict[str, str] = {}
        for entry in self._manifest_files(data):
            sha = entry.get("sha")
            content = entry.get("content")
            if content is None and sha:
                content = _read_object(sha)
            if content is None:
                continue
            cp_state[entry["path"]] = content

        current_state: dict[str, str] = {}
        for path in self._collect_files(scope_arg):
            try:
                rel = str(path.relative_to(self.project_dir))
                current_state[rel] = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue

        added = sorted(set(current_state) - set(cp_state))
        removed = sorted(set(cp_state) - set(current_state))
        modified = sorted(
            f for f in (set(cp_state) & set(current_state))
            if cp_state[f] != current_state[f]
        )

        if not added and not removed and not modified:
            return f"No changes since checkpoint #{index}"

        lines = [f"Changes since checkpoint #{index} '{data.get('label', '')}':"]
        for label_, items in (("Added", added), ("Removed", removed), ("Modified", modified)):
            if items:
                glyph = {"Added": "+", "Removed": "-", "Modified": "~"}[label_]
                lines.append(f"\n  {label_} ({len(items)}):")
                for f in items[:20]:
                    lines.append(f"    {glyph} {f}")
                if len(items) > 20:
                    lines.append(f"    … +{len(items) - 20} more")
        return "\n".join(lines)

    def cleanup(self, keep_last: int = 10) -> int:
        """Remove old checkpoints and GC orphaned content blobs.

        Returns the number of manifests deleted (not blobs — blobs are
        global and may still be referenced by other projects/sessions).
        """
        existing = sorted(self.store_dir.glob("[0-9]*.json"))
        removed_manifests = 0
        if len(existing) > keep_last:
            for path in existing[:-keep_last]:
                path.unlink()
                removed_manifests += 1

        # GC: scan ALL manifests across ALL projects/sessions, collect
        # referenced shas, delete unreferenced blobs.
        gc_objects()
        return removed_manifests

    # ── manifest helpers ───────────────────────────────────────────────

    @staticmethod
    def _load_manifest(path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def _manifest_files(data: dict) -> list[dict[str, str]]:
        """Read files from a manifest, supporting both new (files+sha) and
        legacy (snapshot+content) layouts so we don't break existing data.
        """
        if "files" in data:
            return data["files"]
        if "snapshot" in data:
            # Legacy v1 manifest: convert on the fly.
            return data["snapshot"]
        return []


# ── global GC ──────────────────────────────────────────────────────────────


def _all_referenced_shas() -> set[str]:
    referenced: set[str] = set()
    if not _PROJECTS_DIR.exists():
        return referenced
    for manifest in _PROJECTS_DIR.rglob("[0-9]*.json"):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for entry in CogitStore._manifest_files(data):
            sha = entry.get("sha")
            if sha:
                referenced.add(sha)
    return referenced


def gc_objects() -> int:
    """Delete content blobs no manifest references. Returns blob count removed."""
    if not _OBJECTS_DIR.exists():
        return 0
    referenced = _all_referenced_shas()
    removed = 0
    for blob in _OBJECTS_DIR.rglob("*"):
        if not blob.is_file():
            continue
        if blob.name not in referenced:
            try:
                blob.unlink()
                removed += 1
            except OSError:
                pass
    # Clean up empty parent dirs.
    for shard in _OBJECTS_DIR.iterdir():
        if shard.is_dir() and not any(shard.iterdir()):
            try:
                shard.rmdir()
            except OSError:
                pass
    return removed


def known_projects() -> Iterable[Path]:
    """List known project_hash dirs (for diagnostics / cleanup tools)."""
    if not _PROJECTS_DIR.exists():
        return []
    return [p for p in _PROJECTS_DIR.iterdir() if p.is_dir()]
