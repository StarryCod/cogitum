"""F22: process_manager must archive a finished entry when PID is reused."""
from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_pid_reuse_archives_old_entry():
    """When a finished BackgroundProcess shares a PID with a fresh spawn,
    the old entry is archived under '<pid>-old-<started_at>' instead of
    being silently overwritten.
    """
    from cogitum.core.process_manager import (
        BackgroundProcess,
        ProcessManager,
    )

    # Fresh manager — bypass the singleton so other tests aren't affected.
    pm = ProcessManager()

    # Plant a finished entry at PID 99999.
    class _FakeProc:
        def __init__(self):
            self.pid = 99999
            self.returncode = 0
            self.stdout = None
            self.stdin = None

        def kill(self):
            pass

    fake_finished = BackgroundProcess(
        pid=99999, proc=_FakeProc(), command="old: echo done"
    )
    fake_finished.finished = True
    fake_finished.exit_code = 0
    fake_finished.started_at = 1000.0
    pm._processes[99999] = fake_finished

    # Spawn a new process. We mock create_subprocess_shell so we can
    # control the PID it returns.
    captured = {}

    class _NewProc:
        def __init__(self):
            self.pid = 99999  # Same PID — reuse collision
            self.returncode = None
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_eof()
            self.stdin = None

    async def _fake_create(*args, **kwargs):
        captured["called"] = True
        return _NewProc()

    import cogitum.core.process_manager as pm_mod
    real = asyncio.create_subprocess_shell
    pm_mod.asyncio.create_subprocess_shell = _fake_create  # type: ignore
    try:
        bp = await pm.spawn("echo new")
    finally:
        pm_mod.asyncio.create_subprocess_shell = real  # type: ignore

    assert bp.command == "echo new"
    # Old entry must still be retrievable under archive key.
    archive_keys = [k for k in pm._processes if isinstance(k, str)]
    assert any("-old-" in k for k in archive_keys), (
        f"old finished entry must be archived; keys={list(pm._processes)}"
    )
    archive_key = next(k for k in archive_keys if "-old-" in k)
    archived = pm._processes[archive_key]
    assert archived.command == "old: echo done"
    # New entry sits at the integer PID.
    assert pm._processes[99999] is bp


@pytest.mark.asyncio
async def test_pid_reuse_skipped_when_no_collision():
    """No archive when PID isn't reused — sanity."""
    from cogitum.core.process_manager import ProcessManager

    pm = ProcessManager()
    before_keys = set(pm._processes.keys())

    class _NewProc:
        def __init__(self):
            self.pid = 88888
            self.returncode = None
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_eof()
            self.stdin = None

    async def _fake_create(*args, **kwargs):
        return _NewProc()

    import cogitum.core.process_manager as pm_mod
    real = asyncio.create_subprocess_shell
    pm_mod.asyncio.create_subprocess_shell = _fake_create  # type: ignore
    try:
        await pm.spawn("echo only")
    finally:
        pm_mod.asyncio.create_subprocess_shell = real  # type: ignore

    new_keys = set(pm._processes.keys()) - before_keys
    assert any("-old-" not in str(k) for k in new_keys)
    archive_count = sum(1 for k in new_keys if isinstance(k, str) and "-old-" in k)
    assert archive_count == 0, "no archive expected without collision"
