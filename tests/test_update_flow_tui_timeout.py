"""F20: TUI _run_upgrade must time out after 120s and clean up."""
from __future__ import annotations

import asyncio
import inspect

import pytest


def test_run_upgrade_uses_wait_for():
    """The TUI upgrade body must wrap the pull in asyncio.wait_for(timeout=120)."""
    from cogitum import update_flow

    src = inspect.getsource(update_flow._UpdateApp._run_upgrade)
    assert "wait_for" in src, "_run_upgrade must use asyncio.wait_for for timeout"
    assert "timeout=120" in src, "_run_upgrade must enforce a 120s timeout"


def test_run_upgrade_handles_timeouterror():
    """On TimeoutError the dialog shows 'Upgrade timed out'."""
    from cogitum import update_flow

    src = inspect.getsource(update_flow._UpdateApp._run_upgrade)
    assert "TimeoutError" in src, "must handle asyncio.TimeoutError"
    assert "Upgrade timed out" in src or "timed out" in src.lower()


def test_run_upgrade_kills_group_on_cancel():
    """The pull coroutine must kill the subprocess group when cancelled."""
    from cogitum import update_flow

    src = inspect.getsource(update_flow._UpdateApp._run_upgrade)
    # Either killpg (POSIX) or proc.kill — at minimum we must not just
    # let the subprocess linger.
    assert ("killpg" in src) or ("proc.kill" in src), (
        "must terminate git on cancel"
    )


def test_cli_path_still_has_120s_timeout():
    """CLI variant kept its existing 120s timeout."""
    from cogitum import update_flow

    src = inspect.getsource(update_flow._run_headless)
    assert "timeout=120" in src
