"""Tests for terminal tool — three modes, background lifecycle."""
from __future__ import annotations

import asyncio

import pytest

from cogitum.core.builtin_tools import terminal
from cogitum.core.process_manager import ProcessManager


@pytest.fixture(autouse=True)
def _fresh_pm():
    """Reset the singleton ProcessManager for each test."""
    ProcessManager._instance = None
    yield
    pm = ProcessManager._instance
    if pm:
        for bp in list(pm._processes.values()):
            if not bp.finished:
                try:
                    bp.proc.kill()
                except Exception:
                    pass
    ProcessManager._instance = None


def _pid_from_spawn(out: str) -> int:
    return int(out.split("PID")[1].split()[0])


@pytest.mark.asyncio
async def test_normal_mode_returns_output():
    out = await terminal(command="echo hello", mode="normal")
    assert "hello" in out


@pytest.mark.asyncio
async def test_normal_mode_returns_exit_code_on_failure():
    out = await terminal(command="false", mode="normal")
    assert "[exit 1]" in out


@pytest.mark.asyncio
async def test_timeout_mode_kills_long_command():
    out = await terminal(command="sleep 5", mode="timeout", timeout=1)
    assert "TIMEOUT" in out
    assert "1s" in out
    assert "background" in out.lower()


@pytest.mark.asyncio
async def test_timeout_mode_returns_normally_for_fast_command():
    out = await terminal(command="echo quick", mode="timeout", timeout=10)
    assert "quick" in out


@pytest.mark.asyncio
async def test_background_spawn_returns_pid():
    out = await terminal(command="sleep 2", mode="background")
    assert "OK: started background process PID" in out
    pid = _pid_from_spawn(out)
    await terminal(command="kill", mode="background", pid=pid)


@pytest.mark.asyncio
async def test_background_list_shows_running_process():
    spawn = await terminal(command="sleep 2", mode="background")
    pid = _pid_from_spawn(spawn)
    listing = await terminal(command="list", mode="background")
    assert f"PID {pid}" in listing
    assert "running" in listing
    await terminal(command="kill", mode="background", pid=pid)


@pytest.mark.asyncio
async def test_background_read_returns_output():
    spawn = await terminal(
        command="for i in 1 2 3; do echo line$i; sleep 0.1; done",
        mode="background",
    )
    pid = _pid_from_spawn(spawn)
    await asyncio.sleep(1.0)
    read_out = await terminal(
        command="read", mode="background", pid=pid, last_n=10
    )
    assert "line1" in read_out
    assert "line3" in read_out


@pytest.mark.asyncio
async def test_background_write_stdin():
    spawn = await terminal(command="head -n 1", mode="background")
    pid = _pid_from_spawn(spawn)
    write_out = await terminal(
        command="write", mode="background", pid=pid, stdin_data="hello-stdin"
    )
    assert "OK" in write_out
    await asyncio.sleep(0.5)
    read_out = await terminal(command="read", mode="background", pid=pid)
    assert "hello-stdin" in read_out


@pytest.mark.asyncio
async def test_background_kill():
    spawn = await terminal(command="sleep 30", mode="background")
    pid = _pid_from_spawn(spawn)
    kill_out = await terminal(command="kill", mode="background", pid=pid)
    assert "OK: killed" in kill_out


@pytest.mark.asyncio
async def test_background_close_stdin():
    spawn = await terminal(command="cat", mode="background")
    pid = _pid_from_spawn(spawn)
    close_out = await terminal(command="close", mode="background", pid=pid)
    assert "OK: closed stdin" in close_out
    await asyncio.sleep(0.5)
    listing = await terminal(command="list", mode="background")
    assert "exited" in listing or "No background" in listing


@pytest.mark.asyncio
async def test_background_missing_pid_errors_cleanly():
    out = await terminal(command="read", mode="background")
    assert "ERROR" in out
    assert "pid required" in out


@pytest.mark.asyncio
async def test_unknown_mode_errors():
    out = await terminal(command="echo x", mode="bogus")
    assert "ERROR" in out
    assert "bogus" in out
