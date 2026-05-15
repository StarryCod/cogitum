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


_CONFIG_DIR = Path(
    os.environ.get("COGITUM_CONFIG_DIR")
    or os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
) / "cogitum"

_AUTH_PATH = _CONFIG_DIR / "auth.json"

_lock = threading.Lock()


def _read() -> dict[str, Any]:
    if not _AUTH_PATH.exists():
        return {}
    try:
        return json.loads(_AUTH_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _write(data: dict[str, Any]) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _AUTH_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(_AUTH_PATH)


def get(provider_id: str) -> OAuthCredentials | None:
    with _lock:
        data = _read()
    entry = data.get(provider_id)
    if not entry:
        return None
    try:
        return OAuthCredentials.from_dict(entry)
    except Exception:  # noqa: BLE001
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
