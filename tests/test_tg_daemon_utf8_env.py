"""F7: cog tg start (Windows daemon child) must inherit UTF-8 env.

The daemon's child process is launched via subprocess.Popen, bypassing
cogitum.cli._windows_init() which sets the console codepage. Without
seeding PYTHONIOENCODING/PYTHONUTF8 in the child env, the bot's logging
StreamHandler chokes on every emoji/box-drawing char on a cp1251
Russian Windows console and silently drops log lines.

This test mocks subprocess.Popen and asserts the env dict passed to
the child contains both UTF-8 keys. We can run on Linux because
start_service has a POSIX fallback path through _is_pid_alive.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock


def test_start_service_seeds_utf8_env(tmp_path, monkeypatch):
    """Popen kwargs MUST include env with PYTHONIOENCODING=utf-8 +
    PYTHONUTF8=1 so the child's logging.StreamHandler can render
    emoji/glyphs on a Russian Windows console."""
    from cogitum.gateway import _daemon_windows as dwin

    # Redirect %APPDATA%/state to tmp so we don't write to the user's
    # real config dir under tests.
    monkeypatch.setenv("APPDATA", str(tmp_path))
    # Make sure no stale pidfile fools _is_pid_alive.
    pid_path, _ = dwin._state_paths()
    if pid_path.exists():
        pid_path.unlink()

    fake_proc = MagicMock()
    fake_proc.pid = 12345
    fake_proc.poll = MagicMock(return_value=None)  # still alive

    with patch.object(dwin.subprocess, "Popen", return_value=fake_proc) as m_popen, \
         patch.object(dwin.time, "sleep"):
        dwin.start_service()

    assert m_popen.called, "subprocess.Popen must be invoked"
    kwargs = m_popen.call_args.kwargs
    env = kwargs.get("env")
    assert env is not None, f"start_service must pass env=... to Popen, kwargs={kwargs!r}"
    assert env.get("PYTHONIOENCODING") == "utf-8", (
        f"missing PYTHONIOENCODING=utf-8 in child env, got {env.get('PYTHONIOENCODING')!r}"
    )
    assert env.get("PYTHONUTF8") == "1", (
        f"missing PYTHONUTF8=1 in child env, got {env.get('PYTHONUTF8')!r}"
    )


def test_telegram_main_calls_windows_init():
    """F7 part b: direct `python -m cogitum.gateway.telegram` invocation
    must run cli._windows_init() so foreground `cog tg run` (the
    operator's debug path) has the same UTF-8 console as the daemon.
    """
    import importlib
    import sys

    # Drop any stale modules so the patch lands on the live import.
    for mod in list(sys.modules):
        if mod.startswith("cogitum"):
            sys.modules.pop(mod, None)

    from cogitum.gateway import telegram as tg_module

    called = {"n": 0}

    def fake_init():
        called["n"] += 1

    # Patch _windows_init at its source so the import inside main()
    # picks up the fake.
    from cogitum import cli as cli_mod
    orig = cli_mod._windows_init
    cli_mod._windows_init = fake_init  # type: ignore
    try:
        # Stub out asyncio.run so we don't actually start a bot.
        # Also stub run_bot itself so the coroutine factory never
        # produces a real coroutine (avoids the un-awaited warning).
        with patch.object(tg_module.asyncio, "run", return_value=None), \
             patch.object(tg_module, "run_bot", lambda *a, **kw: None), \
             patch.object(tg_module.logging, "basicConfig"):
            try:
                tg_module.main()
            except SystemExit:
                pass
    finally:
        cli_mod._windows_init = orig  # type: ignore

    assert called["n"] >= 1, "telegram.main() must invoke _windows_init()"
