"""
Setup wizard — Textual UI for configuring providers, keys, OAuth and the
default model.

Architecture
------------
SetupScreen (hub)
  ├─ left rail: sections (Providers · Subscriptions · Default model · Vault · Diagnostics)
  ├─ right pane: section content with actions
  └─ pushes sub-modals for actual edits:
       AddProviderModal       — pick from preset catalog or define custom
       ProviderEditModal      — toggle enable, manage keys, manage models
       KeyEntryModal          — paste / pick storage backend (env/keyring/vault/plain)
       OAuthLoginModal        — drives anthropic / openai-codex login flow
       ConfirmModal           — generic yes/no
       MessageModal           — generic info / error

Everything mutates `~/.config/cogitum/providers.toml` through ConfigWriter
(tomlkit) so user comments and formatting survive. Settings.toml goes
through `write_settings`.

Visual language: Imperial Fists. No blue, only warm tonal palette from
`design.py`. Cards on charcoal, gold accents, bronze details, rust for
errors, parchment text.
"""

from __future__ import annotations

import asyncio
import time
import webbrowser
from dataclasses import dataclass
from typing import Awaitable, Callable

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Input, Label, ListItem, ListView, Static

from .core.auth import storage as auth_storage
from .core.auth.registry import REGISTRY as OAUTH_REGISTRY
from .core.auth.types import OAuthAuthInfo, OAuthCredentials, OAuthPrompt
from .core.llm.config_writer import ConfigWriter
from .core.llm.loader import _PROVIDERS_PATH, _SETTINGS_PATH, load_mesh, load_settings, write_settings
from .core.llm.presets import PROVIDER_PRESETS, ProviderPreset, by_id as preset_by_id
from .design import (
    BRONZE,
    COPPER,
    GOLD,
    GOLD_DIM,
    GOLD_HI,
    MUTED,
    OK,
    RUST,
    TXT,
    TXT_DIM,
)


# ---------------------------------------------------------------------------
# Non-selectable Static — prevents text highlight on click
# ---------------------------------------------------------------------------

class _Static(Static):
    """Static widget with text selection disabled."""
    ALLOW_SELECT = False


# ---------------------------------------------------------------------------
# Generic modals
# ---------------------------------------------------------------------------

