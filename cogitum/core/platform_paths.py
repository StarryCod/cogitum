"""
cogitum.core.platform_paths
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Cross-platform configuration / data / log paths for Cogitum.

The original codebase hard-coded ``~/.config/cogitum`` everywhere — fine
on Linux and tolerated on macOS, but on Windows that puts state in
``C:\\Users\\<user>\\.config\\cogitum`` which collides with OneDrive
sync, breaks if the user has redirected ``%USERPROFILE%``, and isn't
where Windows-native tooling looks for app data.

This module is the single source of truth. Everything that needs a
config / data / log / cache directory imports from here. Per platform:

  Linux:
    config  → $XDG_CONFIG_HOME/cogitum  (default ~/.config/cogitum)
    data    → $XDG_DATA_HOME/cogitum    (default ~/.local/share/cogitum)
    log     → $XDG_STATE_HOME/cogitum   (default ~/.local/state/cogitum)
    cache   → $XDG_CACHE_HOME/cogitum   (default ~/.cache/cogitum)

  macOS:
    config  → ~/Library/Application Support/cogitum
    data    → ~/Library/Application Support/cogitum
    log     → ~/Library/Logs/cogitum
    cache   → ~/Library/Caches/cogitum

  Windows:
    config  → %APPDATA%\\cogitum             (roaming, syncs across devices)
    data    → %LOCALAPPDATA%\\cogitum        (local, large files)
    log     → %LOCALAPPDATA%\\cogitum\\logs   (local)
    cache   → %LOCALAPPDATA%\\cogitum\\cache  (local)

Override knobs (always honoured if set):
  COGITUM_CONFIG_DIR  → forces the config dir (used by tests + power users)
  COGITUM_DATA_DIR    → forces the data dir
  COGITUM_LOG_DIR     → forces the log dir
  COGITUM_CACHE_DIR   → forces the cache dir

All getters create the directory on demand and return an absolute
``pathlib.Path``. They are idempotent — call as many times as you like.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


__all__ = [
    "get_config_dir",
    "get_data_dir",
    "get_log_dir",
    "get_cache_dir",
    "is_windows",
    "is_macos",
    "is_linux",
]


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def is_windows() -> bool:
    return sys.platform == "win32"


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


# ---------------------------------------------------------------------------
# Internal: per-platform base directories
# ---------------------------------------------------------------------------

def _windows_appdata_roaming() -> Path:
    """%APPDATA% — roaming AppData. Falls back to ~/AppData/Roaming."""
    return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))


def _windows_localappdata() -> Path:
    """%LOCALAPPDATA% — local AppData. Falls back to ~/AppData/Local."""
    return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))


def _xdg(env_name: str, fallback_segment: str) -> Path:
    """Resolve an XDG base directory with a HOME-relative fallback."""
    explicit = os.environ.get(env_name)
    if explicit:
        return Path(explicit)
    return Path.home() / fallback_segment


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_config_dir() -> Path:
    """Where Cogitum stores user-editable config (providers.toml,
    settings.toml, mcp.toml, prefill.json, secrets.env, auth.json).

    Precedence: ``$COGITUM_CONFIG_DIR`` → platform default.

    For backward compatibility with the historical layout that tracked
    ``XDG_CONFIG_HOME``: ``COGITUM_CONFIG_DIR`` is treated as a *parent*
    that gets ``cogitum`` appended (matching the legacy
    ``$XDG_CONFIG_HOME/cogitum`` convention), but only if the parent
    doesn't already end in ``cogitum``. New callers can also pass an
    explicit absolute path that already includes the segment.

    Always returns an existing directory.
    """
    explicit = os.environ.get("COGITUM_CONFIG_DIR")
    if explicit:
        path = Path(explicit)
        if path.name != "cogitum":
            path = path / "cogitum"
    elif is_windows():
        path = _windows_appdata_roaming() / "cogitum"
    elif is_macos():
        path = Path.home() / "Library" / "Application Support" / "cogitum"
    else:
        path = _xdg("XDG_CONFIG_HOME", ".config") / "cogitum"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_data_dir() -> Path:
    """Where Cogitum stores larger generated data (sessions/*.jsonl,
    skills/**, memory/*.md, cogit checkpoints, web_auth Chromium
    profiles).

    On Linux this is XDG_DATA_HOME ($HOME/.local/share by default) so
    the directory survives config-dir wipes. On macOS we collapse to
    Application Support to match platform conventions. On Windows we
    use %LOCALAPPDATA% so OneDrive doesn't try to sync gigabyte-scale
    Chromium profiles or session logs.

    Precedence: ``$COGITUM_DATA_DIR`` → platform default.
    """
    explicit = os.environ.get("COGITUM_DATA_DIR")
    if explicit:
        path = Path(explicit)
    elif is_windows():
        path = _windows_localappdata() / "cogitum"
    elif is_macos():
        path = Path.home() / "Library" / "Application Support" / "cogitum"
    else:
        path = _xdg("XDG_DATA_HOME", ".local/share") / "cogitum"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_log_dir() -> Path:
    """Where Cogitum writes log files. On Linux this is
    XDG_STATE_HOME (per the freedesktop spec for runtime state). On
    macOS, ~/Library/Logs/cogitum. On Windows, a ``logs`` subdir of
    %LOCALAPPDATA%\\cogitum.

    Precedence: ``$COGITUM_LOG_DIR`` → platform default.
    """
    explicit = os.environ.get("COGITUM_LOG_DIR")
    if explicit:
        path = Path(explicit)
    elif is_windows():
        path = _windows_localappdata() / "cogitum" / "logs"
    elif is_macos():
        path = Path.home() / "Library" / "Logs" / "cogitum"
    else:
        path = _xdg("XDG_STATE_HOME", ".local/state") / "cogitum"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_cache_dir() -> Path:
    """Where Cogitum writes ephemeral cache (model resolution cache,
    HTTP response cache, etc.). Safe to wipe at any time.

    Precedence: ``$COGITUM_CACHE_DIR`` → platform default.
    """
    explicit = os.environ.get("COGITUM_CACHE_DIR")
    if explicit:
        path = Path(explicit)
    elif is_windows():
        path = _windows_localappdata() / "cogitum" / "cache"
    elif is_macos():
        path = Path.home() / "Library" / "Caches" / "cogitum"
    else:
        path = _xdg("XDG_CACHE_HOME", ".cache") / "cogitum"
    path.mkdir(parents=True, exist_ok=True)
    return path
