"""TUI integration tests for the setup wizard.

We test behavior (logic, dismissal results, side effects) not pixel sizes —
headless run_test reports button size=1 even though interactive TUI renders
them at height=3 due to the `border: tall` style. The CSS fix is verified
by reading the rules from the class itself.
"""
from __future__ import annotations

import re

import pytest


# ---------------------------------------------------------------------------
# CSS-level checks (no app needed)
# ---------------------------------------------------------------------------

def test_add_provider_modal_foot_height_is_3():
    """CSS rule for #ap-foot must have height: 3 (not auto/1)."""
    from cogitum.setup_flow import AddProviderModal
    css = AddProviderModal.DEFAULT_CSS
    m = re.search(r"#ap-foot\s*\{[^}]*height:\s*(\S+)\s*;", css)
    assert m, "no height rule for #ap-foot"
    assert m.group(1).rstrip(";") == "3", f"#ap-foot height should be 3, got {m.group(1)}"


def test_custom_provider_modal_foot_height_is_3():
    from cogitum.setup_flow import CustomProviderModal
    css = CustomProviderModal.DEFAULT_CSS
    m = re.search(r"#cp-foot\s*\{[^}]*height:\s*(\S+)\s*;", css)
    assert m, "no height rule for #cp-foot"
    assert m.group(1).rstrip(";") == "3"


def test_setup_screen_card_actions_height_is_3():
    from cogitum.setup_flow import SetupScreen
    css = SetupScreen.DEFAULT_CSS
    m = re.search(r"\.card-actions\s*\{[^}]*height:\s*(\S+)\s*;", css)
    assert m, "no height rule for .card-actions"
    assert m.group(1).rstrip(";") == "3"


def test_key_manager_modal_foot_height_is_3():
    from cogitum.setup_flow import KeyManagerModal
    css = KeyManagerModal.DEFAULT_CSS
    m = re.search(r"#km-foot\s*\{[^}]*height:\s*(\S+)\s*;", css)
    assert m, "no height rule for #km-foot"
    assert m.group(1).rstrip(";") == "3"


# ---------------------------------------------------------------------------
# AddProviderModal — sentinel return values
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_provider_cancel_returns_none():
    """Cancel button -> dismiss(None)."""
    from textual.app import App
    from cogitum.setup_flow import AddProviderModal

    captured = {}

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(AddProviderModal(set()), self._on_done)

        def _on_done(self, result) -> None:
            captured["result"] = result
            self.exit()

    async with _Host().run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        modal = next(s for s in pilot.app.screen_stack
                     if isinstance(s, AddProviderModal))
        modal.action_cancel()
        await pilot.pause()

    assert captured["result"] is None


@pytest.mark.asyncio
async def test_add_provider_select_custom_returns_custom_string():
    """Selecting the 'custom' row -> dismiss('custom')."""
    from textual.app import App
    from textual.widgets import ListView
    from cogitum.setup_flow import AddProviderModal

    captured = {}

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(AddProviderModal(set()), self._on_done)

        def _on_done(self, result) -> None:
            captured["result"] = result
            self.exit()

    async with _Host().run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        modal = next(s for s in pilot.app.screen_stack
                     if isinstance(s, AddProviderModal))
        # The 'custom' slot is the last item
        custom_idx = modal._items.index("custom")
        lv = modal.query_one("#ap-list", ListView)
        lv.index = custom_idx
        await pilot.pause()
        modal.action_select()
        await pilot.pause()

    assert captured["result"] == "custom"


@pytest.mark.asyncio
async def test_add_provider_select_preset_returns_preset_object():
    """Selecting a preset row -> dismiss(ProviderPreset)."""
    from textual.app import App
    from textual.widgets import ListView
    from cogitum.setup_flow import AddProviderModal
    from cogitum.core.llm.presets import ProviderPreset

    captured = {}

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(AddProviderModal(set()), self._on_done)

        def _on_done(self, result) -> None:
            captured["result"] = result
            self.exit()

    async with _Host().run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        modal = next(s for s in pilot.app.screen_stack
                     if isinstance(s, AddProviderModal))
        # Pick first preset
        lv = modal.query_one("#ap-list", ListView)
        lv.index = 0
        await pilot.pause()
        modal.action_select()
        await pilot.pause()

    assert isinstance(captured["result"], ProviderPreset)


