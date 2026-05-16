"""Tests for the refactored cogit checkpoint store.

Covers:
- Critical [C3]: .gitignore is respected (root patterns + defaults)
- Critical [C4]: restore deletes orphan files
- Critical [C5]: content-addressable dedup (one blob, many references)
- Critical [C6]: cleanup GC removes unreferenced blobs
- Critical [C9]: cross-project isolation (same session_id, different dirs)
- Auto-safety: pre-restore checkpoint is created
- Backward compat: legacy snapshot manifests still load
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cogitum.core import cogit as cogit_mod
from cogitum.core.cogit import CogitStore, gc_objects, _project_hash


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """Redirect _COGIT_ROOT et al. to tmp_path so tests don't touch
    real ~/.config/cogitum/cogits/.
    """
    fake_root = tmp_path / "cogits"
    monkeypatch.setattr(cogit_mod, "_COGIT_ROOT", fake_root)
    monkeypatch.setattr(cogit_mod, "_OBJECTS_DIR", fake_root / "objects")
    monkeypatch.setattr(cogit_mod, "_PROJECTS_DIR", fake_root / "projects")
    return fake_root


def _project(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write(p: Path, rel: str, content: str) -> None:
    target = p / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


# ── C9: cross-project isolation ────────────────────────────────────────────


def test_cross_project_isolation(isolated_store, tmp_path):
    """Same session_id, two project dirs → two separate manifest spaces."""
    proj_a = _project(tmp_path, "proj_a")
    proj_b = _project(tmp_path, "proj_b")
    _write(proj_a, "a.txt", "A")
    _write(proj_b, "b.txt", "B")

    store_a = CogitStore(session_id="default", project_dir=proj_a)
    store_b = CogitStore(session_id="default", project_dir=proj_b)

    store_a.save(label="a-only")
    store_b.save(label="b-only")

    # Each store sees only its own checkpoints.
    list_a = store_a.list_checkpoints()
    list_b = store_b.list_checkpoints()
    assert len(list_a) == 1
    assert len(list_b) == 1
    assert list_a[0].label == "a-only"
    assert list_b[0].label == "b-only"

    # Project hashes differ.
    assert _project_hash(proj_a.resolve()) != _project_hash(proj_b.resolve())


def test_restore_refuses_wrong_project_dir(isolated_store, tmp_path):
    """If a manifest was saved in /A but the store now points at /B,
    restore must refuse — not silently overwrite /B with /A's files."""
    proj_a = _project(tmp_path, "proj_a")
    proj_b = _project(tmp_path, "proj_b")
    _write(proj_a, "a.txt", "from A")

    store_a = CogitStore(session_id="default", project_dir=proj_a)
    store_a.save(label="a")

    # Manually copy A's manifest into B's manifest dir to simulate
    # accidental cross-pollination.
    manifest_a = next((cogit_mod._PROJECTS_DIR / _project_hash(proj_a.resolve()) / "default").glob("*.json"))
    target_dir = cogit_mod._PROJECTS_DIR / _project_hash(proj_b.resolve()) / "default"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "0001.json").write_text(manifest_a.read_text(), encoding="utf-8")

    store_b = CogitStore(session_id="default", project_dir=proj_b)
    msg = store_b.restore(1)
    assert msg.startswith("ERROR:")
    assert "refusing" in msg
    assert not (proj_b / "a.txt").exists()


# ── C3: .gitignore awareness ────────────────────────────────────────────────


def test_gitignore_excludes_files(isolated_store, tmp_path):
    proj = _project(tmp_path, "proj")
    _write(proj, ".gitignore", "secrets.txt\nbuild/\n*.log\n")
    _write(proj, "main.py", "print('hi')")
    _write(proj, "secrets.txt", "TOKEN=xxx")
    _write(proj, "build/artifact.bin", "binary")
    _write(proj, "app.log", "noise")

    store = CogitStore(session_id="s", project_dir=proj)
    cp = store.save(label="t")
    # Only main.py + .gitignore should be saved.
    paths = _saved_paths(store, cp.index)
    assert "main.py" in paths
    assert "secrets.txt" not in paths
    assert "app.log" not in paths
    assert not any(p.startswith("build/") for p in paths)


