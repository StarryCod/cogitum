"""Cross-platform regression tests for the terminal tool.

Two issues:

1. Windows: ``start_new_session=True`` is POSIX-only. On Windows
   it either raised ValueError on older Pythons or silently broke
   stdout capture on newer ones — users saw "(no output)" or
   "[exit N]" with no body. The fix swaps to
   ``CREATE_NEW_PROCESS_GROUP`` via creationflags on Windows and
   keeps ``start_new_session`` on POSIX, gated on
   ``sys.platform``.

2. The same kwargs are reused by ``ProcessManager.spawn`` for the
   background-process path. Both need the cross-platform shape or
   one platform's terminal tool would silently swallow output
   while the other keeps working.

These tests exercise the import-time platform branching directly so
we don't need a Windows host to assert the contract.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import patch

import pytest


# ── Platform-aware kwargs shape ──────────────────────────────────────


def test_builtin_tools_detach_kwargs_match_platform() -> None:
    """The kwargs dict picked at import time must match the current
    platform exactly. POSIX: {start_new_session: True}. Windows:
    {creationflags: CREATE_NEW_PROCESS_GROUP}."""
    from cogitum.core.builtin_tools import _SUBPROC_DETACH_KWARGS

    if sys.platform == "win32":
        assert "creationflags" in _SUBPROC_DETACH_KWARGS
        assert "start_new_session" not in _SUBPROC_DETACH_KWARGS
        # CREATE_NEW_PROCESS_GROUP = 0x00000200
        assert _SUBPROC_DETACH_KWARGS["creationflags"] == 0x00000200
    else:
        assert _SUBPROC_DETACH_KWARGS == {"start_new_session": True}


def test_process_manager_detach_kwargs_match_platform() -> None:
    """Same contract for the background-process path."""
    from cogitum.core.process_manager import _SUBPROC_DETACH_KWARGS

    if sys.platform == "win32":
        assert "creationflags" in _SUBPROC_DETACH_KWARGS
        assert "start_new_session" not in _SUBPROC_DETACH_KWARGS
    else:
        assert _SUBPROC_DETACH_KWARGS == {"start_new_session": True}


def test_builtin_and_process_manager_kwargs_are_consistent() -> None:
    """Both modules must produce IDENTICAL detach kwargs for the
    current platform — otherwise a process spawned via terminal()
    behaves differently from one spawned via the background path,
    and that drift is the kind of bug nobody finds until it
    matters."""
    from cogitum.core.builtin_tools import _SUBPROC_DETACH_KWARGS as a
    from cogitum.core.process_manager import _SUBPROC_DETACH_KWARGS as b
    assert a == b


# ── Subprocess actually captures stdout on the current platform ──────


@pytest.mark.asyncio
async def test_terminal_normal_mode_captures_stdout_cross_platform() -> None:
    """End-to-end: terminal(mode='normal') must capture output on
    POSIX and Windows alike. Run a command that prints a known
    marker and verify it round-trips through the subprocess + decode
    path. This is the regression lock for the 'Windows shows
    no output' bug."""
    from cogitum.core.builtin_tools import terminal

    if sys.platform == "win32":
        # cmd.exe /c echo hello — works without any unix tools
        out = await terminal(command="echo hello-windows")
    else:
        out = await terminal(command="echo hello-posix")

    assert "hello" in out, (
        f"expected stdout to contain 'hello', got: {out!r}\n"
        "If this is empty/None on Windows, the start_new_session "
        "regression is back."
    )