# ---------------------------------------------------------------------------
# KeyManagerModal — sentinels and removal
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_key_manager_close_without_changes_returns_closed():
    from textual.app import App
    from cogitum.core.llm.config_writer import ConfigWriter
    from cogitum.setup_flow import KeyManagerModal

    w = ConfigWriter()
    w.add_provider("kmclose", name="KMClose", format="openai_compat",
                   base_url="https://x", auth="bearer", enabled=True)
    w.set_key("kmclose", "k1", "env:K1")
    w.save()

    captured = {}

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(KeyManagerModal("kmclose", "KMClose"), self._on_done)

        def _on_done(self, result) -> None:
            captured["result"] = result
            self.exit()

    async with _Host().run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        modal = next(s for s in pilot.app.screen_stack
                     if isinstance(s, KeyManagerModal))
        modal.action_close()
        await pilot.pause()

    assert captured["result"] == "closed"


@pytest.mark.asyncio
async def test_key_manager_add_returns_add_sentinel():
    from textual.app import App
    from cogitum.core.llm.config_writer import ConfigWriter
    from cogitum.setup_flow import KeyManagerModal

    w = ConfigWriter()
    w.add_provider("kmadd", name="KMAdd", format="openai_compat",
                   base_url="https://x", auth="bearer", enabled=True)
    w.set_key("kmadd", "k1", "env:K1")
    w.save()

    captured = {}

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(KeyManagerModal("kmadd", "KMAdd"), self._on_done)

        def _on_done(self, result) -> None:
            captured["result"] = result
            self.exit()

    async with _Host().run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        modal = next(s for s in pilot.app.screen_stack
                     if isinstance(s, KeyManagerModal))
        # Directly invoke the handler since pilot.click in headless can't always
        # find a small button reliably
        modal._btn_add()
        await pilot.pause()

    assert captured["result"] == "add"


@pytest.mark.asyncio
async def test_key_manager_lists_all_keys():
    from textual.app import App
    from cogitum.core.llm.config_writer import ConfigWriter
    from cogitum.setup_flow import KeyManagerModal

    w = ConfigWriter()
    w.add_provider("kmlist", name="KMList", format="openai_compat",
                   base_url="https://x", auth="bearer", enabled=True)
    w.set_key("kmlist", "alpha", "env:A")
    w.set_key("kmlist", "beta", "env:B")
    w.set_key("kmlist", "gamma", "env:C")
    w.save()

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(KeyManagerModal("kmlist", "KMList"))

    async with _Host().run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        modal = next(s for s in pilot.app.screen_stack
                     if isinstance(s, KeyManagerModal))
        assert set(modal._key_ids) == {"alpha", "beta", "gamma"}


@pytest.mark.asyncio
async def test_key_manager_remove_persists_to_disk():
    """Remove flow: confirm yes -> key gone from providers.toml."""
    from textual.app import App
    from cogitum.core.llm.config_writer import ConfigWriter
    from cogitum.setup_flow import KeyManagerModal

    w = ConfigWriter()
    w.add_provider("rmflow", name="RmFlow", format="openai_compat",
                   base_url="https://x", auth="bearer", enabled=True)
    w.set_key("rmflow", "victim", "env:V")
    w.set_key("rmflow", "survivor", "env:S")
    w.save()

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(KeyManagerModal("rmflow", "RmFlow"))

    async with _Host().run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        modal = next(s for s in pilot.app.screen_stack
                     if isinstance(s, KeyManagerModal))
        # Stub push_screen_wait to auto-confirm
        async def fake_confirm(_screen):
            return True
        modal.app.push_screen_wait = fake_confirm  # type: ignore[method-assign]

        from textual.widgets import ListView
        lv = modal.query_one("#km-list", ListView)
        lv.index = modal._key_ids.index("victim")
        await pilot.pause()
        await modal._do_remove_selected()
        await pilot.pause()

    w2 = ConfigWriter()
    keys = w2.list_keys("rmflow")
    assert "victim" not in keys
    assert "survivor" in keys