def test_default_ignore_excludes_dotenv(isolated_store, tmp_path):
    proj = _project(tmp_path, "proj")
    _write(proj, ".env", "API_KEY=leak")
    _write(proj, "secrets.env", "SECRET=oops")
    _write(proj, "main.py", "ok")

    store = CogitStore(session_id="s", project_dir=proj)
    cp = store.save(label="t")
    paths = _saved_paths(store, cp.index)
    assert "main.py" in paths
    assert ".env" not in paths
    assert "secrets.env" not in paths


# ── C4: restore deletes orphans ─────────────────────────────────────────────


def test_restore_deletes_orphan_files(isolated_store, tmp_path):
    proj = _project(tmp_path, "proj")
    _write(proj, "a.txt", "A")
    _write(proj, "b.txt", "B")

    store = CogitStore(session_id="s", project_dir=proj)
    store.save(label="snapshot")

    # Agent creates a new file after the checkpoint.
    _write(proj, "garbage.txt", "should be removed on restore")
    assert (proj / "garbage.txt").exists()

    msg = store.restore(1)
    assert msg.startswith("OK:")
    assert "removed 1 orphan" in msg
    assert not (proj / "garbage.txt").exists()
    assert (proj / "a.txt").read_text() == "A"
    assert (proj / "b.txt").read_text() == "B"


def test_restore_orphan_respects_scope(isolated_store, tmp_path):
    """Scope-restricted restore must only delete orphans inside the scope."""
    proj = _project(tmp_path, "proj")
    _write(proj, "src/a.py", "A")
    _write(proj, "docs/readme.md", "doc")

    store = CogitStore(session_id="s", project_dir=proj)
    store.save(label="src-only", scope="src")

    # Add orphan inside scope and outside scope.
    _write(proj, "src/b.py", "should be removed (in scope)")
    _write(proj, "docs/extra.md", "should survive (out of scope)")

    msg = store.restore(1)
    assert "OK" in msg
    assert not (proj / "src/b.py").exists()
    assert (proj / "docs/extra.md").exists()


# ── C5: content-addressable dedup ───────────────────────────────────────────


def test_dedup_two_checkpoints_share_blobs(isolated_store, tmp_path):
    proj = _project(tmp_path, "proj")
    _write(proj, "a.txt", "AAA")
    _write(proj, "b.txt", "BBB")

    store = CogitStore(session_id="s", project_dir=proj)
    store.save(label="cp1")
    store.save(label="cp2")  # identical content → same blobs reused

    blobs = list(cogit_mod._OBJECTS_DIR.rglob("*"))
    blobs = [b for b in blobs if b.is_file()]
    # Exactly two unique blobs: one per unique content string.
    assert len(blobs) == 2


def test_changed_file_creates_new_blob_old_kept(isolated_store, tmp_path):
    proj = _project(tmp_path, "proj")
    _write(proj, "a.txt", "v1")
    store = CogitStore(session_id="s", project_dir=proj)
    store.save(label="cp1")
    _write(proj, "a.txt", "v2")
    store.save(label="cp2")

    blobs = [b for b in cogit_mod._OBJECTS_DIR.rglob("*") if b.is_file()]
    assert len(blobs) == 2  # v1 and v2 both kept (cp1 references v1)


# ── C6: cleanup GC ─────────────────────────────────────────────────────────


