"""
cogitum.core.process_manager
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Background process manager for the terminal tool.

Tracks background processes, allows reading output, writing stdin, killing.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


log = logging.getLogger(__name__)


# Detach kwargs — same rationale as builtin_tools._SUBPROC_DETACH_KWARGS.
# `start_new_session=True` is POSIX-only; on Windows we need
# CREATE_NEW_PROCESS_GROUP via creationflags or stdout capture breaks
# silently. Computed at import time, per platform.
if sys.platform == "win32":
    _SUBPROC_DETACH_KWARGS: dict = {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,  # type: ignore[attr-defined]
    }
else:
    _SUBPROC_DETACH_KWARGS = {"start_new_session": True}


# Hard cap on concurrently tracked background processes. The agent loop can
# spawn in a tight loop (intentional or not); without a ceiling a misbehaving
# plan can fork-bomb the host through the terminal tool. 32 is plenty for
# realistic parallel workloads (servers + watchers + builds) while still
# being defensible RAM/CPU-wise.
MAX_BACKGROUND_PROCESSES = 32

# Output ring-buffer cap. Used as the deque's ``maxlen`` so eviction is O(1)
# and atomic — the previous list-slice approach reallocated and was racy
# against the reader task.
OUTPUT_LINES_MAX = 5000


class ProcessLimitExceeded(RuntimeError):
    """Raised when ProcessManager.spawn would exceed MAX_BACKGROUND_PROCESSES."""


@dataclass
class BackgroundProcess:
    """A tracked background process."""
    pid: int
    proc: asyncio.subprocess.Process
    command: str
    started_at: float = field(default_factory=time.time)
    # Bounded ring buffer: O(1) append + automatic eviction of the oldest
    # line once we hit ``OUTPUT_LINES_MAX``. Replaces the old
    # ``list + slice-and-reassign`` scheme which reallocated on every
    # overflow and could lose lines under contention with the reader.
    output_lines: deque[str] = field(
        default_factory=lambda: deque(maxlen=OUTPUT_LINES_MAX)
    )
    _reader_task: asyncio.Task | None = None
    finished: bool = False
    exit_code: int | None = None
    # Wall-clock when the reader saw EOF / cleanup ran. ``cleanup_finished_older_than``
    # uses this to drop *recently-finished* processes by AGE OF DEATH, not
    # by wall-clock uptime. Without it, a 6-minute-running task that
    # finished one second ago was being evicted instantly while a
    # 2-second flash failure stayed forever — the housekeeping was
    # comparing the wrong axis.
    finished_at: float | None = None

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
        self._processes: dict[int | str, BackgroundProcess] = {}
        # F28: serialise spawn() so two concurrent callers can't both
        # pass the live-count check and over-spawn past the cap.
        # Realistic when an agent dispatches several terminal-tool
        # calls in parallel; without this lock the cleanup → cap-check
        # → create_subprocess_shell → register window was wide open.
        self._spawn_lock = asyncio.Lock()

    @classmethod
    def get(cls) -> "ProcessManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _live_count(self) -> int:
        """Count tracked processes that have not yet finished."""
        return sum(1 for bp in self._processes.values() if not bp.finished)

    async def spawn(self, command: str, workdir: str | None = None) -> BackgroundProcess:
        """Start a background process and track it.

        Raises ``ProcessLimitExceeded`` once the live process count reaches
        ``MAX_BACKGROUND_PROCESSES`` — this is the fork-bomb guardrail.
        Finished-but-still-tracked processes (kept around so the agent can
        read their final output) do not count against the cap.
        """
        # F28: lock-protected critical section covers cleanup, cap check,
        # subprocess creation AND insertion into ``_processes`` so two
        # concurrent spawns can't both observe ``_live_count < MAX`` and
        # both create a subprocess past the cap.
        async with self._spawn_lock:
            # Opportunistic housekeeping: drop long-dead processes so the cap
            # only counts what's actually live.
            if len(self._processes) >= MAX_BACKGROUND_PROCESSES:
                self.cleanup_finished_older_than(seconds=60)
            if self._live_count() >= MAX_BACKGROUND_PROCESSES:
                raise ProcessLimitExceeded(
                    f"background process cap reached "
                    f"({MAX_BACKGROUND_PROCESSES} live). "
                    f"Kill or wait for existing processes before spawning more."
                )

            cwd = workdir or os.getcwd()
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.PIPE,
                cwd=cwd,
                **_SUBPROC_DETACH_KWARGS,
            )

            bp = BackgroundProcess(pid=proc.pid, proc=proc, command=command)
            # F22: PID-reuse collision. The OS can recycle a PID once
            # the previous holder is reaped. If we still have a finished
            # entry for the same number, archive it under a synthetic
            # key so the agent can still read its final output instead
            # of having it silently overwritten by the new process.
            existing = self._processes.get(proc.pid)
            if existing is not None and existing.finished:
                archive_key = f"{proc.pid}-old-{existing.started_at}"
                self._processes[archive_key] = existing
                self._processes.pop(proc.pid, None)
            self._processes[proc.pid] = bp

        # Start background reader OUTSIDE the lock — this only schedules
        # a task, but no need to keep the lock for it.
        bp._reader_task = asyncio.create_task(self._read_output(bp))
        return bp

    async def _read_output(self, bp: BackgroundProcess) -> None:
        """Continuously read output from the process."""
        try:
            while True:
                line = await bp.proc.stdout.readline()
                if not line:
                    break
                # deque(maxlen=...) handles the cap atomically — no
                # slice-and-reassign, no lost lines under contention.
                bp.output_lines.append(line.decode(errors="replace").rstrip("\n"))
        except asyncio.CancelledError:
            # Cancellation MUST propagate — otherwise this task ends as
            # "completed" instead of "cancelled" and ``asyncio.gather(...,
            # return_exceptions=True)`` upstream silently misses the
            # signal. The ``finally`` below still runs and marks the
            # process as finished, so cleanup is correct either way.
            raise
        except Exception:
            # Reader-task crashes are non-fatal (subprocess can still be
            # alive), but we want the trace surfaced — the previous
            # silent ``pass`` made it impossible to debug stuck readers.
            log.exception("background process reader (pid=%s) crashed", bp.pid)
        finally:
            bp.finished = True
            bp.exit_code = bp.proc.returncode
            # Stamp the death-clock so cleanup_finished_older_than can
            # compare against AGE OF DEATH instead of total uptime.
            bp.finished_at = time.time()

    def list_processes(self) -> list[BackgroundProcess]:
        """List all tracked processes."""
        return list(self._processes.values())

    def get_process(self, pid: int) -> BackgroundProcess | None:
        """Get a process by PID."""
        return self._processes.get(pid)

    async def kill(self, pid: int) -> str:
        """Kill a background process and its entire process group.

        ``spawn`` puts the child in a new session / process group (POSIX) or
        a new process group (Windows). We have to signal the *group*, not
        just the leader — otherwise an LLM running e.g.
        ``bash -c 'sleep 999 & sleep 999 & wait'`` leaves the inner sleeps
        orphaned and burning resources after the leader exits.
        """
        bp = self._processes.get(pid)
        if not bp:
            return f"ERROR: no process with PID {pid}"
        if bp.finished:
            return f"Process {pid} already finished (exit {bp.exit_code})"
        try:
            _terminate_group(bp.proc)
            # Give the group ~0.5s to exit gracefully, then escalate.
            for _ in range(5):
                await asyncio.sleep(0.1)
                if bp.proc.returncode is not None:
                    break
            if bp.proc.returncode is None:
                _kill_group(bp.proc)
                # Final settle so the reader task sees EOF.
                await asyncio.sleep(0.1)
            bp.finished = True
            bp.exit_code = bp.proc.returncode
            return f"OK: killed process {pid}"
        except ProcessLookupError:
            # Race: process exited between our check and the signal.
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
        if bp.proc.stdin is None or bp.proc.stdin.is_closing():
            return f"ERROR: process {pid} stdin is closed"
        try:
            bp.proc.stdin.write((data + "\n").encode())
            await bp.proc.stdin.drain()
            return f"OK: sent {len(data)} chars to PID {pid}"
        except Exception as e:
            return f"ERROR: {e}"

    async def close_stdin(self, pid: int) -> str:
        """Close stdin (send EOF) so the process can exit normally."""
        bp = self._processes.get(pid)
        if not bp:
            return f"ERROR: no process with PID {pid}"
        if bp.finished:
            return f"ERROR: process {pid} already finished"
        if bp.proc.stdin is None or bp.proc.stdin.is_closing():
            return f"ERROR: process {pid} stdin already closed"
        try:
            bp.proc.stdin.close()
            return f"OK: closed stdin for PID {pid}"
        except Exception as e:
            return f"ERROR: {e}"

    def read_output(self, pid: int, last_n: int = 50) -> str:
        """Read last N lines of output from a process."""
        bp = self._processes.get(pid)
        if not bp:
            return f"ERROR: no process with PID {pid}"
        # deque doesn't support [-N:] slicing; materialise then tail.
        all_lines = list(bp.output_lines)
        lines = all_lines[-last_n:]
        if not lines:
            return "(no output yet)"
        header = f"[PID {pid} | {bp.status} | {bp.uptime:.0f}s uptime | {len(all_lines)} total lines]"
        return header + "\n" + "\n".join(lines)

    def cleanup_finished(self) -> int:
        """Remove finished processes from tracking. Returns count removed."""
        to_remove = [pid for pid, bp in self._processes.items() if bp.finished]
        for pid in to_remove:
            if self._processes[pid]._reader_task:
                self._processes[pid]._reader_task.cancel()
            del self._processes[pid]
        return len(to_remove)

    def cleanup_finished_older_than(self, seconds: float = 300) -> int:
        """Drop finished processes whose exit was more than `seconds` ago.

        Compares against ``finished_at`` (wall-clock at EOF) rather than
        ``uptime`` (wall-clock since start) — the housekeeping question
        is "how long has it been gone?", not "how long did it run?".
        Live processes and processes whose reader hasn't stamped a
        finish time yet are kept; the agent can still read their final
        output.
        """
        now = time.time()
        to_remove = []
        for pid, bp in self._processes.items():
            if not bp.finished:
                continue
            if bp.finished_at is None:
                # Reader didn't stamp a death-time (legacy path / mid-
                # upgrade). Don't evict — wait for the next tick when the
                # reader's ``finally`` will populate it.
                continue
            if now - bp.finished_at > seconds:
                to_remove.append(pid)
        for pid in to_remove:
            if self._processes[pid]._reader_task:
                self._processes[pid]._reader_task.cancel()
            del self._processes[pid]
        return len(to_remove)


# ── Process-group signalling helpers ─────────────────────────────────────
#
# Why not just ``proc.terminate()``? On POSIX, ``terminate`` sends SIGTERM
# to the leader's PID only. Anything the leader forked into the same
# process group survives. We spawn with ``start_new_session=True`` so the
# group ID equals the leader's PID, and ``killpg`` reaches every member.
#
# On Windows there's no concept of a process group signal, but
# ``CREATE_NEW_PROCESS_GROUP`` plus ``CTRL_BREAK_EVENT`` is the
# documented equivalent for graceful shutdown; ``taskkill /T /F`` is the
# hard-kill fallback that also walks the descendant tree.

def _terminate_group(proc: asyncio.subprocess.Process) -> None:
    """Send a graceful termination signal to the entire process group."""
    if sys.platform == "win32":
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        except (ValueError, OSError):
            # Group might already be gone / not a console process — fall
            # back to terminate so we still try the leader.
            try:
                proc.terminate()
            except (OSError, ProcessLookupError):
                # Process truly gone; nothing to do.
                log.debug(
                    "_terminate_group(win32): proc.terminate() failed for pid=%s",
                    proc.pid, exc_info=True,
                )
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """Force-kill the entire process group (SIGKILL / taskkill /T /F)."""
    if sys.platform == "win32":
        # taskkill walks the process tree (/T) and forces (/F). Best-effort.
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except (OSError, FileNotFoundError):
            # taskkill missing (rare on modern Windows) or fork failed —
            # fall back to the asyncio.Process kill, which sends a single
            # TerminateProcess and skips the descendant tree.
            log.debug("taskkill /T /F failed for pid=%s; falling back to proc.kill()",
                      proc.pid, exc_info=True)
            try:
                proc.kill()
            except (OSError, ProcessLookupError):
                # Already dead — nothing more we can do.
                log.debug("proc.kill() also failed for pid=%s", proc.pid, exc_info=True)
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
