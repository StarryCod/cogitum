"""F28: ProcessManager.spawn must serialise via _spawn_lock.

Without the lock, two concurrent spawns can both pass the
``_live_count() < MAX`` gate and over-spawn past the cap, defeating the
fork-bomb guardrail.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from cogitum.core import process_manager
from cogitum.core.process_manager import (
    MAX_BACKGROUND_PROCESSES,
    ProcessLimitExceeded,
    ProcessManager,
)


def test_spawn_lock_attribute_exists():
    """ProcessManager.__init__ creates the asyncio.Lock used by spawn()."""
    pm = ProcessManager()
    assert isinstance(pm._spawn_lock, asyncio.Lock)


@pytest.mark.asyncio
async def test_concurrent_spawns_do_not_exceed_cap(monkeypatch):
    """Two spawns racing past the cap should one error, not both succeed."""
    pm = ProcessManager()

    # Pre-populate _processes up to MAX-1 so the *very next* spawn is
    # the one that fills the cap. A racing concurrent spawn must hit
    # ProcessLimitExceeded.
    class _FakeProc:
        def __init__(self, pid):
            self.pid = pid
            self.returncode = None

    for i in range(MAX_BACKGROUND_PROCESSES - 1):
        bp = process_manager.BackgroundProcess(
            pid=10000 + i, proc=_FakeProc(10000 + i), command="fake"
        )
        pm._processes[bp.pid] = bp

    # Now stub create_subprocess_shell so we don't really fork — just hand
    # back a fake proc with an incrementing pid.
    counter = {"i": 0}

    async def fake_create(*a, **kw):
        counter["i"] += 1
        return _FakeProc(20000 + counter["i"])

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create)

    # Also stub the reader so it returns immediately.
    async def fake_read(self, bp):
        bp.finished = True

    monkeypatch.setattr(ProcessManager, "_read_output", fake_read)

    # Race two spawns. With the lock, exactly one wins.
    async def attempt():
        try:
            return await pm.spawn("echo hi")
        except ProcessLimitExceeded:
            return "blocked"

    results = await asyncio.gather(attempt(), attempt())
    blocked = sum(1 for r in results if r == "blocked")
    success = sum(1 for r in results if r != "blocked")

    # Lock prevents both from squeezing past the cap.
    assert blocked >= 1, f"both spawns succeeded — race past cap: {results!r}"
    assert success <= 1


@pytest.mark.asyncio
async def test_spawn_lock_serialises_create_calls(monkeypatch):
    """Concurrent spawns observe each other's mutation through the lock."""
    pm = ProcessManager()

    in_flight = {"max": 0, "now": 0}

    class _FakeProc:
        def __init__(self, pid):
            self.pid = pid
            self.returncode = None

    pid_counter = {"i": 30000}

    async def slow_create(*a, **kw):
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        await asyncio.sleep(0.05)
        in_flight["now"] -= 1
        pid_counter["i"] += 1
        return _FakeProc(pid_counter["i"])

    monkeypatch.setattr(asyncio, "create_subprocess_shell", slow_create)

    async def fake_read(self, bp):
        bp.finished = True

    monkeypatch.setattr(ProcessManager, "_read_output", fake_read)

    await asyncio.gather(*[pm.spawn(f"echo {i}") for i in range(5)])
    # The lock means at most ONE create_subprocess_shell is in flight
    # at any moment.
    assert in_flight["max"] == 1, (
        f"spawn lock failed to serialise: max concurrency was {in_flight['max']}"
    )
