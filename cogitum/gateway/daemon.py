"""
cogitum.gateway.daemon
~~~~~~~~~~~~~~~~~~~~~~~
Systemd user service management for the Telegram gateway.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SERVICE_NAME = "cogitum-tg"
_SERVICE_DIR = Path.home() / ".config" / "systemd" / "user"
_SERVICE_PATH = _SERVICE_DIR / f"{_SERVICE_NAME}.service"


def _python_path() -> str:
    """Get the Python interpreter that has cogitum installed."""
    # Prefer the project venv if it exists
    venv = Path.home() / "Cogitum" / ".venv" / "bin" / "python"
    if venv.exists():
        return str(venv)
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
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""


def install_service() -> str:
    """Install the systemd user service file."""
    _SERVICE_DIR.mkdir(parents=True, exist_ok=True)
    _SERVICE_PATH.write_text(_service_content(), encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return f"Service installed: {_SERVICE_PATH}"


def enable_service() -> str:
    """Enable auto-start on login."""
    install_service()
    result = subprocess.run(
        ["systemctl", "--user", "enable", _SERVICE_NAME],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return f"Enable failed: {result.stderr.strip()}"
    return f"Enabled: {_SERVICE_NAME} (auto-start on login)"


def start_service() -> str:
    """Start the gateway daemon."""
    install_service()
    result = subprocess.run(
        ["systemctl", "--user", "start", _SERVICE_NAME],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return f"Start failed: {result.stderr.strip()}"
    return "Started ✓"


def stop_service() -> str:
    """Stop the gateway daemon."""
    result = subprocess.run(
        ["systemctl", "--user", "stop", _SERVICE_NAME],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return f"Stop failed: {result.stderr.strip()}"
    return "Stopped ✓"


def restart_service() -> str:
    """Restart the gateway daemon."""
    install_service()
    result = subprocess.run(
        ["systemctl", "--user", "restart", _SERVICE_NAME],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return f"Restart failed: {result.stderr.strip()}"
    return "Restarted ✓"


def status_service() -> dict[str, str]:
    """Get daemon status."""
    result = subprocess.run(
        ["systemctl", "--user", "status", _SERVICE_NAME],
        capture_output=True, text=True,
    )
    output = result.stdout.strip()

    # Parse status
    active = "unknown"
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("Active:"):
            active = line.split(":", 1)[1].strip()
            break

    is_enabled = subprocess.run(
        ["systemctl", "--user", "is-enabled", _SERVICE_NAME],
        capture_output=True, text=True,
    ).stdout.strip()

    return {
        "active": active,
        "enabled": is_enabled,
        "service_path": str(_SERVICE_PATH),
        "full_output": output,
    }


def disable_service() -> str:
    """Disable auto-start."""
    result = subprocess.run(
        ["systemctl", "--user", "disable", _SERVICE_NAME],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return f"Disable failed: {result.stderr.strip()}"
    return "Disabled (won't auto-start)"


def uninstall_service() -> str:
    """Stop, disable, and remove the service file."""
    stop_service()
    disable_service()
    if _SERVICE_PATH.exists():
        _SERVICE_PATH.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return "Service removed"