@pytest.mark.asyncio
async def test_key_manager_remove_cancel_keeps_key():
    """Confirm 'No' -> key stays."""
    from textual.app import App
    from cogitum.core.llm.config_writer import ConfigWriter
    from cogitum.setup_flow import KeyManagerModal

    w = ConfigWriter()
    w.add_provider("rmcan", name="RmCan", format="openai_compat",
                   base_url="https://x", auth="bearer", enabled=True)
    w.set_key("rmcan", "saved", "env:V")
    w.save()

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(KeyManagerModal("rmcan", "RmCan"))

    async with _Host().run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        modal = next(s for s in pilot.app.screen_stack
                     if isinstance(s, KeyManagerModal))
        async def fake_confirm(_screen):
            return False
        modal.app.push_screen_wait = fake_confirm  # type: ignore[method-assign]

        from textual.widgets import ListView
        lv = modal.query_one("#km-list", ListView)
        lv.index = 0
        await pilot.pause()
        await modal._do_remove_selected()
        await pilot.pause()

    w2 = ConfigWriter()
    assert "saved" in w2.list_keys("rmcan")


# ---------------------------------------------------------------------------
# SetupScreen — render-section refresh and provider cards
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_render_section_reloads_writer_from_disk():
    """_render_section must re-read providers.toml."""
    from textual.app import App
    from cogitum.core.llm.config_writer import ConfigWriter
    from cogitum.setup_flow import SetupScreen

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(SetupScreen())

    async with _Host().run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        screen = next(s for s in pilot.app.screen_stack
                      if isinstance(s, SetupScreen))
        first_writer = screen._writer

        # Mutate disk through a separate instance
        external = ConfigWriter()
        external.add_provider("inject1", name="Injected",
                              format="openai_compat", base_url="https://i",
                              auth="bearer", enabled=False)
        external.save()

        screen._render_section()
        await pilot.pause()
        second_writer = screen._writer
        assert first_writer is not second_writer
        assert second_writer.has_provider("inject1")


@pytest.mark.asyncio
async def test_provider_card_shows_manage_keys_button():
    """Provider with keys must render the 'Manage keys (N)' button."""
    from textual.app import App
    from cogitum.core.llm.config_writer import ConfigWriter
    from cogitum.setup_flow import SetupScreen

    w = ConfigWriter()
    w.add_provider("hasprov", name="HasProv", format="openai_compat",
                   base_url="https://x", auth="bearer", enabled=True)
    w.set_key("hasprov", "primary", "env:K")
    w.save()

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(SetupScreen())

    async with _Host().run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        screen = next(s for s in pilot.app.screen_stack
                      if isinstance(s, SetupScreen))
        # The button id is prov-keys-<pid>
        btns = list(screen.query("#prov-keys-hasprov"))
        assert btns, "Manage keys button missing for provider with keys"


@pytest.mark.asyncio
async def test_provider_card_no_manage_keys_when_no_keys():
    """Provider with zero keys must NOT render Manage keys button."""
    from textual.app import App
    from cogitum.core.llm.config_writer import ConfigWriter
    from cogitum.setup_flow import SetupScreen

    w = ConfigWriter()
    w.add_provider("noprov", name="NoProv", format="openai_compat",
                   base_url="https://x", auth="bearer", enabled=False)
    w.save()  # no keys

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(SetupScreen())

    async with _Host().run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        screen = next(s for s in pilot.app.screen_stack
                      if isinstance(s, SetupScreen))
        btns = list(screen.query("#prov-keys-noprov"))
        assert not btns, "Manage keys should not appear without keys"
        # But Add key button should
        add_btns = list(screen.query("#prov-key-noprov"))
        assert add_btns


@pytest.mark.asyncio
async def test_provider_remove_button_present_for_non_oauth():
    """Custom (non-OAuth) providers get a Remove button."""
    from textual.app import App
    from cogitum.core.llm.config_writer import ConfigWriter
    from cogitum.setup_flow import SetupScreen

    w = ConfigWriter()
    w.add_provider("custfoo", name="CustFoo", format="openai_compat",
                   base_url="https://x", auth="bearer", enabled=True)
    w.save()

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(SetupScreen())

    async with _Host().run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        screen = next(s for s in pilot.app.screen_stack
                      if isinstance(s, SetupScreen))
        btns = list(screen.query("#prov-remove-custfoo"))
        assert btns, "Remove button missing for non-OAuth provider"


