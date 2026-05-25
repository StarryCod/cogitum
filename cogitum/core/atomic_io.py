"""Crash-safe atomic file write helpers.

Two callers (sessions index, gateway tg_offset, future ones) need the
same recipe: write payload to a sibling ``.tmp``, fsync the file,
rename into place with ``os.replace``, then fsync the parent directory
so the rename itself is durable. Centralised here so a missed step
(parent fsync was the gap before Tier-4 R2) cannot regress in one
caller while the other is fixed.

Why parent dir fsync matters: ``os.replace`` updates the directory
inode but on POSIX that update is not persisted until either an
explicit ``fsync`` on the directory fd or an unrelated metadata
flush. Power-loss between rename and the next sync can leave the new
inode written but the directory still pointing at the old name —
boot returns the pre-write file. Skip on Windows where directory fds
do not support fsync.
"""

from __future__ import annotations

import itertools
import os
from pathlib import Path

# Per-process monotonic counter used by ``atomic_write_text`` to disambiguate
# concurrent writers targeting the same path — see the function docstring.
_TMP_COUNTER = itertools.count()


def _fsync_dir(path: Path) -> None:
    """Best-effort parent-directory fsync, POSIX only.

    Windows does not support opening a directory for fsync; the call
    raises ``PermissionError``. ``os.O_DIRECTORY`` is also missing on
    Windows. Probe the attribute and silently skip when absent.
    """
    o_directory = getattr(os, "O_DIRECTORY", None)
    if o_directory is None:
        return  # Windows / non-POSIX
    fd = None
    try:
        fd = os.open(str(path), o_directory)
        os.fsync(fd)
    except OSError:
        # Best-effort: if the dir can't be fsynced (NFS, exotic FS,
        # etc.), the rename has still happened — we just lose the
        # crash-durability guarantee for this one write.
        pass
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def atomic_write_text(path: Path, payload: str, *, encoding: str = "utf-8") -> None:
    """Write ``payload`` to ``path`` atomically with parent-dir fsync.

    Recipe: tmp file in the same directory → write+flush+fsync →
    ``os.replace`` → fsync parent dir. On any exception, the stale
    tmp file is removed (best-effort) and the exception re-raised
    with the original target untouched.

    The tmp suffix encodes pid + a per-process counter so two
    concurrent callers writing the SAME path don't collide on the
    same ``.tmp`` file (which would let writer B truncate writer A's
    in-flight buffer, or have writer A's cleanup ``unlink`` writer
    B's tmp). Last-writer-wins on ``os.replace`` is fine; torn
    content during the overlap is not.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(
        f"{path.suffix}.{os.getpid()}.{next(_TMP_COUNTER)}.tmp"
    )
    try:
        with open(tmp_path, "w", encoding=encoding) as f:
            f.write(payload)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, path)
        _fsync_dir(path.parent)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
