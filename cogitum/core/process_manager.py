"""
cogitum.core.process_manager
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Background process manager for the terminal tool.

Tracks background processes, allows reading output, writing stdin, killing.
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BackgroundProcess:
    """A tracked background process."""
    pid: int
    proc: asyncio.subprocess.Process
    command: str
    started_at: float = field(default_factory=time.time)
    output_lines: list[str] = field(default_factory=list)
    _reader_task: asyncio.Task | None = None
    finished: bool = False
    exit_code: int | None = None

    @property
    def uptime(self) -> float:
        return time.time() - self.started_at

    @property
    def status(self) -> str:
        if self.finished:
            return f"exited ({self.exit_code})"
        return "running"


class ProcessManager:
    """Singleton manager for background processes."""

    _instance: Optional["ProcessManager"] = None

    def __init__(self) -> None:
        self._processes: dict[int, BackgroundProcess] = {}

    @classmethod
    def get(cls) -> "ProcessManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def spawn(self, command: str, workdir: str | None = None) -> BackgroundProcess:
        """Start a background process and track it."""
        cwd = workdir or os.getcwd()
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.PIPE,
            cwd=cwd,
        )

        bp = BackgroundProcess(pid=proc.pid, proc=proc, command=command)
        self._processes[proc.pid] = bp

        # Start background reader
        bp._reader_task = asyncio.create_task(self._read_output(bp))
        return bp

    async def _read_output(self, bp: BackgroundProcess) -> None:
        """Continuously read output from the process."""
        try:
            while True:
                line = await bp.proc.stdout.readline()
                if not line:
                    break
                bp.output_lines.append(line.decode(errors="replace").rstrip("\n"))
                # Cap at 5000 lines (ring buffer behavior)
                if len(bp.output_lines) > 5000:
                    bp.output_lines = bp.output_lines[-3000:]
        except (asyncio.CancelledError, Exception):
            pass
        finally:
            bp.finished = True
            bp.exit_code = bp.proc.returncode

    def list_processes(self) -> list[BackgroundProcess]:
        """List all tracked processes."""
        return list(self._processes.values())

    def get_process(self, pid: int) -> BackgroundProcess | None:
        """Get a process by PID."""
        return self._processes.get(pid)

    async def kill(self, pid: int) -> str:
        """Kill a background process."""
        bp = self._processes.get(pid)
        if not bp:
            return f"ERROR: no process with PID {pid}"
        if bp.finished:
            return f"Process {pid} already finished (exit {bp.exit_code})"
        try:
            bp.proc.terminate()
            await asyncio.sleep(0.5)
            if bp.proc.returncode is None:
                bp.proc.kill()
            bp.finished = True
            bp.exit_code = bp.proc.returncode
            return f"OK: killed process {pid}"
        except Exception as e:
            return f"ERROR: {e}"

    async def write_stdin(self, pid: int, data: str) -> str:
        """Write data to a process's stdin."""
        bp = self._processes.get(pid)
        if not bp:
            return f"ERROR: no process with PID {pid}"
        if bp.finished:
            return f"ERROR: process {pid} already finished"
        if bp.proc.stdin is None:
            return f"ERROR: process {pid} has no stdin"
        try:
            bp.proc.stdin.write((data + "\n").encode())
            await bp.proc.stdin.drain()
            return f"OK: sent to PID {pid}"
        except Exception as e:
            return f"ERROR: {e}"

    def read_output(self, pid: int, last_n: int = 50) -> str:
        """Read last N lines of output from a process."""
        bp = self._processes.get(pid)
        if not bp:
            return f"ERROR: no process with PID {pid}"
        lines = bp.output_lines[-last_n:]
        if not lines:
            return "(no output yet)"
        header = f"[PID {pid} | {bp.status} | {bp.uptime:.0f}s uptime | {len(bp.output_lines)} total lines]"
        return header + "\n" + "\n".join(lines)

    def cleanup_finished(self) -> int:
        """Remove finished processes from tracking. Returns count removed."""
        to_remove = [pid for pid, bp in self._processes.items() if bp.finished]
        for pid in to_remove:
            if self._processes[pid]._reader_task:
                self._processes[pid]._reader_task.cancel()
            del self._processes[pid]
        return len(to_remove)