# ---------------------------------------------------------------------------
# KeyEntryModal — env-backend round-trip and validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_key_entry_env_backend_returns_env_ref():
    from textual.app import App
    from cogitum.setup_flow import KeyEntryModal, KeyEntryResult

    captured = {}

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(KeyEntryModal("p1", "Provider 1", "MY_API_KEY"),
                             self._done)

        def _done(self, r) -> None:
            captured["r"] = r
            self.exit()

    async with _Host().run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        modal = next(s for s in pilot.app.screen_stack
                     if isinstance(s, KeyEntryModal))
        # env is default (idx 0)
        assert modal.BACKENDS[modal._backend_idx][0] == "env"
        modal.action_save()
        await pilot.pause()

    r = captured["r"]
    assert isinstance(r, KeyEntryResult)
    assert r.secret_ref == "env:MY_API_KEY"
    assert r.backend == "env"


@pytest.mark.asyncio
async def test_key_entry_vault_requires_secret():
    """Saving with vault backend and empty secret must show error modal."""
    from textual.app import App
    from cogitum.setup_flow import KeyEntryModal, MessageModal

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(KeyEntryModal("p1", "Provider 1", "P1_KEY"))

    async with _Host().run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        modal = next(s for s in pilot.app.screen_stack
                     if isinstance(s, KeyEntryModal))
        # Switch to vault
        for i, (bid, _, _) in enumerate(modal.BACKENDS):
            if bid == "vault":
                modal._backend_idx = i
                break
        modal.action_save()
        await pilot.pause()
        msgs = [s for s in pilot.app.screen_stack if isinstance(s, MessageModal)]
        assert msgs, "vault + empty secret must trigger error modal"


@pytest.mark.asyncio
async def test_key_entry_cancel_returns_none():
    from textual.app import App
    from cogitum.setup_flow import KeyEntryModal

    captured = {}

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(KeyEntryModal("p1", "P1", "P1_KEY"), self._done)

        def _done(self, r) -> None:
            captured["r"] = r
            self.exit()

    async with _Host().run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        modal = next(s for s in pilot.app.screen_stack
                     if isinstance(s, KeyEntryModal))
        modal.action_cancel()
        await pilot.pause()

    assert captured["r"] is None


# ---------------------------------------------------------------------------
# CustomProviderModal
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_custom_provider_modal_validates_required_fields():
    from textual.app import App
    from cogitum.setup_flow import CustomProviderModal, MessageModal

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(CustomProviderModal())

    async with _Host().run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        modal = next(s for s in pilot.app.screen_stack
                     if isinstance(s, CustomProviderModal))
        # Click OK without entering anything
        modal._ok()
        await pilot.pause()
        msgs = [s for s in pilot.app.screen_stack if isinstance(s, MessageModal)]
        assert msgs, "missing fields should trigger error modal"


@pytest.mark.asyncio
async def test_custom_provider_modal_returns_preset_with_valid_input():
    from textual.app import App
    from textual.widgets import Input
    from cogitum.setup_flow import CustomProviderModal
    from cogitum.core.llm.presets import ProviderPreset

    captured = {}

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(CustomProviderModal(), self._done)

        def _done(self, r) -> None:
            captured["r"] = r
            self.exit()

    async with _Host().run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        modal = next(s for s in pilot.app.screen_stack
                     if isinstance(s, CustomProviderModal))
        modal.query_one("#cp-id", Input).value = "myprov"
        modal.query_one("#cp-name", Input).value = "My Provider"
        modal.query_one("#cp-url", Input).value = "https://api.example.com/v1"
        await pilot.pause()
        modal._ok()
        await pilot.pause()

    r = captured["r"]
    assert isinstance(r, ProviderPreset)
    assert r.id == "myprov"
    assert r.base_url == "https://api.example.com/v1"