class MessageModal(ModalScreen[None]):
    DEFAULT_CSS = """
    MessageModal { align: center middle; background: rgba(0,0,0,0.55); }
    #msg-shell {
        width: 60; padding: 1 2;
        background: #161618; border: round #7A5A1A;
    }
    #msg-title { color: #F5C24A; text-style: bold; }
    #msg-body  { color: #E6E1CF; padding: 1 0; }
    #msg-foot  { height: 3; align: right middle; }
    """
    BINDINGS = [Binding("escape", "dismiss", "close"), Binding("enter", "dismiss", "close")]

    def __init__(self, title: str, body: str, *, error: bool = False) -> None:
        super().__init__()
        self._title = title
        self._body = body
        self._error = error

    def compose(self) -> ComposeResult:
        with Vertical(id="msg-shell"):
            t = Text(self._title, style=f"bold {RUST if self._error else GOLD_HI}")
            yield _Static(t, id="msg-title")
            yield _Static(self._body, id="msg-body")
            with Horizontal(id="msg-foot"):
                yield Button("OK", id="msg-ok", variant="primary")

    @on(Button.Pressed, "#msg-ok")
    def _ok(self) -> None:
        self.dismiss(None)

    def action_dismiss(self) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmModal { align: center middle; background: rgba(0,0,0,0.55); }
    #conf-shell { width: 64; padding: 1 2; background: #161618; border: round #A8732D; }
    #conf-title { color: #F5C24A; text-style: bold; }
    #conf-body  { color: #E6E1CF; padding: 1 0; }
    #conf-foot  { height: 3; align: right middle; }
    #conf-foot Button { margin-left: 1; }
    """
    BINDINGS = [Binding("escape", "no", "cancel")]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="conf-shell"):
            yield _Static(Text(self._title, style=f"bold {GOLD_HI}"), id="conf-title")
            yield _Static(self._body, id="conf-body")
            with Horizontal(id="conf-foot"):
                yield Button("Cancel", id="conf-no")
                yield Button("Confirm", id="conf-yes", variant="primary")

    @on(Button.Pressed, "#conf-yes")
    def _yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#conf-no")
    def _no(self) -> None:
        self.dismiss(False)

    def action_no(self) -> None:
        self.dismiss(False)


# ---------------------------------------------------------------------------
# Key entry modal
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class KeyEntryResult:
    secret_ref: str       # e.g. "env:OPENAI_API_KEY", "keyring:cogitum:OPENAI_API_KEY"
    backend: str          # "env" | "keyring" | "plain"
    raw_value: str | None  # for keyring/plain
    label: str = "primary"


class KeyEntryModal(ModalScreen[KeyEntryResult | None]):
    """Collect an API key + storage backend choice."""

    DEFAULT_CSS = """
    KeyEntryModal { align: center middle; background: rgba(0,0,0,0.55); }
    #key-shell {
        width: 78; padding: 1 2;
        background: #161618; border: round #7A5A1A;
    }
    #key-title  { color: #F5C24A; text-style: bold; height: 1; margin-bottom: 1; }
    #key-sub    { color: #9C957D; height: 1; margin-bottom: 1; }
    .krow       { height: 4; margin-bottom: 0; }
    .krow Label { width: 18; color: #9C957D; content-align: left middle; height: 100%; padding: 0 1 0 0; }
    .krow Input {
        width: 1fr; background: #1C1C1F; border: round #2A2620;
        color: #E6E1CF; height: 3;
    }
    .krow Input:focus { border: round #A8732D; }
    #backend-section { height: auto; margin: 1 0; }
    #backend-label { color: #9C957D; height: 1; margin-bottom: 1; }
    .be-option {
        height: 2; padding: 0 1; margin-bottom: 0;
        color: #9C957D;
    }
    .be-option:hover { background: #1C1C1F; }
    .be-option.selected { color: #F5C24A; background: #1A1610; }
    #key-hint { color: #7A5A1A; height: auto; margin: 1 0; }
    #key-foot { height: 3; align: right middle; margin-top: 1; }
    #key-foot Button { margin-left: 1; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("ctrl+s", "save", "save"),
    ]

    BACKENDS = (
        ("keyring", "system keyring (libsecret/KWallet) — recommended"),
        ("env", "environment variable (set in shell rc)"),
        ("plain", "plain text in providers.toml (dev only)"),
    )

    def __init__(
        self,
        provider_id: str,
        provider_name: str,
        suggested_env: str = "",
    ) -> None:
        super().__init__()
        self._pid = provider_id
        self._pname = provider_name
        self._suggested_env = suggested_env or f"{provider_id.upper().replace('-','_')}_API_KEY"
        self._backend_idx = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="key-shell"):
            yield _Static(Text(f"Add API key — {self._pname}", style=f"bold {GOLD_HI}"), id="key-title")
            yield _Static(Text("Pick a storage backend, then paste the key.", style=TXT_DIM), id="key-sub")

            with Horizontal(classes="krow"):
                yield Label("Label")
                yield Input(value="primary", id="key-label", placeholder="primary, team-2, …")
            with Horizontal(classes="krow"):
                yield Label("Env var name")
                yield Input(value=self._suggested_env, id="key-env")
            with Horizontal(classes="krow"):
                yield Label("Secret value")
                yield Input(password=True, id="key-secret",
                            placeholder="paste key (hidden — never logged)")

            with Vertical(id="backend-section"):
                yield _Static(Text("Storage backend", style=GOLD_DIM), id="backend-label")
                for i, (bid, blabel) in enumerate(self.BACKENDS):
                    cls = "be-option selected" if i == 0 else "be-option"
                    yield _Static(self._render_backend(i, bid, blabel),
                                 classes=cls, id=f"be-{bid}")

            yield _Static(self._hint(), id="key-hint")

            with Horizontal(id="key-foot"):
                yield Button("Cancel", id="key-cancel")
                yield Button("Save",   id="key-save", variant="primary")

    def _render_backend(self, idx: int, bid: str, blabel: str) -> Text:
        prefix = "● " if idx == self._backend_idx else "○ "
        out = Text()
        out.append(prefix, style=GOLD_HI if idx == self._backend_idx else GOLD_DIM)
        out.append(f"{bid:<10}", style=GOLD if idx == self._backend_idx else TXT_DIM)
        out.append(blabel, style=TXT if idx == self._backend_idx else TXT_DIM)
        return out

    def _refresh_backends(self) -> None:
        for i, (bid, blabel) in enumerate(self.BACKENDS):
            w = self.query_one(f"#be-{bid}", _Static)
            w.update(self._render_backend(i, bid, blabel))
            if i == self._backend_idx:
                w.add_class("selected")
            else:
                w.remove_class("selected")
        self.query_one("#key-hint", _Static).update(self._hint())

    def on_click(self, event) -> None:
        """Handle clicks on backend options."""
        target = event.widget
        if target is None:
            return
        tid = target.id or ""
        if tid.startswith("be-"):
            # Find which backend was clicked
            for i, (bid, _) in enumerate(self.BACKENDS):
                if tid == f"be-{bid}":
                    self._backend_idx = i
                    self._refresh_backends()
                    break

    def _hint(self) -> Text:
        backend = self.BACKENDS[self._backend_idx][0]
        out = Text()
        if backend == "keyring":
            out.append("Saves to your system keyring under ", style=TXT_DIM)
            out.append("cogitum / <env-var>", style=BRONZE)
            out.append("\nproviders.toml will reference it as ", style=TXT_DIM)
            out.append("keyring:cogitum:<env-var>", style=GOLD)
        elif backend == "env":
            out.append("Add to your shell rc:  ", style=TXT_DIM)
            out.append(f"export {self._env()}=…", style=GOLD)
            out.append("\nproviders.toml will reference it as ", style=TXT_DIM)
            out.append(f"env:{self._env()}", style=GOLD)
        else:
            out.append("⚠ key will be written into providers.toml in clear text. ",
                       style=RUST)
            out.append("Rotate later.", style=TXT_DIM)
        return out

    def _env(self) -> str:
        try:
            return self.query_one("#key-env", Input).value or self._suggested_env
        except Exception:
            return self._suggested_env



    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        self._save()

    @on(Button.Pressed, "#key-cancel")
    def _cancel_btn(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#key-save")
    def _save_btn(self) -> None:
        self._save()

    def _save(self) -> None:
        label = (self.query_one("#key-label", Input).value or "primary").strip()
        env_var = (self.query_one("#key-env", Input).value or "").strip()
        secret = (self.query_one("#key-secret", Input).value or "").strip()
        backend = self.BACKENDS[self._backend_idx][0]

        if backend in ("keyring", "plain") and not secret:
            self.app.push_screen(MessageModal("Missing key", "Paste a secret first.", error=True))
            return
        if backend in ("keyring", "env") and not env_var:
            self.app.push_screen(MessageModal("Missing env name", "Provide an env var name.", error=True))
            return

        if backend == "keyring":
            try:
                import keyring as _kr
                _kr.set_password("cogitum", env_var, secret)
            except Exception as e:  # noqa: BLE001
                self.app.push_screen(MessageModal("keyring failed", str(e), error=True))
                return
            ref = f"keyring:cogitum:{env_var}"
        elif backend == "env":
            ref = f"env:{env_var}"
        else:
            ref = f"plain:{secret}"

        self.dismiss(KeyEntryResult(
            secret_ref=ref, backend=backend,
            raw_value=secret if backend != "env" else None,
            label=label,
        ))


# ---------------------------------------------------------------------------
# Add provider modal
# ---------------------------------------------------------------------------

class AddProviderModal(ModalScreen[ProviderPreset | None]):
    """Pick a preset or define a fully custom provider."""

    DEFAULT_CSS = """
    AddProviderModal { align: center middle; background: rgba(0,0,0,0.55); }
    #ap-shell {
        width: 84; height: 32; padding: 1 2;
        background: #161618; border: round #7A5A1A;
    }
    #ap-title { color: #F5C24A; text-style: bold; height: 1; }
    #ap-sub   { color: #9C957D; height: 1; padding-bottom: 1; }
    #ap-list  {
        height: 1fr; background: #0E0E11; border: round #2A2620;
        padding: 0 1;
    }
    #ap-list > ListItem.--highlight,
    #ap-list > ListItem:hover { background: #261E10; }
    #ap-foot { height: auto; align: right middle; padding-top: 1; }
    #ap-foot Button { margin-left: 1; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("enter", "select", "select"),
    ]

    def __init__(self, existing: set[str]) -> None:
        super().__init__()
        self._existing = existing
        self._items: list[ProviderPreset | None] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="ap-shell"):
            yield _Static(Text("Add a provider", style=f"bold {GOLD_HI}"), id="ap-title")
            yield _Static(Text("Pick a preset; you'll add a key in the next step.",
                              style=TXT_DIM), id="ap-sub")
            yield ListView(id="ap-list")
            with Horizontal(id="ap-foot"):
                yield Button("Cancel", id="ap-cancel")
                yield Button("Select", id="ap-ok", variant="primary")

    def on_mount(self) -> None:
        lv = self.query_one("#ap-list", ListView)
        for preset in PROVIDER_PRESETS:
            self._items.append(preset)
            row = self._render_preset(preset)
            lv.append(ListItem(_Static(row), id=f"ap-{preset.id}"))
        self._items.append(None)
        lv.append(ListItem(_Static(self._render_custom()), id="ap-custom"))
        lv.focus()

    def _render_preset(self, p: ProviderPreset) -> Text:
        out = Text()
        installed = p.id in self._existing
        out.append("✓ " if installed else "  ",
                   style=OK if installed else GOLD_DIM)
        out.append(f"{p.id:<14}", style=GOLD)
        out.append(p.name, style=TXT)
        out.append(f"  {p.format}", style=BRONZE)
        out.append(f"   {p.base_url}", style=TXT_DIM)
        return out

    def _render_custom(self) -> Text:
        out = Text()
        out.append("  ", style=GOLD_DIM)
        out.append(f"{'custom':<14}", style=GOLD_HI)
        out.append("Define a fully custom provider", style=TXT)
        out.append("   (base url, format, headers — manual)", style=TXT_DIM)
        return out

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_select(self) -> None:
        idx = self.query_one("#ap-list", ListView).index
        if idx is None:
            return
        self.dismiss(self._items[idx])

    @on(ListView.Selected, "#ap-list")
    def _on_selected(self) -> None:
        self.action_select()

    @on(Button.Pressed, "#ap-ok")
    def _ok(self) -> None:
        self.action_select()

    @on(Button.Pressed, "#ap-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Custom provider form (when "custom" picked)
# ---------------------------------------------------------------------------

class CustomProviderModal(ModalScreen[ProviderPreset | None]):
    """Free-form provider definition — id, name, base_url, format, auth."""

    DEFAULT_CSS = """
    CustomProviderModal { align: center middle; background: rgba(0,0,0,0.55); }
    #cp-shell { width: 78; padding: 1 2; background: #161618; border: round #7A5A1A; }
    #cp-title { color: #F5C24A; text-style: bold; height: 1; margin-bottom: 1; }
    .cprow { height: 4; margin-bottom: 0; }
    .cprow Label { width: 16; color: #9C957D; content-align: left middle; height: 100%; padding: 0 1 0 0; }
    .cprow Input {
        width: 1fr; background: #1C1C1F; border: round #2A2620; color: #E6E1CF; height: 3;
    }
    .cprow Input:focus { border: round #A8732D; }
    #cp-foot { height: 3; align: right middle; margin-top: 1; }
    #cp-foot Button { margin-left: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="cp-shell"):
            yield _Static(Text("Custom provider", style=f"bold {GOLD_HI}"), id="cp-title")
            with Horizontal(classes="cprow"):
                yield Label("ID")
                yield Input(id="cp-id", placeholder="lowercase-slug, e.g. my-vllm")
            with Horizontal(classes="cprow"):
                yield Label("Name")
                yield Input(id="cp-name", placeholder="display name")
            with Horizontal(classes="cprow"):
                yield Label("Format")
                yield Input(id="cp-format", value="openai_compat",
                            placeholder="openai_compat | anthropic_native")
            with Horizontal(classes="cprow"):
                yield Label("Base URL")
                yield Input(id="cp-url", placeholder="https://… /v1")
            with Horizontal(classes="cprow"):
                yield Label("Auth")
                yield Input(id="cp-auth", value="bearer",
                            placeholder="bearer | x_api_key | header_custom")
            with Horizontal(id="cp-foot"):
                yield Button("Cancel", id="cp-cancel")
                yield Button("Add", id="cp-ok", variant="primary")

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#cp-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#cp-ok")
    def _ok(self) -> None:
        pid = (self.query_one("#cp-id", Input).value or "").strip().lower()
        name = (self.query_one("#cp-name", Input).value or pid).strip() or pid
        fmt = (self.query_one("#cp-format", Input).value or "openai_compat").strip()
        url = (self.query_one("#cp-url", Input).value or "").strip()
        auth = (self.query_one("#cp-auth", Input).value or "bearer").strip()
        if not pid or not url:
            self.app.push_screen(MessageModal("Missing fields", "id and base_url are required.", error=True))
            return
        env_var = pid.upper().replace("-", "_") + "_API_KEY"
        self.dismiss(ProviderPreset(
            id=pid, name=name, format=fmt, base_url=url, auth=auth,
            env_var=env_var, models=(),
        ))


# ---------------------------------------------------------------------------
# OAuth login modal — drives anthropic / openai-codex
# ---------------------------------------------------------------------------

class OAuthLoginModal(ModalScreen[OAuthCredentials | None]):
    DEFAULT_CSS = """
    OAuthLoginModal { align: center middle; background: rgba(0,0,0,0.55); }
    #oa-shell { width: 86; padding: 1 2; background: #161618; border: round #7A5A1A; }
    #oa-title { color: #F5C24A; text-style: bold; height: 1; }
    #oa-sub { color: #9C957D; padding-bottom: 1; }
    #oa-url {
        background: #0E0E11; border: round #2A2620; color: #D9A23B;
        padding: 0 1; height: 3;
    }
    #oa-instructions { color: #9C957D; padding: 1 0; }
    #oa-progress {
        background: #0E0E11; border: round #2A2620; color: #A8732D;
        padding: 0 1; height: 1fr; min-height: 4;
    }
    #oa-paste {
        background: #1C1C1F; border: round #2A2620; color: #E6E1CF;
        padding: 0 1; height: 3; margin-top: 1;
    }
    #oa-paste:focus { border: round #A8732D; }
    #oa-foot { height: 3; align: right middle; padding-top: 1; }
    #oa-foot Button { margin-left: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, provider_id: str) -> None:
        super().__init__()
        self._pid = provider_id
        self._oauth = OAUTH_REGISTRY[provider_id]
        self._url: str | None = None
        self._instr: str = ""
        self._task: asyncio.Task | None = None
        self._paste_future: asyncio.Future[str] | None = None
        self._progress_lines: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="oa-shell"):
            yield _Static(Text(f"Connect {self._oauth.name}", style=f"bold {GOLD_HI}"),
                         id="oa-title")
            yield _Static(Text("Starting OAuth flow… a browser will open.", style=TXT_DIM),
                         id="oa-sub")
            yield _Static("", id="oa-url")
            yield _Static("", id="oa-instructions")
            yield _Static("", id="oa-progress")
            yield Input(placeholder="paste redirect URL here if browser is on another machine",
                        id="oa-paste")
            with Horizontal(id="oa-foot"):
                yield Button("Cancel", id="oa-cancel")

    async def on_mount(self) -> None:
        loop = asyncio.get_running_loop()
        self._paste_future = loop.create_future()
        self._task = asyncio.create_task(self._run_login())

    async def _run_login(self) -> None:
        async def on_auth(info: OAuthAuthInfo) -> None:
            self._url = info.url
            self._instr = info.instructions or ""
            self._render_url()
            self._append_progress("authorize URL prepared.")
            try:
                webbrowser.open(info.url)
                self._append_progress("browser launched.")
            except Exception:  # noqa: BLE001
                self._append_progress("could not auto-open browser — copy URL manually.")

        async def on_prompt(p: OAuthPrompt) -> str:
            self._append_progress("waiting for browser callback or pasted URL…")
            assert self._paste_future is not None
            return await self._paste_future

        async def on_progress(msg: str) -> None:
            self._append_progress(msg)

        try:
            creds = await self._oauth.login(
                on_auth=on_auth, on_prompt=on_prompt, on_progress=on_progress,
            )
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            self._append_progress(f"✗ failed: {e}")
            await asyncio.sleep(2.0)
            self.dismiss(None)
            return
        self._append_progress("✓ tokens received.")
        self.dismiss(creds)

    def _render_url(self) -> None:
        url_text = Text()
        url_text.append("authorize URL  ", style=GOLD_DIM)
        url_text.append(self._url or "", style=GOLD)
        self.query_one("#oa-url", _Static).update(url_text)
        self.query_one("#oa-instructions", _Static).update(Text(self._instr, style=TXT_DIM))

    def _append_progress(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self._progress_lines.append(f"[{ts}] {msg}")
        body = Text()
        for line in self._progress_lines[-12:]:
            body.append(line + "\n", style=TXT)
        self.query_one("#oa-progress", _Static).update(body)

    @on(Input.Submitted, "#oa-paste")
    def _on_paste(self, event: Input.Submitted) -> None:
        if self._paste_future and not self._paste_future.done():
            self._paste_future.set_result(event.value)
            self._append_progress("manual URL submitted, exchanging…")

    @on(Button.Pressed, "#oa-cancel")
    def _cancel(self) -> None:
        self._cleanup()
        self.dismiss(None)

    def action_cancel(self) -> None:
        self._cleanup()
        self.dismiss(None)

    def _cleanup(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        if self._paste_future and not self._paste_future.done():
            self._paste_future.cancel()


# ---------------------------------------------------------------------------
# Setup hub
# ---------------------------------------------------------------------------

class SetupScreen(Screen):
    """Main wizard — sections rail + content pane."""

    DEFAULT_CSS = """
    SetupScreen {
        background: #0E0E11;
        layout: vertical;
    }
    #setup-banner {
        height: 4; padding: 1 2; background: #0E0E11; color: #F5C24A;
        text-style: bold;
    }
    #setup-tagline { color: #7A5A1A; }
    #setup-main {
        layout: horizontal; height: 1fr;
    }
    #setup-rail {
        width: 28; min-width: 24;
        background: #161618; border-right: vkey #2A2620;
        padding: 1 1;
    }
    #setup-rail > .rail-item {
        height: 3; padding: 1 1;
        color: #9C957D;
    }
    #setup-rail > .rail-item.active {
        background: #261E10; color: #F5C24A; text-style: bold;
    }
    #setup-content {
        width: 1fr;
        padding: 1 2;
        background: #0E0E11;
        overflow-y: auto;
    }
    #setup-foot {
        height: 1;
        background: #0E0E11;
        color: #7A5A1A; padding: 0 2;
    }
    .card {
        background: #161618; border: round #2A2620;
        padding: 1 2; margin-bottom: 1;
    }
    .card-title {
        color: #F5C24A; text-style: bold; padding-bottom: 1;
    }
    .card-actions { height: auto; align: left middle; padding-top: 1; }
    .card-actions Button { margin-right: 1; }
    """

    BINDINGS = [
        Binding("escape", "back_to_app", "back to TUI"),
        Binding("ctrl+q", "quit", "quit"),
        Binding("up", "rail_prev", "", show=False),
        Binding("down", "rail_next", "", show=False),
        Binding("ctrl+r", "reload", "reload config"),
    ]

    SECTIONS = (
        ("providers", "Providers"),
        ("subs", "Subscriptions"),
        ("default", "Default model"),
        ("telegram", "Telegram"),
        ("vault", "Vault"),
        ("diag", "Diagnostics"),
    )

    def __init__(self) -> None:
        super().__init__()
        self._writer: ConfigWriter = ConfigWriter()
        self._active = "providers"
        self._default_model_map: dict[str, str] = {}

    # --- compose -------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield _Static(self._banner_text(), id="setup-banner")
        with Horizontal(id="setup-main"):
            with Vertical(id="setup-rail"):
                for sid, label in self.SECTIONS:
                    yield _Static(self._rail_text(sid, label),
                                 classes=f"rail-item{' active' if sid == self._active else ''}",
                                 id=f"rail-{sid}")
            yield VerticalScroll(id="setup-content")
        yield _Static(
            "esc back · ctrl+r reload · ctrl+q quit",
            id="setup-foot",
        )

    def _banner_text(self) -> Text:
        out = Text()
        out.append("⬡  COGITUM SETUP", style=f"bold {GOLD_HI}")
        out.append("\n")
        out.append(f"  config: {_PROVIDERS_PATH}", style=GOLD_DIM)
        return out

    def _rail_text(self, sid: str, label: str) -> Text:
        out = Text()
        glyph = "▸ " if sid == self._active else "  "
        out.append(glyph, style=GOLD if sid == self._active else GOLD_DIM)
        out.append(label, style=GOLD_HI if sid == self._active else TXT_DIM)
        return out

    def on_mount(self) -> None:
        self._render_section()

    # --- nav -----------------------------------------------------------

    def _set_active(self, sid: str) -> None:
        self._active = sid
        for s, _ in self.SECTIONS:
            w = self.query_one(f"#rail-{s}", _Static)
            label = next(l for sid_, l in self.SECTIONS if sid_ == s)
            w.update(self._rail_text(s, label))
            if s == sid:
                w.add_class("active")
            else:
                w.remove_class("active")
        self._render_section()

    def action_rail_next(self) -> None:
        ids = [s for s, _ in self.SECTIONS]
        idx = ids.index(self._active)
        self._set_active(ids[(idx + 1) % len(ids)])

    def action_rail_prev(self) -> None:
        ids = [s for s, _ in self.SECTIONS]
        idx = ids.index(self._active)
        self._set_active(ids[(idx - 1) % len(ids)])

    def action_back_to_app(self) -> None:
        self.app.pop_screen()

    def action_reload(self) -> None:
        self._writer = ConfigWriter()
        self._render_section()

    # --- click rail ----------------------------------------------------

    def on_click(self, event) -> None:
        target = event.widget
        if target is None or not target.id:
            return
        if target.id.startswith("rail-"):
            self._set_active(target.id.removeprefix("rail-"))

    # --- section dispatch ---------------------------------------------

    def _render_section(self) -> None:
        content = self.query_one("#setup-content", VerticalScroll)
        content.remove_children()
        # Force DOM cleanup before re-mounting
        self.app.refresh()

        if self._active == "providers":
            self._render_providers(content)
        elif self._active == "subs":
            self._render_subs(content)
        elif self._active == "default":
            self._render_default(content)
        elif self._active == "telegram":
            self._render_telegram(content)
        elif self._active == "vault":
            self._render_vault(content)
        elif self._active == "diag":
            self._render_diag(content)

    # ---- providers ----------------------------------------------------

    def _render_providers(self, content: VerticalScroll) -> None:
        # Header card with "add provider" CTA
        header = Vertical(classes="card")
        content.mount(header)
        header.mount(_Static(Text("API Providers", style=f"bold {GOLD_HI}"), classes="card-title"))
        header.mount(_Static(Text(
            "Each provider has many keys; the mesh balances load across them"
            " and falls back on rate-limit. Add as many as you like.",
            style=TXT_DIM)))
        actions = Horizontal(classes="card-actions")
        header.mount(actions)
        actions.mount(Button("+ Add provider", id="prov-add", variant="primary"))
        actions.mount(Button("Open providers.toml", id="prov-edit"))

        # One card per existing provider
        providers = self._writer.providers()
        if not providers:
            empty = Vertical(classes="card")
            content.mount(empty)
            empty.mount(_Static(Text("No providers yet. Click + Add to begin.",
                                    style=TXT_DIM)))
            return

        for pid, raw in providers.items():
            self._render_provider_card(content, pid, raw)

    def _render_provider_card(self, content, pid: str, raw) -> None:
        card = Vertical(classes="card")
        content.mount(card)
        title = Text()
        title.append(f"{pid}", style=f"bold {GOLD_HI}")
        title.append(f"   {raw.get('name', '')}", style=TXT_DIM)
        title.append(f"   {raw.get('format', 'openai_compat')}", style=BRONZE)
        if not bool(raw.get("enabled", True)):
            title.append("   [disabled]", style=COPPER)
        card.mount(_Static(title, classes="card-title"))
        card.mount(_Static(Text(raw.get("base_url", ""), style=TXT_DIM)))

        # keys
        keys = raw.get("keys") or {}
        if keys:
            for kid, kdata in keys.items():
                line = Text()
                line.append(f"  · {kid:<14}", style=GOLD)
                line.append(kdata.get("secret_ref", ""), style=BRONZE)
                if kdata.get("notes"):
                    line.append(f"   {kdata['notes']}", style=TXT_DIM)
                card.mount(_Static(line))
        else:
            card.mount(_Static(Text("  no keys yet — add one to enable", style=COPPER)))

        # models
        models = raw.get("models") or {}
        if models:
            mtitle = Text()
            mtitle.append(f"  {len(models)} models  ", style=GOLD_DIM)
            mtitle.append(", ".join(list(models.keys())[:4]), style=TXT_DIM)
            if len(models) > 4:
                mtitle.append(f"  +{len(models)-4} more", style=TXT_DIM)
            card.mount(_Static(mtitle))

        actions = Horizontal(classes="card-actions")
        card.mount(actions)
        actions.mount(Button("+ Add key", id=f"prov-key-{pid}"))
        if bool(raw.get("enabled", True)):
            actions.mount(Button("Disable", id=f"prov-disable-{pid}"))
        else:
            actions.mount(Button("Enable", id=f"prov-enable-{pid}", variant="primary"))

    # ---- subscriptions ----

    def _render_subs(self, content: VerticalScroll) -> None:
        header = Vertical(classes="card")
        content.mount(header)
        header.mount(_Static(Text("Subscriptions (OAuth)", style=f"bold {GOLD_HI}"),
                            classes="card-title"))
        header.mount(_Static(Text(
            "Use a Claude Pro/Max or ChatGPT Plus/Pro account instead of an API key. "
            "Cogitum opens your browser, completes the flow, and stores tokens in "
            "~/.config/cogitum/auth.json (mode 0600).", style=TXT_DIM)))

        for pid, oauth in OAUTH_REGISTRY.items():
            card = Vertical(classes="card")
            content.mount(card)
            creds = auth_storage.get(pid)
            title = Text()
            title.append(oauth.name, style=f"bold {GOLD_HI}")
            if creds:
                ttl_min = max(0.0, (creds.expires - time.time()) / 60.0)
                title.append("   ✓ logged in", style=OK)
                title.append(f"   refresh in {ttl_min:.1f}m", style=TXT_DIM)
            else:
                title.append("   not connected", style=COPPER)
            card.mount(_Static(title, classes="card-title"))
            actions = Horizontal(classes="card-actions")
            card.mount(actions)
            if creds:
                actions.mount(Button("Re-login", id=f"sub-login-{pid}"))
                actions.mount(Button("Logout", id=f"sub-logout-{pid}"))
            else:
                actions.mount(Button("Connect", id=f"sub-login-{pid}", variant="primary"))

    # ---- default ----

    def _render_default(self, content: VerticalScroll) -> None:
        header = Vertical(classes="card")
        content.mount(header)
        header.mount(_Static(Text("Default model", style=f"bold {GOLD_HI}"),
                            classes="card-title"))
        try:
            settings = load_settings()
        except Exception as e:  # noqa: BLE001
            settings = {}
            content.mount(_Static(Text(f"settings load failed: {e}", style=RUST)))
        cur = settings.get("default_model", "—")
        line = Text()
        line.append("current: ", style=GOLD_DIM)
        line.append(cur, style=GOLD_HI)
        header.mount(_Static(line))

        try:
            mesh = load_mesh()
        except Exception as e:  # noqa: BLE001
            content.mount(_Static(Text(f"mesh load failed: {e}", style=RUST)))
            return

        self._default_model_map = {}
        for idx, r in enumerate(mesh.list_resolved()):
            row = Vertical(classes="card")
            content.mount(row)
            t = Text()
            t.append(r.qualified_id, style=GOLD)
            t.append(f"   {r.model.display}", style=TXT)
            row.mount(_Static(t))
            actions = Horizontal(classes="card-actions")
            row.mount(actions)
            btn_id = f"def-model-{idx}"
            self._default_model_map[btn_id] = r.qualified_id
            actions.mount(Button(
                "Set as default", id=btn_id,
                variant="primary" if r.qualified_id != cur else "default",
            ))

    # ---- vault ----

    def _render_vault(self, content: VerticalScroll) -> None:
        card = Vertical(classes="card")
        content.mount(card)
        card.mount(_Static(Text("Encrypted vault", style=f"bold {GOLD_HI}"),
                          classes="card-title"))
        card.mount(_Static(Text(
            "AES-256-GCM with Argon2id KDF. Password is held in process memory "
            "for the session. For headless runs export "
            "COGITUM_VAULT_PASSWORD.\n\nUse `cog vault init / set / get / unset / list`"
            " from the shell — full TUI vault editing comes next.",
            style=TXT_DIM)))

    # ---- telegram gateway ----

    def _render_telegram(self, content: VerticalScroll) -> None:
        from .gateway.tg_config import load_tg_config, save_tg_config, TelegramConfig, TG_CONFIG_PATH
        from .gateway.daemon import status_service

        cfg = load_tg_config()
        status = status_service()

        # Header card
        card = Vertical(classes="card")
        content.mount(card)
        card.mount(_Static(Text("Telegram Gateway", style=f"bold {GOLD_HI}"),
                          classes="card-title"))
        card.mount(_Static(Text(
            "Connect Cogitum to Telegram — agent responds to your messages in a bot chat.",
            style=TXT_DIM)))

        # Status
        status_card = Vertical(classes="card")
        content.mount(status_card)
        status_card.mount(_Static(Text("Status", style=f"bold {GOLD}"), classes="card-title"))

        active_text = status.get("active", "unknown")
        is_running = "running" in active_text.lower() or "active" in active_text.lower()
        status_style = OK if is_running else MUTED
        status_card.mount(_Static(Text(f"  Daemon: {active_text}", style=status_style)))
        status_card.mount(_Static(Text(f"  Enabled: {status.get('enabled', '?')}", style=TXT_DIM)))

        if cfg.is_valid():
            token_display = f"{cfg.bot_token[:8]}...{cfg.bot_token[-4:]}"
            status_card.mount(_Static(Text(f"  Token: {token_display}", style=TXT_DIM)))
            status_card.mount(_Static(Text(f"  User ID: {cfg.allowed_user_id}", style=TXT_DIM)))
        else:
            status_card.mount(_Static(Text("  ⚠ Not configured", style=RUST)))

        # Config card
        config_card = Vertical(classes="card")
        content.mount(config_card)
        config_card.mount(_Static(Text("Configuration", style=f"bold {GOLD}"), classes="card-title"))
        config_card.mount(_Static(Text(f"  File: {TG_CONFIG_PATH}", style=TXT_DIM)))

        # Token input
        config_card.mount(_Static(Text("  Bot Token (from @BotFather):", style=TXT)))
        token_input = Input(
            value=cfg.bot_token or "",
            placeholder="paste bot token here",
            password=True,
            id="tg-token-input",
        )
        config_card.mount(token_input)

        # User ID input
        config_card.mount(_Static(Text("  Your Telegram User ID:", style=TXT)))
        uid_input = Input(
            value=str(cfg.allowed_user_id) if cfg.allowed_user_id else "",
            placeholder="numeric user ID (get from @userinfobot)",
            id="tg-uid-input",
        )
        config_card.mount(uid_input)

        # Action buttons
        actions = Horizontal(classes="card-actions")
        config_card.mount(actions)
        actions.mount(Button("Save", id="tg-save", variant="primary"))
        actions.mount(Button("Test", id="tg-test", variant="default"))

        # Daemon control buttons
        daemon_card = Vertical(classes="card")
        content.mount(daemon_card)
        daemon_card.mount(_Static(Text("Daemon Control", style=f"bold {GOLD}"), classes="card-title"))

        daemon_actions = Horizontal(classes="card-actions")
        daemon_card.mount(daemon_actions)
        if is_running:
            daemon_actions.mount(Button("Stop", id="tg-stop", variant="warning"))
            daemon_actions.mount(Button("Restart", id="tg-restart", variant="default"))
        else:
            daemon_actions.mount(Button("Start", id="tg-start", variant="success"))
        daemon_actions.mount(Button("Enable auto-start", id="tg-enable", variant="default"))

    @on(Button.Pressed, "#tg-save")
    def _tg_save(self, event: Button.Pressed) -> None:
        from .gateway.tg_config import save_tg_config, TelegramConfig
        try:
            token = self.query_one("#tg-token-input", Input).value.strip()
            uid_str = self.query_one("#tg-uid-input", Input).value.strip()
            if not token:
                self.app.notify("Token required", severity="error")
                return
            if not uid_str or not uid_str.isdigit():
                self.app.notify("Valid user ID required", severity="error")
                return
            cfg = TelegramConfig(
                bot_token=token,
                allowed_user_id=int(uid_str),
                enabled=True,
                show_thinking=True,
                show_tool_calls=True,
            )
            save_tg_config(cfg)
            self.app.notify("✓ Telegram config saved", severity="information")
            self._render_section()
        except Exception as e:
            self.app.notify(f"Save failed: {e}", severity="error")

    @on(Button.Pressed, "#tg-test")
    def _tg_test(self, event: Button.Pressed) -> None:
        import httpx
        try:
            token = self.query_one("#tg-token-input", Input).value.strip()
            if not token:
                self.app.notify("Enter token first", severity="warning")
                return
            resp = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
            data = resp.json()
            if data.get("ok"):
                name = data["result"].get("username", "?")
                self.app.notify(f"✓ Connected to @{name}", severity="information")
            else:
                self.app.notify(f"✗ {data.get('description')}", severity="error")
        except Exception as e:
            self.app.notify(f"✗ Connection failed: {e}", severity="error")

    @on(Button.Pressed, "#tg-start")
    def _tg_start(self, event: Button.Pressed) -> None:
        from .gateway.daemon import start_service
        result = start_service()
        self.app.notify(result, severity="information")
        self._render_section()

    @on(Button.Pressed, "#tg-stop")
    def _tg_stop(self, event: Button.Pressed) -> None:
        from .gateway.daemon import stop_service
        result = stop_service()
        self.app.notify(result, severity="information")
        self._render_section()

    @on(Button.Pressed, "#tg-restart")
    def _tg_restart(self, event: Button.Pressed) -> None:
        from .gateway.daemon import restart_service
        result = restart_service()
        self.app.notify(result, severity="information")
        self._render_section()

    @on(Button.Pressed, "#tg-enable")
    def _tg_enable(self, event: Button.Pressed) -> None:
        from .gateway.daemon import enable_service
        result = enable_service()
        self.app.notify(result, severity="information")
        self._render_section()

    # ---- diagnostics ----

    def _render_diag(self, content: VerticalScroll) -> None:
        card = Vertical(classes="card")
        content.mount(card)
        card.mount(_Static(Text("Diagnostics", style=f"bold {GOLD_HI}"),
                          classes="card-title"))
        try:
            mesh = load_mesh()
        except Exception as e:  # noqa: BLE001
            card.mount(_Static(Text(f"mesh load failed: {e}", style=RUST)))
            return

        if not mesh.providers:
            card.mount(_Static(Text("No active providers.", style=COPPER)))
            return

        for p in mesh.providers.values():
            block = Vertical(classes="card")
            content.mount(block)
            t = Text()
            t.append(p.id, style=f"bold {GOLD_HI}")
            t.append(f"   {p.name}", style=TXT)
            block.mount(_Static(t))
            for s in p.pool.snapshot():
                line = Text()
                line.append(f"  · key={s['id']:<14}", style=GOLD)
                line.append(f"status={s['status']:<14}", style=BRONZE)
                line.append(f"req={s['total_requests']:<5} tok={s['total_tokens']}", style=TXT_DIM)
                block.mount(_Static(line))

    # --- buttons -------------------------------------------------------

    @on(Button.Pressed)
    @work(exclusive=True)
    async def _on_button(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""

        if bid == "prov-add":
            existing = set(self._writer.providers().keys())
            preset = await self.app.push_screen_wait(AddProviderModal(existing))
            if preset is not None:
                await self._add_provider_flow(preset)
            else:
                # "custom" selected — open free-form provider definition
                custom_preset = await self.app.push_screen_wait(CustomProviderModal())
                if custom_preset is not None:
                    await self._add_provider_flow(custom_preset)
            self._render_section()
            return

        if bid == "prov-edit":
            self._open_in_editor()
            return

        if bid.startswith("prov-key-"):
            pid = bid.removeprefix("prov-key-")
            await self._add_key_flow(pid)
            return

        if bid.startswith("prov-disable-"):
            pid = bid.removeprefix("prov-disable-")
            self._writer.set_enabled(pid, False)
            self._writer.save()
            self._render_section()
            return

        if bid.startswith("prov-enable-"):
            pid = bid.removeprefix("prov-enable-")
            self._writer.set_enabled(pid, True)
            self._writer.save()
            self._render_section()
            return

        if bid.startswith("sub-login-"):
            pid = bid.removeprefix("sub-login-")
            await self._oauth_flow(pid)
            return

        if bid.startswith("sub-logout-"):
            pid = bid.removeprefix("sub-logout-")
            ok = await self.app.push_screen_wait(
                ConfirmModal("Logout", f"Remove tokens for {pid}?")
            )
            if ok:
                auth_storage.remove(pid)
                self._render_section()
            return

        if bid.startswith("def-model-"):
            qid = getattr(self, "_default_model_map", {}).get(bid, "")
            if not qid:
                return
            settings = load_settings()
            settings["default_model"] = qid
            write_settings(settings)
            await self.app.push_screen_wait(MessageModal("Saved", f"default = {qid}"))
            self._render_section()
            return

    # ---- flows --------------------------------------------------------

    async def _add_provider_flow(self, preset: ProviderPreset) -> None:
        # If id collides, just open key flow on the existing provider.
        if self._writer.has_provider(preset.id):
            await self._add_key_flow(preset.id)
            return

        self._writer.add_provider(
            preset.id,
            name=preset.name,
            format=preset.format,
            base_url=preset.base_url,
            auth=preset.auth,
            enabled=False,  # flip on after a key is added
            extra_headers=dict(preset.extra_headers) if preset.extra_headers else None,
        )
        for m in preset.models:
            self._writer.add_model(
                preset.id, m.id,
                display=m.display,
                aliases=list(m.aliases),
                capabilities=list(m.capabilities),
                context_window=m.context_window,
                max_output_tokens=m.max_output_tokens,
            )
        self._writer.save()
        # immediately walk into key entry
        await self._add_key_flow(preset.id, suggested_env=preset.env_var)

    async def _add_key_flow(self, pid: str, *, suggested_env: str = "") -> None:
        preset = preset_by_id(pid)
        env = suggested_env or (preset.env_var if preset else "")
        name = preset.name if preset else pid
        result = await self.app.push_screen_wait(KeyEntryModal(pid, name, env))
        if result is None:
            return
        self._writer.set_key(pid, result.label, result.secret_ref,
                             notes=f"added via setup, backend={result.backend}")
        # Auto-enable provider once it has a key
        self._writer.set_enabled(pid, True)
        self._writer.save()

        # Auto-discover models if provider has none
        await self._auto_discover_models(pid, result.secret_ref)

        await self.app.push_screen_wait(MessageModal(
            "Key saved",
            f"{pid} now uses secret_ref = {result.secret_ref}\nProvider enabled.",
        ))
        self._render_section()

    async def _auto_discover_models(self, pid: str, secret_ref: str) -> None:
        """Try to discover models from /v1/models endpoint."""
        from .core.llm.discovery import discover_models, resolve_secret_ref

        provider_data = self._writer.provider(pid)
        if not provider_data:
            return

        # Skip if provider already has models defined
        existing_models = provider_data.get("models", {})
        if existing_models and len(existing_models) > 0:
            return

        base_url = provider_data.get("base_url", "")
        if not base_url:
            return

        # Resolve the key
        api_key = resolve_secret_ref(secret_ref)
        if not api_key:
            return

        try:
            models = await discover_models(base_url, api_key)
        except Exception:  # noqa: BLE001
            return

        if not models:
            return

        # Add discovered models to config
        for m in models:
            self._writer.add_model(
                pid, m["model_id"],
                display=m.get("display", m["model_id"]),
                capabilities=m.get("capabilities", ["text", "tools"]),
                context_window=m.get("context_window", 128000),
                max_output_tokens=m.get("max_output_tokens", 16000),
            )
        self._writer.save()

    async def _oauth_flow(self, pid: str) -> None:
        if pid not in OAUTH_REGISTRY:
            await self.app.push_screen_wait(
                MessageModal("Unknown provider", pid, error=True)
            )
            return
        creds = await self.app.push_screen_wait(OAuthLoginModal(pid))
        if creds is None:
            return
        auth_storage.set_(pid, creds)

        # Auto-create and enable the matching provider entry
        if pid == "anthropic":
            target = "anthropic-pro"
            if not self._writer.has_provider(target):
                self._writer.add_provider(
                    target, name="Claude Pro/Max", format="anthropic_native",
                    base_url="https://api.anthropic.com", auth="bearer", enabled=True,
                )
                # Subscription tokens can't access model listing, add defaults
                _claude_models = [
                    ("claude-opus-4-5", "Claude Opus 4.5 (Pro)",
                     ["text", "vision", "reasoning", "tools", "caching"], 200000, 32000),
                    ("claude-sonnet-4-5", "Claude Sonnet 4.5 (Pro)",
                     ["text", "vision", "reasoning", "tools", "caching"], 200000, 16000),
                    ("claude-haiku-3-5", "Claude Haiku 3.5 (Pro)",
                     ["text", "vision", "tools", "caching"], 200000, 8192),
                ]
                for mid, display, caps, ctx, max_out in _claude_models:
                    self._writer.add_model(target, mid,
                        display=display, capabilities=caps,
                        context_window=ctx, max_output_tokens=max_out)
                self._writer.set_key(target, "subscription", "oauth:anthropic")
            self._writer.set_enabled(target, True)
            self._writer.save()

        elif pid == "openai-codex":
            target = "openai-codex"
            if not self._writer.has_provider(target):
                self._writer.add_provider(
                    target, name="ChatGPT Plus/Pro", format="openai_compat",
                    base_url="https://api.openai.com/v1", auth="bearer", enabled=True,
                )
                # Subscription tokens can't access /v1/models (403), add defaults
                _codex_models = [
                    ("gpt-5", "GPT-5", ["text", "vision", "reasoning", "tools"], 256000, 64000),
                    ("gpt-5-mini", "GPT-5 mini", ["text", "vision", "tools"], 256000, 32000),
                    ("o3", "o3", ["text", "reasoning", "tools"], 200000, 100000),
                    ("o4-mini", "o4-mini", ["text", "reasoning", "tools"], 200000, 65536),
                    ("gpt-4.1", "GPT-4.1", ["text", "vision", "tools"], 1048576, 32768),
                    ("gpt-4.1-mini", "GPT-4.1 mini", ["text", "vision", "tools"], 1048576, 32768),
                    ("gpt-4.1-nano", "GPT-4.1 nano", ["text", "tools"], 1048576, 32768),
                ]
                for mid, display, caps, ctx, max_out in _codex_models:
                    self._writer.add_model(target, mid,
                        display=display, capabilities=caps,
                        context_window=ctx, max_output_tokens=max_out)
                self._writer.set_key(target, "subscription", "oauth:openai-codex")
            self._writer.set_enabled(target, True)
            self._writer.save()

        await self.app.push_screen_wait(MessageModal(
            "Connected",
            f"{pid} authenticated. Tokens stored in ~/.config/cogitum/auth.json\n"
            f"Provider enabled — models now available in /models.",
        ))
        self._render_section()

    def _open_in_editor(self) -> None:
        import os, shutil, subprocess
        ed = os.environ.get("EDITOR") or shutil.which("nvim") or shutil.which("vim") or shutil.which("nano")
        if not ed:
            self.app.push_screen(MessageModal(
                "No editor", "$EDITOR not set; open the file from your shell.", error=True
            ))
            return
        # We need to suspend the textual app briefly.
        with self.app.suspend():
            subprocess.call([ed, str(_PROVIDERS_PATH)])
        self._writer = ConfigWriter()
        self._render_section()


__all__ = [
    "SetupScreen",
    "AddProviderModal",
    "CustomProviderModal",
    "KeyEntryModal",
    "OAuthLoginModal",
    "MessageModal",
    "ConfirmModal",
]


# silence unused
_: Callable[[str], Awaitable[None]] | None = None
_ = _SETTINGS_PATH
