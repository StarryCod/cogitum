"""
cogitum.core.mcp.watcher
~~~~~~~~~~~~~~~~~~~~~~~~

Lightweight file watcher for ``~/.config/cogitum/mcp.toml``.

Triggers a full MCP reconcile (via ``discover_mcp_tools``) whenever the
file's mtime or content hash changes. Runs as an asyncio task — designed
to live for the lifetime of the gateway / TUI process.

Why not inotify / watchdog: portability + zero deps. mtime polling at 1s
is more than enough for a config file the user edits maybe a few times
a day.

Usage
-----
::

    from cogitum.core.mcp.watcher import start_watcher
    task = start_watcher(rebuild_callback=my_async_rebuild)
    # ... later:
    task.cancel()

The callback signature is ``Callable[[], Awaitable[None]]`` — it gets
no arguments and is expected to do whatever the host needs (e.g. call
``discover_mcp_tools`` with the right registry + sampling callback).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable

from .config import config_path

log = logging.getLogger(__name__)

RebuildCallback = Callable[[], Awaitable[None]]

DEFAULT_POLL_INTERVAL = 1.5  # seconds


# ---------------------------------------------------------------------------
# Snapshot helper
# ---------------------------------------------------------------------------


def _snapshot(path: Path) -> tuple[float, str]:
    """
    Return ``(mtime, sha1)`` for ``path``.

    Missing file is represented as ``(0.0, "")``. The hash is over the raw
    bytes so we don't react to whitespace-only edits the way mtime alone
    would.
    """
    try:
        st = path.stat()
    except FileNotFoundError:
        return (0.0, "")
    try:
        data = path.read_bytes()
    except OSError:
        return (st.st_mtime, "")
    return (st.st_mtime, hashlib.sha1(data).hexdigest())


# ---------------------------------------------------------------------------
# Watcher loop
# ---------------------------------------------------------------------------


async def _watch_loop(
    rebuild: RebuildCallback,
    *,
    path: Path,
    interval: float,
) -> None:
    last = _snapshot(path)
    log.info("mcp watcher: watching %s (interval=%.1fs)", path, interval)
    while True:
        try:
            await asyncio.sleep(interval)
            cur = _snapshot(path)
            if cur == last:
                continue
            log.info(
                "mcp watcher: change detected (%s → %s); reconciling",
                last[1][:8] or "∅", cur[1][:8] or "∅",
            )
            last = cur
            try:
                await rebuild()
            except Exception:
                log.exception("mcp watcher: rebuild callback raised")
        except asyncio.CancelledError:
            log.debug("mcp watcher: cancelled")
            return
        except Exception:
            # Never let the watcher die on a transient error.
            log.exception("mcp watcher: unexpected error in loop")
            await asyncio.sleep(interval)


def start_watcher(
    rebuild_callback: RebuildCallback,
    *,
    path: Path | None = None,
    interval: float = DEFAULT_POLL_INTERVAL,
    loop: asyncio.AbstractEventLoop | None = None,
) -> asyncio.Task:
    """
    Start a background task that watches ``mcp.toml`` and calls
    ``rebuild_callback`` on every change.

    Returns the asyncio Task so the caller can cancel it on shutdown.
    """
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if loop is None:
        loop = asyncio.get_event_loop()
    return loop.create_task(
        _watch_loop(rebuild_callback, path=target, interval=interval),
        name="mcp-config-watcher",
    )
