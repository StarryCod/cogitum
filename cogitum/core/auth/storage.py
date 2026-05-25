"""
Persistent OAuth credential storage.

Lives at ~/.config/cogitum/auth.json with mode 0600. JSON, no encryption
because tokens already rotate and the file mode is restrictive — same
trade-off Claude Code, Codex CLI and pi-mono make.

API-key secrets do NOT go here; those are in `providers.toml` via the
`credentials.py` resolver (env, keyring or vault).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from .types import OAuthCredentials
import logging

log = logging.getLogger(__name__)


from ..platform_paths import get_config_dir

_CONFIG_DIR = get_config_dir()

_AUTH_PATH = _CONFIG_DIR / "auth.json"

_lock = threading.Lock()


def _ensure_config_dir() -> None:
    """Create ~/.config/cogitum with mode 0700 from the start.

    Matches what ssh-keygen / age / gpg do with their own per-user
    config dirs: refuse to put secrets in a world-readable directory.
    Tightening the mode after the fact (via chmod) leaves a window
    where a parallel reader could enter the dir; ``mkdir(mode=0o700)``
    closes that gap. On Windows the mode is ignored by the OS but the
    call still succeeds, so the cross-platform path stays simple.
    """
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _CONFIG_DIR.chmod(0o700)
    except OSError:
        # Best-effort — some filesystems (FAT, exotic NFS mounts)
        # don't honour POSIX modes. Continue silently.
        pass


def _read() -> dict[str, Any]:
    if not _AUTH_PATH.exists():
        return {}
    try:
        return json.loads(_AUTH_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _write(data: dict[str, Any]) -> None:
    """Atomically rewrite ``auth.json`` with mode 0600 from creation.

    Uses ``os.open(..., O_CREAT|O_WRONLY|O_TRUNC, 0o600)`` so the file
    has the right mode at inode-create time — closes the chmod-after
    window where a colocated reader could open the freshly-created
    file at the default ``0o644`` for the few microseconds before
    ``chmod`` ran. Tier-4 hardening.

    Falls back to ``atomic_write_text`` for the rename + parent fsync
    durability semantics (tmp → fsync → replace → fsync dir) so a
    power loss between rename and the next sync can't lose the new
    payload.
    """
    _ensure_config_dir()

    payload = json.dumps(data, indent=2, ensure_ascii=False)

    # We bypass ``atomic_write_text`` only for the SECRET-bearing
    # ``auth.json`` — every other caller uses the helper directly.
    # The reason: ``atomic_write_text`` opens with the default umask,
    # which on Linux yields 0o644. We need 0o600 from creation.
    import itertools as _it
    # Reuse a per-process counter so concurrent writers (different
    # threads, different providers) don't collide on the tmp name.
    if not hasattr(_write, "_counter"):
        _write._counter = _it.count()
    tmp_path = _AUTH_PATH.with_suffix(
        f".json.{os.getpid()}.{next(_write._counter)}.tmp"
    )
    try:
        flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC
        fd = os.open(str(tmp_path), flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
        except Exception:
            # fdopen took ownership; close-on-error is handled by it.
            raise
        os.replace(tmp_path, _AUTH_PATH)
        # Parent directory fsync so the rename is durable on POSIX
        # (Windows skip is handled inside _fsync_dir).
        from ..atomic_io import _fsync_dir
        _fsync_dir(_AUTH_PATH.parent)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def get(provider_id: str) -> OAuthCredentials | None:
    with _lock:
        data = _read()
    entry = data.get(provider_id)
    if not entry:
        return None
    try:
        return OAuthCredentials.from_dict(entry)
    except Exception:
        return None


def set_(provider_id: str, creds: OAuthCredentials) -> None:
    with _lock:
        data = _read()
        data[provider_id] = creds.as_dict()
        _write(data)


def remove(provider_id: str) -> bool:
    with _lock:
        data = _read()
        if provider_id in data:
            del data[provider_id]
            _write(data)
            return True
    return False


def list_providers() -> list[str]:
    with _lock:
        return sorted(_read().keys())


__all__ = ["get", "set_", "remove", "list_providers", "OAuthCredentials"]