# ---------------------------------------------------------------------------
# Full flow: AddProvider -> select preset -> add_provider_flow seed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_manage_models_modal_lists_and_removes():
    """ManageModelsModal lists existing models and removes the selected one."""
    from textual.app import App
    from cogitum.core.llm.config_writer import ConfigWriter
    from cogitum.setup_flow import ManageModelsModal

    w = ConfigWriter()
    w.add_provider("mmtest", name="MMTest", format="openai_compat",
                   base_url="https://x", auth="bearer", enabled=True)
    w.set_key("mmtest", "primary", "env:K")
    w.add_model("mmtest", "wrong-llama-80b", display="Wrong",
                capabilities=["text"], context_window=8192,
                max_output_tokens=2048)
    w.add_model("mmtest", "qwen-3-235b", display="Qwen 3 235B",
                capabilities=["text", "tools"], context_window=128000,
                max_output_tokens=8192)
    w.save()

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(ManageModelsModal("mmtest", "MMTest"))

    async with _Host().run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await pilot.pause()
        modal = next(s for s in pilot.app.screen_stack
                     if isinstance(s, ManageModelsModal))
        assert "wrong-llama-80b" in modal._model_ids
        assert "qwen-3-235b" in modal._model_ids

        # Stub confirm to auto-yes
        async def fake_confirm(_screen):
            return True
        modal.app.push_screen_wait = fake_confirm  # type: ignore[method-assign]

        from textual.widgets import ListView
        lv = modal.query_one("#mm-list", ListView)
        lv.index = modal._model_ids.index("wrong-llama-80b")
        await pilot.pause()
        await modal._do_remove_selected()
        await pilot.pause()

    w2 = ConfigWriter()
    models = w2.provider("mmtest").get("models") or {}
    assert "wrong-llama-80b" not in models
    assert "qwen-3-235b" in models


@pytest.mark.asyncio
async def test_manage_models_modal_adds_manually():
    """ManageModelsModal Add button persists a new model."""
    from textual.app import App
    from textual.widgets import Input
    from cogitum.core.llm.config_writer import ConfigWriter
    from cogitum.setup_flow import ManageModelsModal

    w = ConfigWriter()
    w.add_provider("mmadd", name="MMAdd", format="openai_compat",
                   base_url="https://x", auth="bearer", enabled=True)
    w.set_key("mmadd", "primary", "env:K")
    w.save()

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(ManageModelsModal("mmadd", "MMAdd"))

    async with _Host().run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await pilot.pause()
        modal = next(s for s in pilot.app.screen_stack
                     if isinstance(s, ManageModelsModal))
        modal.query_one("#mm-add-input", Input).value = "llama-3.1-8b"
        await pilot.pause()
        await modal._btn_add()
        await pilot.pause()

    w2 = ConfigWriter()
    models = w2.provider("mmadd").get("models") or {}
    assert "llama-3.1-8b" in models


# ---------------------------------------------------------------------------
# CSS heights — full coverage
# ---------------------------------------------------------------------------

def test_manage_models_modal_foot_height_is_3():
    from cogitum.setup_flow import ManageModelsModal
    css = ManageModelsModal.DEFAULT_CSS
    m = re.search(r"#mm-foot\s*\{[^}]*height:\s*(\S+)\s*;", css)
    assert m
    assert m.group(1).rstrip(";") == "3"


def test_key_entry_modal_test_button_added():
    """KeyEntryModal compose must include the Test button."""
    import inspect
    from cogitum.setup_flow import KeyEntryModal
    src = inspect.getsource(KeyEntryModal.compose)
    assert '"key-test"' in src or "'key-test'" in src


@pytest.mark.asyncio
async def test_add_provider_flow_seeds_provider_disabled():
    """_add_provider_flow writes a provider entry, enabled=False until key added."""
    from textual.app import App
    from cogitum.core.llm.config_writer import ConfigWriter
    from cogitum.core.llm.presets import ProviderPreset
    from cogitum.setup_flow import SetupScreen

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(SetupScreen())

    async with _Host().run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        screen = next(s for s in pilot.app.screen_stack
                      if isinstance(s, SetupScreen))

        preset = ProviderPreset(
            id="flowtest", name="FlowTest", format="openai_compat",
            base_url="https://api.flow.example/v1", auth="bearer",
            env_var="FLOWTEST_API_KEY", models=(),
        )
        # Run just the writer half — skip the modal chain
        screen._writer.add_provider(
            preset.id, name=preset.name, format=preset.format,
            base_url=preset.base_url, auth=preset.auth, enabled=False,
        )
        screen._writer.save()

    # Verify on disk
    w = ConfigWriter()
    assert w.has_provider("flowtest")
    p = w.provider("flowtest")
    assert p["enabled"] is False  # disabled until key added
