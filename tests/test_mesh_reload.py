"""Tests for mesh reload — both TUI (_load_mesh_async) and TG (_reload_mesh).

The wizard mutates providers.toml; both surfaces must pick up changes
without restarting the process.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# TUI: _on_setup_close + action_open_models reload
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tui_setup_close_reloads_mesh(monkeypatch):
    """When SetupScreen closes, _on_setup_close must rebuild self.mesh."""
    from textual.app import App
    from cogitum.app import CogitumApp
    from cogitum.core.llm.config_writer import ConfigWriter

    # Pre-seed providers.toml with one provider
    w = ConfigWriter()
    w.add_provider("preexisting", name="Pre", format="openai_compat",
                   base_url="https://x", auth="bearer", enabled=True)
    w.set_key("preexisting", "primary", "env:NONEXISTENT_FAKE_VAR")
    w.save()

    async with CogitumApp().run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await pilot.pause()
        app = pilot.app

        first_mesh = app.mesh
        # Mutate providers.toml externally (simulating wizard save)
        w2 = ConfigWriter()
        w2.add_provider("added-after", name="Added", format="openai_compat",
                        base_url="https://y", auth="bearer", enabled=False)
        w2.save()

        # Trigger _on_setup_close manually
        app._on_setup_close(None)
        await pilot.pause()
        await pilot.pause()

        # Mesh must be a different object (or providers map differs)
        assert app.mesh is not first_mesh, "_on_setup_close did not rebuild mesh"


@pytest.mark.asyncio
async def test_tui_action_open_models_reloads_mesh():
    """action_open_models must reload mesh before opening picker."""
    from cogitum.app import CogitumApp
    from cogitum.core.llm.config_writer import ConfigWriter

    async with CogitumApp().run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await pilot.pause()
        app = pilot.app

        first_mesh = app.mesh

        # Add a provider externally
        w = ConfigWriter()
        w.add_provider("models-test", name="MT", format="openai_compat",
                       base_url="https://m", auth="bearer", enabled=False)
        w.save()

        # Call the action — should reload before pushing picker
        app.action_open_models()
        await pilot.pause()
        await pilot.pause()

        assert app.mesh is not first_mesh


# ---------------------------------------------------------------------------
# TG gateway: _reload_mesh
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tg_reload_mesh_picks_up_new_providers():
    """Gateway._reload_mesh re-reads providers.toml."""
    import os
    from cogitum.core.llm.config_writer import ConfigWriter
    from cogitum.gateway.telegram import CogitumBot
    from cogitum.gateway.tg_config import TelegramConfig

    # Seed with a valid provider (model + key resolvable)
    os.environ["FAKE_INIT_KEY_NEW"] = "secret-1"
    w = ConfigWriter()
    w.add_provider("init-prov", name="Init", format="openai_compat",
                   base_url="https://i", auth="bearer", enabled=True)
    w.set_key("init-prov", "primary", "env:FAKE_INIT_KEY_NEW")
    w.add_model("init-prov", "init-m", display="Init Model",
                capabilities=["text"], context_window=8192,
                max_output_tokens=2048)
    w.save()

    cfg = TelegramConfig(
        bot_token="123:fake-token",
        allowed_user_id=1,
        enabled=True,
    )
    bot = CogitumBot(cfg)

    from cogitum.core.llm.loader import load_mesh
    bot.mesh = load_mesh()
    initial_mesh = bot.mesh

    class _FakeAPI:
        async def send_message(self, *a, **k): pass
        async def close(self): pass

    bot.api = _FakeAPI()

    # Add a fresh provider on disk
    os.environ["FAKE_NEW_KEY_AFTER"] = "secret-2"
    w2 = ConfigWriter()
    w2.add_provider("new-prov", name="New", format="openai_compat",
                    base_url="https://n", auth="bearer", enabled=True)
    w2.set_key("new-prov", "primary", "env:FAKE_NEW_KEY_AFTER")
    w2.add_model("new-prov", "new-m", display="New Model",
                 capabilities=["text"], context_window=8192,
                 max_output_tokens=2048)
    w2.save()

    await bot._reload_mesh(silent=True)

    # Mesh should be a different object and now contain new-prov
    assert bot.mesh is not initial_mesh, "mesh not rebuilt"
    assert "new-prov" in bot.mesh.providers, \
        f"new provider not visible after reload, got: {list(bot.mesh.providers.keys())}"


@pytest.mark.asyncio
async def test_tg_reload_mesh_preserves_current_model():
    """If the previously-selected model still exists, _reload_mesh keeps it."""
    from cogitum.core.llm.config_writer import ConfigWriter
    from cogitum.gateway.telegram import CogitumBot
    from cogitum.gateway.tg_config import TelegramConfig
    from cogitum.core.llm.loader import load_mesh

    # Seed provider with a real-shaped model id
    import os
    os.environ["FAKE_PRESERVE_KEY"] = "test-secret"
    w = ConfigWriter()
    w.add_provider("preserve-prov", name="P", format="openai_compat",
                   base_url="https://p", auth="bearer", enabled=True)
    w.set_key("preserve-prov", "primary", "env:FAKE_PRESERVE_KEY")
    w.add_model("preserve-prov", "preserve-model",
                display="Preserve", capabilities=["text"],
                context_window=8192, max_output_tokens=2048)
    w.save()

    cfg = TelegramConfig(bot_token="x:y", allowed_user_id=1, enabled=True)
    bot = CogitumBot(cfg)
    bot.mesh = load_mesh()

    # Make a mock agent with the preserve model selected
    class _FakeAgent:
        class _Cfg:
            model = "preserve-prov/preserve-model"
        cfg = _Cfg()
        mesh = bot.mesh

    bot.agent = _FakeAgent()

    class _FakeAPI:
        async def send_message(self, *a, **k): pass
        async def close(self): pass

    bot.api = _FakeAPI()

    # Reload — model must remain the same (still in mesh)
    await bot._reload_mesh(silent=True)
    assert bot.agent.cfg.model == "preserve-prov/preserve-model"


@pytest.mark.asyncio
async def test_tg_reload_handles_disk_error_gracefully():
    """If load_mesh fails, _reload_mesh keeps old mesh and reports error."""
    from cogitum.gateway.telegram import CogitumBot
    from cogitum.gateway.tg_config import TelegramConfig
    from cogitum.core.llm.loader import load_mesh
    from cogitum.core.llm.config_writer import ConfigWriter

    # Initial valid setup
    w = ConfigWriter()
    w.add_provider("err-prov", name="Err", format="openai_compat",
                   base_url="https://x", auth="bearer", enabled=True)
    w.set_key("err-prov", "primary", "env:FAKE_ERR_KEY")
    w.save()

    cfg = TelegramConfig(bot_token="x:y", allowed_user_id=1, enabled=True)
    bot = CogitumBot(cfg)
    bot.mesh = load_mesh()
    initial = bot.mesh

    sent = []

    class _FakeAPI:
        async def send_message(self, chat_id, msg, **kw):
            sent.append(msg)
        async def close(self): pass

    bot.api = _FakeAPI()

    # Force load_mesh to raise
    import cogitum.gateway.telegram as tg_mod
    original = tg_mod.load_mesh

    def boom():
        raise RuntimeError("disk corrupted")
    tg_mod.load_mesh = boom

    try:
        await bot._reload_mesh(silent=False, chat_id=1)
    finally:
        tg_mod.load_mesh = original

    # Old mesh kept, error message sent
    assert bot.mesh is initial
    assert any("reload failed" in s for s in sent), \
        f"error message missing, got: {sent}"