def test_cleanup_gc_removes_unreferenced_blobs(isolated_store, tmp_path):
    proj = _project(tmp_path, "proj")
    _write(proj, "a.txt", "v1")
    store = CogitStore(session_id="s", project_dir=proj)
    store.save(label="cp1")
    _write(proj, "a.txt", "v2")
    store.save(label="cp2")
    _write(proj, "a.txt", "v3")
    store.save(label="cp3")

    # Keep only the last 1 → cp1 and cp2 manifests deleted, but blobs may
    # linger until GC runs.
    removed = store.cleanup(keep_last=1)
    assert removed == 2

    # gc_objects() runs inside cleanup — old v1/v2 blobs should be gone.
    blobs = [b for b in cogit_mod._OBJECTS_DIR.rglob("*") if b.is_file()]
    assert len(blobs) == 1
    assert blobs[0].read_text() == "v3"


def test_gc_objects_keeps_referenced_blobs_across_projects(isolated_store, tmp_path):
    """Two projects, both reference the same content. GC must not remove
    a blob just because one project's manifest was deleted."""
    proj_a = _project(tmp_path, "a")
    proj_b = _project(tmp_path, "b")
    _write(proj_a, "x.txt", "shared")
    _write(proj_b, "y.txt", "shared")

    store_a = CogitStore(session_id="s", project_dir=proj_a)
    store_b = CogitStore(session_id="s", project_dir=proj_b)
    store_a.save(label="a")
    store_b.save(label="b")

    blobs = [b for b in cogit_mod._OBJECTS_DIR.rglob("*") if b.is_file()]
    assert len(blobs) == 1  # dedup across projects works

    # Delete A's manifest, run GC — blob still referenced by B.
    for m in (cogit_mod._PROJECTS_DIR / _project_hash(proj_a.resolve()) / "s").glob("*.json"):
        m.unlink()
    removed = gc_objects()
    assert removed == 0
    assert len([b for b in cogit_mod._OBJECTS_DIR.rglob("*") if b.is_file()]) == 1


# ── auto-safety pre-restore ─────────────────────────────────────────────────


def test_restore_creates_pre_restore_checkpoint(isolated_store, tmp_path):
    proj = _project(tmp_path, "proj")
    _write(proj, "a.txt", "original")
    store = CogitStore(session_id="s", project_dir=proj)
    store.save(label="snapshot")

    # User edits after save — these edits must be recoverable.
    _write(proj, "a.txt", "user changes I might want back")

    msg = store.restore(1)
    assert "pre-restore safety" in msg

    # Pre-restore checkpoint exists and contains the user's edits.
    cps = store.list_checkpoints()
    pre = [c for c in cps if "pre_restore" in c.label]
    assert pre, f"expected pre-restore checkpoint, got {[c.label for c in cps]}"


# ── backward compat: legacy snapshot manifests ──────────────────────────────


def test_legacy_snapshot_manifest_loads(isolated_store, tmp_path):
    """Old manifests (snapshot=[{path,content}]) must still list and restore."""
    proj = _project(tmp_path, "proj")
    _write(proj, "a.txt", "live")

    store = CogitStore(session_id="s", project_dir=proj)
    # Hand-craft a legacy v1 manifest into the right project dir.
    manifest_dir = cogit_mod._PROJECTS_DIR / _project_hash(proj.resolve()) / "s"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    legacy = {
        "index": 1,
        "label": "legacy",
        "timestamp": 1.0,
        "scope": ".",
        "project_dir": str(proj.resolve()),
        "snapshot": [{"path": "a.txt", "content": "from legacy"}],
    }
    (manifest_dir / "0001.json").write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
    )

    cps = store.list_checkpoints()
    assert len(cps) == 1
    assert cps[0].label == "legacy"
    assert cps[0].file_count == 1

    msg = store.restore(1, auto_safety=False)
    assert msg.startswith("OK:")
    assert (proj / "a.txt").read_text() == "from legacy"


# ── helpers ─────────────────────────────────────────────────────────────────


def _saved_paths(store: CogitStore, index: int) -> set[str]:
    manifest = store.store_dir / f"{index:04d}.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    return {entry["path"] for entry in CogitStore._manifest_files(data)}
