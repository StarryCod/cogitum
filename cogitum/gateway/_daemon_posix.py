"""
cogitum.gateway._daemon_posix
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
POSIX backend for the Telegram gateway daemon — uses ``systemctl --user``.

Public surface (mirrors ``_daemon_windows``):
    install_service()    → str
    enable_service()     → str
    disable_service()    → str
    start_service()      → str
    stop_service()       → str
    restart_service()    → str
    status_service()     → dict
    uninstall_service()  → str

The wrapping ``daemon.py`` façade dispatches by ``sys.platform``.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# All systemctl invocations are bounded by this hard timeout. Without it,
# a hung systemd-user (rare but real on headless boxes) would freeze every
# `cog tg ...` subcommand indefinitely (M2).
_SYSTEMCTL_TIMEOUT = 15

_SERVICE_NAME = "cogitum-tg"
_SERVICE_DIR = Path.home() / ".config" / "systemd" / "user"
_SERVICE_PATH = _SERVICE_DIR / f"{_SERVICE_NAME}.service"


def _systemctl(*args: str, capture: bool = True) -> subprocess.CompletedProcess:
    """Run `systemctl --user <args>` with a bounded timeout."""
    try:
        return subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=capture,
            text=True,
            timeout=_SYSTEMCTL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=["systemctl", "--user", *args],
            returncode=124,
            stdout="",
            stderr=f"systemctl --user {' '.join(args)} timed out after {_SYSTEMCTL_TIMEOUT}s",
        )


def _python_path() -> str:
    """Resolve the Python interpreter that has cogitum installed.

    Strategy:
      1. Honour explicit ``COGITUM_PYTHON`` env var.
      2. Fall back to ``sys.executable`` (whatever invoked ``cog``).
    """
    explicit = os.environ.get("COGITUM_PYTHON")
    if explicit and Path(explicit).exists():
        return explicit
    return sys.executable


def _service_content() -> str:
    python = _python_path()
    return f"""\
[Unit]
Description=Cogitum Telegram Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={python} -m cogitum.gateway.telegram
Restart=on-failure
RestartSec=10
# Cap the time systemd waits between SIGTERM and SIGKILL. The bot's
# own stop() handler cancels the long-poll task and closes httpx in
# under a second, so 10s is generous. Default (90s) was the reason
# `cog tg stop` looked like it took "minutes" in the TUI even after
# the gateway already finished its graceful shutdown.
TimeoutStopSec=10
KillSignal=SIGTERM
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""


def install_service() -> str:
    _SERVICE_DIR.mkdir(parents=True, exist_ok=True)
    _SERVICE_PATH.write_text(_service_content(), encoding="utf-8")
    _systemctl("daemon-reload")
    return f"Service installed: {_SERVICE_PATH}"


def enable_service() -> str:
    install_service()
    result = _systemctl("enable", _SERVICE_NAME)
    if result.returncode != 0:
        return f"Enable failed: {result.stderr.strip()}"
    return f"Enabled: {_SERVICE_NAME} (auto-start on login)"


def start_service() -> str:
    install_service()
    result = _systemctl("start", _SERVICE_NAME)
    if result.returncode != 0:
        return f"Start failed: {result.stderr.strip()}"
    return "Started ✓"


def stop_service() -> str:
    result = _systemctl("stop", _SERVICE_NAME)
    if result.returncode != 0:
        return f"Stop failed: {result.stderr.strip()}"
    return "Stopped ✓"


def restart_service() -> str:
    """Restart via ``systemctl restart`` (atomic state transition)."""
    install_service()
    result = _systemctl("restart", _SERVICE_NAME)
    if result.returncode != 0:
        return f"Restart failed: {result.stderr.strip()}"
    return "Restarted ✓"


def status_service() -> dict[str, str]:
    result = _systemctl("status", _SERVICE_NAME)
    output = result.stdout.strip()

    active = "unknown"
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("Active:"):
            active = line.split(":", 1)[1].strip()
            break

    is_enabled = _systemctl("is-enabled", _SERVICE_NAME).stdout.strip()

    return {
        "active": active,
        "enabled": is_enabled,
        "service_path": str(_SERVICE_PATH),
        "full_output": output,
        "backend": "systemd",
    }


def disable_service() -> str:
    result = _systemctl("disable", _SERVICE_NAME)
    if result.returncode != 0:
        return f"Disable failed: {result.stderr.strip()}"
    return "Disabled (won't auto-start)"


def uninstall_service() -> str:
    stop_service()
    disable_service()
    if _SERVICE_PATH.exists():
        _SERVICE_PATH.unlink()
    _systemctl("daemon-reload")
    return "Service removed"
