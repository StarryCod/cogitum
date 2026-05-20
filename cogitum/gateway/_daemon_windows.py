"""
cogitum.gateway._daemon_windows
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Windows backend for the Telegram gateway daemon.

Strategy: a detached background ``python -m cogitum.gateway.telegram``
process plus a PID file at ``%APPDATA%\\Cogitum\\cogitum-tg.pid`` and a
log file at ``%APPDATA%\\Cogitum\\cogitum-tg.log``. Auto-start uses the
HKCU ``Run`` registry key (per-user, no admin, no third-party deps —
NSSM not required). On reboot Windows auto-starts the gateway when the
user logs in, the same UX as ``systemctl --user enable`` on Linux.

Why not Task Scheduler / NSSM:
  • Task Scheduler needs ``schtasks`` invocations + an XML manifest, far
    more failure surface than HKCU\\Run.
  • NSSM is a separate executable the user would have to download.
  • HKCU\\Run is what every well-behaved Windows TUI tool uses for the
    "start at login" toggle (e.g. Discord, Slack, Steam in user mode).

The daemon process is launched with ``CREATE_NO_WINDOW`` |
``DETACHED_PROCESS`` | ``CREATE_NEW_PROCESS_GROUP`` so it survives the
terminal closing and doesn't flash a console window.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

_SERVICE_NAME = "cogitum-tg"
_RUN_KEY_NAME = "CogitumTG"  # value name under HKCU\\...\\Run


def _appdata_dir() -> Path:
    """``%APPDATA%\\Cogitum`` (or ``~/.config/cogitum`` if APPDATA missing).

    ``%APPDATA%`` is the canonical roaming-config location on Windows;
    falling back to the POSIX path means the same dir layout works
    when running this module under WSL or Cygwin for a smoke test.
    """
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Cogitum"
    return Path.home() / ".config" / "cogitum"


def _state_paths() -> tuple[Path, Path]:
    base = _appdata_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{_SERVICE_NAME}.pid", base / f"{_SERVICE_NAME}.log"


def _python_path() -> str:
    """Same contract as POSIX backend — honour ``COGITUM_PYTHON``, else
    fall back to ``sys.executable``."""
    explicit = os.environ.get("COGITUM_PYTHON")
    if explicit and Path(explicit).exists():
        return explicit
    return sys.executable


def _is_pid_alive(pid: int) -> bool:
    """Return True iff ``pid`` is a live, non-zombie process.

    Uses Windows ``OpenProcess`` via ctypes — no extra deps. Other
    backends (psutil) work but pull a transitive C extension.
    """
    if pid <= 0:
        return False
    if sys.platform != "win32":
        # POSIX fallback when this module is imported under WSL for tests.
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return pid > 0  # PermissionError → process exists, just not ours
        except OSError:
            return False
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            exit_code = wintypes.DWORD()
            ok = kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
            if not ok:
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        # Safer to claim "alive" on inspection failure — caller will
        # surface the error rather than starting a duplicate poller
        # (TG API rejects a second getUpdates with HTTP 409).
        return True


def _read_pid() -> int | None:
    pid_path, _ = _state_paths()
    if not pid_path.exists():
        return None
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None
    if pid <= 0:
        return None
    return pid


def _write_pid(pid: int) -> None:
    pid_path, _ = _state_paths()
    pid_path.write_text(str(pid), encoding="utf-8")


def _clear_pid() -> None:
    pid_path, _ = _state_paths()
    try:
        pid_path.unlink()
    except FileNotFoundError:
        pass


def _command_args() -> list[str]:
    return [_python_path(), "-m", "cogitum.gateway.telegram"]


def _command_string() -> str:
    """Quoted command line for the registry / status output."""
    parts = _command_args()
    if sys.platform == "win32":
        return subprocess.list2cmdline(parts)
    return " ".join(shlex.quote(p) for p in parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def install_service() -> str:
    """No-op on Windows — the 'service' is a detached process, no
    pre-installation step is needed. Kept for parity with POSIX so
    callers can invoke ``install_service()`` unconditionally."""
    base = _appdata_dir()
    base.mkdir(parents=True, exist_ok=True)
    return f"State dir ready: {base}"


def start_service() -> str:
    install_service()
    existing = _read_pid()
    if existing and _is_pid_alive(existing):
        return f"Already running (pid={existing})"

    _, log_path = _state_paths()

    # Detach so the daemon survives the launching shell and doesn't
    # bind to its console (no flicker, no Ctrl+C propagation).
    creationflags = 0
    if sys.platform == "win32":
        creationflags = (
            subprocess.CREATE_NO_WINDOW          # no console window  # type: ignore[attr-defined]
            | subprocess.DETACHED_PROCESS        # full detach        # type: ignore[attr-defined]
            | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        )

    log_fp = open(log_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            _command_args(),
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            close_fds=True,
            creationflags=creationflags,
            cwd=str(_appdata_dir()),
        )
    finally:
        # Popen dup'd the handle — close ours so log rotation works.
        log_fp.close()

    # Brief sanity check — if the process exits in <0.5s it's a config
    # error, surface that immediately instead of saying "Started ✓"
    # and leaving the user wondering why nothing happens.
    time.sleep(0.5)
    if proc.poll() is not None:
        rc = proc.returncode
        tail = ""
        try:
            data = log_path.read_bytes()[-400:].decode("utf-8", "replace").strip()
            if data:
                tail = f" — log tail: {data[-200:]}"
        except OSError:
            pass
        return f"Start failed: process exited rc={rc}{tail}"

    _write_pid(proc.pid)
    return f"Started ✓ (pid={proc.pid}, log={log_path})"


def stop_service() -> str:
    pid = _read_pid()
    if not pid:
        return "Not running"
    if not _is_pid_alive(pid):
        _clear_pid()
        return "Not running (stale pidfile cleaned)"

    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            PROCESS_TERMINATE = 0x0001
            h = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if not h:
                _clear_pid()
                return f"Stop failed: cannot open pid={pid}"
            try:
                kernel32.TerminateProcess(h, 0)
            finally:
                kernel32.CloseHandle(h)
        except Exception as e:
            return f"Stop failed: {e}"
    else:
        try:
            os.kill(pid, 15)  # SIGTERM
        except ProcessLookupError:
            pass

    # Wait briefly for graceful exit.
    for _ in range(30):
        if not _is_pid_alive(pid):
            break
        time.sleep(0.1)

    _clear_pid()
    return "Stopped ✓"


def restart_service() -> str:
    stop_service()
    time.sleep(0.5)
    return start_service()


def status_service() -> dict[str, str]:
    pid = _read_pid()
    pid_path, log_path = _state_paths()
    if pid and _is_pid_alive(pid):
        active = f"running (pid={pid})"
    elif pid:
        active = "stale (pidfile present, process gone)"
    else:
        active = "stopped"

    enabled = "enabled" if _is_autostart_enabled() else "disabled"

    return {
        "active": active,
        "enabled": enabled,
        "service_path": str(pid_path),
        "log_path": str(log_path),
        "backend": "windows-detached",
    }


# ---------------------------------------------------------------------------
# Auto-start on login — HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
# ---------------------------------------------------------------------------


_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _open_run_key(write: bool):  # type: ignore[no-untyped-def]
    if sys.platform != "win32":
        raise RuntimeError("HKCU registry only available on Windows")
    import winreg  # type: ignore[import-not-found]

    access = winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE if write else winreg.KEY_QUERY_VALUE
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, access), winreg


def _is_autostart_enabled() -> bool:
    if sys.platform != "win32":
        # On WSL/POSIX-fallback we have no registry; treat as disabled.
        return False
    try:
        key, winreg = _open_run_key(write=False)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    try:
        value, _type = winreg.QueryValueEx(key, _RUN_KEY_NAME)
        return bool(value)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    finally:
        try:
            key.Close()  # type: ignore[union-attr]
        except Exception:
            pass


def enable_service() -> str:
    """Add an HKCU\\...\\Run value so the gateway auto-starts on login."""
    if sys.platform != "win32":
        return "Auto-start not supported in this environment (Windows registry unavailable)"
    install_service()
    try:
        import winreg  # type: ignore[import-not-found]

        cmd = _command_string()
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
        ) as key:
            winreg.SetValueEx(key, _RUN_KEY_NAME, 0, winreg.REG_SZ, cmd)
        return f"Enabled: HKCU\\...\\Run\\{_RUN_KEY_NAME} (auto-start on login)"
    except OSError as e:
        return f"Enable failed: {e}"


def disable_service() -> str:
    if sys.platform != "win32":
        return "Auto-start not supported in this environment"
    try:
        import winreg  # type: ignore[import-not-found]

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE,
        ) as key:
            try:
                winreg.DeleteValue(key, _RUN_KEY_NAME)
            except FileNotFoundError:
                return "Disabled (was not enabled)"
        return "Disabled (won't auto-start)"
    except OSError as e:
        return f"Disable failed: {e}"


def uninstall_service() -> str:
    stop_service()
    disable_service()
    pid_path, log_path = _state_paths()
    for p in (pid_path, log_path):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    return "Service removed"
