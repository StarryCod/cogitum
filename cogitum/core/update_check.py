"""
cogitum.core.update_check
~~~~~~~~~~~~~~~~~~~~~~~~~

Lightweight background "is there a newer version?" probe.

Strategy:
  1. Fetch ``pyproject.toml`` from
     ``https://raw.githubusercontent.com/StarryCod/cogitum/master/pyproject.toml``
     (CDN, no auth, no rate limit for sane traffic).
  2. Parse out ``version = "..."`` from the ``[project]`` table.
  3. Compare against the installed :data:`cogitum.__version__`.
  4. Cache the answer for 12 hours under
     ``<cache_dir>/update-check.json`` so we don't pound GitHub on
     every TUI launch.

Failures (no network, GitHub down, malformed response) are
non-fatal — they leave the cache untouched and return
``current=installed, latest=None``. The TUI must treat ``None`` as
"unknown, don't show banner".

The choice of raw github over GitHub releases is deliberate: the
project doesn't tag releases, master IS the rolling source of
truth. If we ever start cutting releases we can switch this to the
``/releases/latest`` endpoint without changing the public API
(:func:`check`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────

_PYPROJECT_URL = "https://raw.githubusercontent.com/StarryCod/cogitum/master/pyproject.toml"
# A 12h cache is the right balance: short enough that a hot fix
# reaches users within a working day, long enough that someone
# launching `cog` 50 times to debug doesn't hammer the CDN.
_CACHE_TTL_S = 12 * 3600
# Network timeout — we never want this probe to slow down TUI start.
# 4 seconds is generous for a CDN hop; if the network is worse than
# that, the user's bigger problems aren't related to update checks.
_NETWORK_TIMEOUT_S = 4.0


# ─────────────────────────────────────────────────────────────────────────
# Public dataclass
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class UpdateInfo:
    """Result of an update check.

    Attributes:
      current:    installed version (read from cogitum.__version__).
      latest:     latest seen on master, or ``None`` if the probe failed.
      newer:      ``True`` iff ``latest`` is strictly higher than
                  ``current`` under semver-ish tuple comparison.
                  Always ``False`` when ``latest is None``.
      install_method: "npm" / "pip" / "source" — best-effort detection
                  to recommend the right update command in the banner.
    """

    current: str
    latest: str | None
    newer: bool
    install_method: str

    def upgrade_command(self) -> str:
        """Recommend a one-liner to upgrade based on install_method."""
        if self.install_method == "npm":
            return "cog --update"          # npm wrapper handles git pull + reinstall
        if self.install_method == "pip":
            return "pip install -U cogitum"
        # source / unknown: assume the user cloned the repo
        return "cd <cogitum repo> && git pull && pip install -e ."


# ─────────────────────────────────────────────────────────────────────────
# Version parsing + comparison
# ─────────────────────────────────────────────────────────────────────────


_VERSION_RE = re.compile(
    r"""^\s*\[project\][^\[]*?\bversion\s*=\s*["']([^"']+)["']""",
    re.MULTILINE | re.DOTALL,
)


def _parse_pyproject_version(text: str) -> str | None:
    """Pull the ``version`` value from the ``[project]`` table of a
    pyproject.toml string. Returns None on parse failure.

    We hand-roll this rather than using ``tomllib`` because the
    function is called from contexts that may not have the full
    Python import machinery wired up yet (e.g. early CLI startup).
    Regex is plenty for one well-known field.
    """
    m = _VERSION_RE.search(text)
    return m.group(1).strip() if m else None


def _split_version(v: str) -> tuple[int, ...]:
    """Return a comparable tuple. '1.2.3' → (1,2,3). Non-numeric
    suffixes (rc1, dev0) are dropped before tuple-ing so 'newer'
    decisions remain monotonic on prereleases."""
    parts: list[int] = []
    for chunk in v.strip().split("."):
        m = re.match(r"^(\d+)", chunk)
        if m:
            parts.append(int(m.group(1)))
        else:
            break
    return tuple(parts) or (0,)


def is_newer(latest: str, current: str) -> bool:
    """``True`` iff *latest* is strictly higher than *current*.

    Tolerant of trailing pre-release tags ('1.2.0rc1' > '1.1.9'
    because we compare the leading numeric tuple). Strictness is
    the right default — we don't want a banner shouting "update!"
    when versions are equal.
    """
    return _split_version(latest) > _split_version(current)


# ─────────────────────────────────────────────────────────────────────────
# Install-method detection
# ─────────────────────────────────────────────────────────────────────────


def detect_install_method() -> str:
    """Best-effort guess at how the user's Cogitum got installed.

    Returns one of: ``"npm"``, ``"pip"``, ``"source"``.

    Heuristics, in order of confidence:
      1. ``COGITUM_HOME`` env var → set by the npm launcher.
      2. The Cogitum package's ``__file__`` lives under
         ``.local/share/cogitum`` (POSIX) or ``LOCALAPPDATA/cogitum``
         (Windows) — that's where the npm wrapper clones.
      3. ``cogitum.dist-info`` exists and was created from a wheel
         (no editable install marker) → pip from PyPI / similar.
      4. Otherwise: assume source (editable / `pip install -e .`).
    """
    import os
    import sys

    if os.environ.get("COGITUM_HOME"):
        return "npm"

    try:
        import cogitum
        pkg_path = Path(cogitum.__file__).resolve()
    except Exception:
        return "source"

    pkg_str = str(pkg_path)
    npm_markers = (".local/share/cogitum", "AppData\\Local\\cogitum",
                   "Library/Application Support/cogitum")
    if any(m in pkg_str for m in npm_markers):
        return "npm"

    # Look for an editable-install marker. setuptools writes a .pth
    # file pointing at the source dir; pip's editable installs leave
    # a ``__editable__`` shim or a direct_url.json with editable=True.
    try:
        from importlib.metadata import distribution
        dist = distribution("cogitum")
        files = dist.files or []
        for f in files:
            name = str(f)
            if "direct_url.json" in name:
                content = json.loads(dist.read_text(name) or "{}")
                if (content.get("dir_info") or {}).get("editable"):
                    return "source"
        return "pip"
    except Exception:
        return "source"


# ─────────────────────────────────────────────────────────────────────────
# Cache
# ─────────────────────────────────────────────────────────────────────────


def _cache_path() -> Path:
    from .platform_paths import get_cache_dir
    return get_cache_dir() / "update-check.json"


def _read_cache() -> dict | None:
    p = _cache_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    fetched_at = data.get("fetched_at", 0)
    if not isinstance(fetched_at, (int, float)):
        return None
    if time.time() - fetched_at > _CACHE_TTL_S:
        return None
    return data


def _write_cache(latest: str) -> None:
    p = _cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"latest": latest, "fetched_at": time.time()}
    try:
        p.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as e:
        logger.debug("update_check: cache write failed: %s", e)


# ─────────────────────────────────────────────────────────────────────────
# The actual probe
# ─────────────────────────────────────────────────────────────────────────


async def _fetch_latest_version() -> str | None:
    """One-shot HTTP GET against the master pyproject.toml.

    Returns the parsed version string on success, ``None`` on any
    failure. Logs at DEBUG only — we don't want a network glitch to
    pollute the user's TUI.
    """
    try:
        import httpx
    except ImportError:
        logger.debug("update_check: httpx not available")
        return None

    try:
        async with httpx.AsyncClient(timeout=_NETWORK_TIMEOUT_S) as client:
            resp = await client.get(
                _PYPROJECT_URL,
                headers={
                    "User-Agent": "cogitum-update-check",
                    "Accept": "text/plain",
                },
            )
        if resp.status_code != 200:
            logger.debug("update_check: status %d", resp.status_code)
            return None
        return _parse_pyproject_version(resp.text)
    except Exception as e:
        logger.debug("update_check: probe failed: %s", e)
        return None


async def check(*, force: bool = False) -> UpdateInfo:
    """Public entry point.

    Returns an :class:`UpdateInfo`. Honours the 12h cache unless
    ``force=True`` (used by an explicit ``cog --check-update`` if
    we ever add one).
    """
    from .. import __version__ as installed
    method = detect_install_method()

    cached = None if force else _read_cache()
    if cached is not None:
        latest = cached.get("latest")
        return UpdateInfo(
            current=installed,
            latest=latest if isinstance(latest, str) else None,
            newer=isinstance(latest, str) and is_newer(latest, installed),
            install_method=method,
        )

    latest = await _fetch_latest_version()
    if latest:
        _write_cache(latest)

    return UpdateInfo(
        current=installed,
        latest=latest,
        newer=bool(latest) and is_newer(latest, installed),
        install_method=method,
    )


__all__ = [
    "UpdateInfo",
    "check",
    "detect_install_method",
    "is_newer",
]
