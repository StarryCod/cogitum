"""
cogitum.gateway.daemon
~~~~~~~~~~~~~~~~~~~~~~
Cross-platform façade for the Telegram gateway daemon.

Dispatches to ``_daemon_posix`` (systemctl --user) on Linux/macOS and
``_daemon_windows`` (detached process + HKCU\\...\\Run) on Windows. The
public surface is identical, so callers (CLI, TUI setup wizard) don't
need to special-case the platform.

Backwards-compat note: ``NotSupportedOnPlatform`` is preserved as a
public symbol so older callers that import it keep working, but it is
no longer raised in normal use — both platforms are supported now. The
exception remains for environments where neither backend can run (e.g.
a sandboxed test runner without ``%APPDATA%`` or ``systemctl``).
"""
from __future__ import annotations

import sys


class NotSupportedOnPlatform(RuntimeError):
    """Raised on platforms where no daemon backend can run.

    Kept for backwards compatibility — ``setup_flow.py`` catches this
    when rendering the Telegram section. With the Windows backend in
    place, normal Windows / Linux / macOS users will never see it.
    """


def _backend():  # type: ignore[no-untyped-def]
    if sys.platform == "win32":
        from . import _daemon_windows as backend
    else:
        from . import _daemon_posix as backend
    return backend


# ---------------------------------------------------------------------------
# Public API — every function delegates to the platform backend.
# ---------------------------------------------------------------------------


def install_service() -> str:
    return _backend().install_service()


def enable_service() -> str:
    return _backend().enable_service()


def disable_service() -> str:
    return _backend().disable_service()


def start_service() -> str:
    return _backend().start_service()


def stop_service() -> str:
    return _backend().stop_service()


def restart_service() -> str:
    return _backend().restart_service()


def status_service() -> dict[str, str]:
    return _backend().status_service()


def uninstall_service() -> str:
    return _backend().uninstall_service()


__all__ = [
    "NotSupportedOnPlatform",
    "install_service",
    "enable_service",
    "disable_service",
    "start_service",
    "stop_service",
    "restart_service",
    "status_service",
    "uninstall_service",
]
