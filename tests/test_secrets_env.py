"""Tests for the persistent secrets.env store."""
from __future__ import annotations

import os


def test_save_and_load_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("COGITUM_CONFIG_DIR", str(tmp_path))
    # Reload module so module-level paths refresh
    import importlib
    import cogitum.core.llm.secrets_env as se
    importlib.reload(se)

    se.save_secret("MY_KEY", "secret-value-123")
    # File exists with mode 0600
    p = tmp_path / "cogitum" / "secrets.env"
    assert p.exists()
    assert (p.stat().st_mode & 0o777) == 0o600

    # In-process os.environ updated
    assert os.environ.get("MY_KEY") == "secret-value-123"

    # Wipe env and reload from disk
    del os.environ["MY_KEY"]
    n = se.load_secrets_into_environ()
    assert n >= 1
    assert os.environ.get("MY_KEY") == "secret-value-123"


def test_save_secret_replaces_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("COGITUM_CONFIG_DIR", str(tmp_path))
    import importlib, cogitum.core.llm.secrets_env as se
    importlib.reload(se)

    se.save_secret("KEY1", "first")
    se.save_secret("KEY1", "second")
    se.save_secret("KEY2", "other")

    # Only one KEY1 line remains
    body = (tmp_path / "cogitum" / "secrets.env").read_text()
    lines_with_key1 = [l for l in body.splitlines()
                       if l.split("=")[0] == "KEY1"]
    assert len(lines_with_key1) == 1
    assert "second" in lines_with_key1[0]
    assert os.environ["KEY1"] == "second"
    assert os.environ["KEY2"] == "other"


def test_save_secret_quotes_values_with_specials(tmp_path, monkeypatch):
    monkeypatch.setenv("COGITUM_CONFIG_DIR", str(tmp_path))
    import importlib, cogitum.core.llm.secrets_env as se
    importlib.reload(se)

    se.save_secret("WITH_SPACE", "hello world")
    se.save_secret("WITH_DOLLAR", "abc$def")
    se.save_secret("WITH_QUOTE", "it's mine")

    # Re-read from disk: values must round-trip
    del os.environ["WITH_SPACE"]
    del os.environ["WITH_DOLLAR"]
    del os.environ["WITH_QUOTE"]
    se.load_secrets_into_environ()
    assert os.environ["WITH_SPACE"] == "hello world"
    assert os.environ["WITH_DOLLAR"] == "abc$def"
    assert os.environ["WITH_QUOTE"] == "it's mine"


def test_load_does_not_override_existing_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COGITUM_CONFIG_DIR", str(tmp_path))
    import importlib, cogitum.core.llm.secrets_env as se
    importlib.reload(se)

    # Persist a secret to disk
    se.save_secret("OVERRIDE_TEST", "from-file")
    # User explicitly sets it in real env
    os.environ["OVERRIDE_TEST"] = "from-shell"

    # Default load should keep shell value
    se.load_secrets_into_environ()
    assert os.environ["OVERRIDE_TEST"] == "from-shell"

    # override=True overwrites
    se.load_secrets_into_environ(override=True)
    assert os.environ["OVERRIDE_TEST"] == "from-file"


def test_remove_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("COGITUM_CONFIG_DIR", str(tmp_path))
    import importlib, cogitum.core.llm.secrets_env as se
    importlib.reload(se)

    se.save_secret("DELETE_ME", "v1")
    assert "DELETE_ME" in os.environ
    removed = se.remove_secret("DELETE_ME")
    assert removed is True
    assert "DELETE_ME" not in os.environ

    # Subsequent remove returns False
    assert se.remove_secret("DELETE_ME") is False


def test_list_secrets_masks_values(tmp_path, monkeypatch):
    monkeypatch.setenv("COGITUM_CONFIG_DIR", str(tmp_path))
    import importlib, cogitum.core.llm.secrets_env as se
    importlib.reload(se)

    se.save_secret("LONG_KEY", "abcdefghijklmnopqrstuvwxyz")
    se.save_secret("SHORT_KEY", "short")

    listing = se.list_secrets()
    assert "LONG_KEY" in listing
    assert "abcdefghijklmnopqrstuvwxyz" not in listing["LONG_KEY"]
    assert listing["LONG_KEY"].startswith("abcd")
    assert listing["LONG_KEY"].endswith("wxyz")
    assert listing["SHORT_KEY"] == "***"


def test_load_handles_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("COGITUM_CONFIG_DIR", str(tmp_path))
    import importlib, cogitum.core.llm.secrets_env as se
    importlib.reload(se)

    # No file exists yet — load returns 0, doesn't crash
    n = se.load_secrets_into_environ()
    assert n == 0


def test_wizard_save_persists_to_disk(tmp_path, monkeypatch):
    """Integration: KeyEntryModal env-backend save → secrets.env populated."""
    monkeypatch.setenv("COGITUM_CONFIG_DIR", str(tmp_path))
    import sys
    for mod in list(sys.modules):
        if mod.startswith("cogitum"):
            sys.modules.pop(mod, None)

    from textual.app import App
    from cogitum.setup_flow import KeyEntryModal
    from textual.widgets import Input

    captured = {}

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(KeyEntryModal("p1", "P1", "WIZARD_TEST_KEY"),
                             self._done)

        def _done(self, r) -> None:
            captured["r"] = r
            self.exit()

    import asyncio

    async def go():
        async with _Host().run_test(headless=True, size=(140, 50)) as pilot:
            await pilot.pause()
            from cogitum.setup_flow import KeyEntryModal
            modal = next(s for s in pilot.app.screen_stack
                         if isinstance(s, KeyEntryModal))
            modal.query_one("#key-secret", Input).value = "wizard-secret-xyz"
            await pilot.pause()
            modal.action_save()
            await pilot.pause()

    asyncio.run(go())

    assert captured["r"] is not None
    assert captured["r"].secret_ref == "env:WIZARD_TEST_KEY"
    # Secret was persisted
    assert os.environ.get("WIZARD_TEST_KEY") == "wizard-secret-xyz"
    # And to disk
    p = tmp_path / "cogitum" / "secrets.env"
    assert p.exists()
    assert "WIZARD_TEST_KEY" in p.read_text()
