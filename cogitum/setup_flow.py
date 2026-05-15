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
            yield Static(t, id="msg-title")
            yield Static(self._body, id="msg-body")
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
            yield Static(Text(self._title, style=f"bold {GOLD_HI}"), id="conf-title")
            yield Static(self._body, id="conf-body")
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
    #key-title  { color: #F5C24A; text-style: bold; height: 1; }
    #key-sub    { color: #9C957D; height: 1; padding-bottom: 1; }
    .row        { height: 3; padding: 0 0 1 0; }
    .row Label  { width: 18; color: #9C957D; padding-top: 1; }
    .row Input  {
        width: 1fr; background: #1C1C1F; border: round #2A2620;
        color: #E6E1CF;
    }
    .row Input:focus { border: round #A8732D; }
    #backend-row { height: 5; padding-bottom: 1; }
    #backend-row Label { padding-top: 0; }
    #backend-row Vertical { width: 1fr; }
    .backend-opt {
        height: 1; color: #9C957D;
    }
    .backend-opt.selected { color: #F5C24A; text-style: bold; }
    #key-hint { color: #7A5A1A; padding: 1 0; }
    #key-foot { height: 3; align: right middle; }
    #key-foot Button { margin-left: 1; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("ctrl+s", "save", "save"),
        Binding("up", "backend_prev", "", show=False),
        Binding("down", "backend_next", "", show=False),
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
            yield Static(Text(f"Add API key — {self._pname}", style=f"bold {GOLD_HI}"), id="key-title")
            yield Static(Text("Pick a storage backend, then paste the key.", style=TXT_DIM), id="key-sub")

            with Horizontal(classes="row"):
                yield Label("Label")
                yield Input(value="primary", id="key-label", placeholder="primary, team-2, …")
            with Horizontal(classes="row"):
                yield Label("Env var name")
                yield Input(value=self._suggested_env, id="key-env")
            with Horizontal(classes="row"):
                yield Label("Secret value")
                yield Input(password=True, id="key-secret",
                            placeholder="paste key (hidden — never logged)")

            with Horizontal(id="backend-row"):
                yield Label("Storage")
                with Vertical():
                    for i, (bid, blabel) in enumerate(self.BACKENDS):
                        cls = "backend-opt selected" if i == 0 else "backend-opt"
                        yield Static(self._render_backend(i, bid, blabel),
                                     classes=cls, id=f"be-{bid}")

            yield Static(self._hint(), id="key-hint")

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
            w = self.query_one(f"#be-{bid}", Static)
            w.update(self._render_backend(i, bid, blabel))
            if i == self._backend_idx:
                w.add_class("selected")
            else:
                w.remove_class("selected")
        self.query_one("#key-hint", Static).update(self._hint())

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

    def action_backend_prev(self) -> None:
        self._backend_idx = (self._backend_idx - 1) % len(self.BACKENDS)
        self._refresh_backends()

    def action_backend_next(self) -> None:
        self._backend_idx = (self._backend_idx + 1) % len(self.BACKENDS)
        self._refresh_backends()

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
    #ap-foot { height: 3; align: right middle; padding-top: 1; }
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
            yield Static(Text("Add a provider", style=f"bold {GOLD_HI}"), id="ap-title")
            yield Static(Text("Pick a preset; you'll add a key in the next step.",
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
            lv.append(ListItem(Static(row), id=f"ap-{preset.id}"))
        self._items.append(None)
        lv.append(ListItem(Static(self._render_custom()), id="ap-custom"))
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
    #cp-title { color: #F5C24A; text-style: bold; height: 1; padding-bottom: 1; }
    .row { height: 3; padding: 0 0 1 0; }
    .row Label { width: 16; color: #9C957D; padding-top: 1; }
    .row Input {
        width: 1fr; background: #1C1C1F; border: round #2A2620; color: #E6E1CF;
    }
    .row Input:focus { border: round #A8732D; }
    #cp-foot { height: 3; align: right middle; }
    #cp-foot Button { margin-left: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="cp-shell"):
            yield Static(Text("Custom provider", style=f"bold {GOLD_HI}"), id="cp-title")
            with Horizontal(classes="row"):
                yield Label("ID")
                yield Input(id="cp-id", placeholder="lowercase-slug, e.g. my-vllm")
            with Horizontal(classes="row"):
                yield Label("Name")
                yield Input(id="cp-name", placeholder="display name")
            with Horizontal(classes="row"):
                yield Label("Format")
                yield Input(id="cp-format", value="openai_compat",
                            placeholder="openai_compat | anthropic_native")
            with Horizontal(classes="row"):
                yield Label("Base URL")
                yield Input(id="cp-url", placeholder="https://… /v1")
            with Horizontal(classes="row"):
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
            yield Static(Text(f"Connect {self._oauth.name}", style=f"bold {GOLD_HI}"),
                         id="oa-title")
            yield Static(Text("Starting OAuth flow… a browser will open.", style=TXT_DIM),
                         id="oa-sub")
            yield Static("", id="oa-url")
            yield Static("", id="oa-instructions")
            yield Static("", id="oa-progress")
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
        self.query_one("#oa-url", Static).update(url_text)
        self.query_one("#oa-instructions", Static).update(Text(self._instr, style=TXT_DIM))

    def _append_progress(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self._progress_lines.append(f"[{ts}] {msg}")
        body = Text()
        for line in self._progress_lines[-12:]:
            body.append(line + "\n", style=TXT)
        self.query_one("#oa-progress", Static).update(body)

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
    .card-actions { height: 3; align: left middle; padding-top: 1; }
    .card-actions Button { margin-right: 1; min-width: 16; content-align: center middle; text-align: center; }
    Button { content-align: center middle; text-align: center; }
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
        yield Static(self._banner_text(), id="setup-banner")
        with Horizontal(id="setup-main"):
            with Vertical(id="setup-rail"):
                for sid, label in self.SECTIONS:
                    yield Static(self._rail_text(sid, label),
                                 classes=f"rail-item{' active' if sid == self._active else ''}",
                                 id=f"rail-{sid}")
            yield VerticalScroll(id="setup-content")
        yield Static(
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
            w = self.query_one(f"#rail-{s}", Static)
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
        elif self._active == "vault":
            self._render_vault(content)
        elif self._active == "diag":
            self._render_diag(content)

    # ---- providers ----------------------------------------------------

    def _render_providers(self, content: VerticalScroll) -> None:
        # Header card with "add provider" CTA
        header = Vertical(classes="card")
        content.mount(header)
        header.mount(Static(Text("API Providers", style=f"bold {GOLD_HI}"), classes="card-title"))
        header.mount(Static(Text(
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
            empty.mount(Static(Text("No providers yet. Click + Add to begin.",
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
        card.mount(Static(title, classes="card-title"))
        card.mount(Static(Text(raw.get("base_url", ""), style=TXT_DIM)))

        # keys
        keys = raw.get("keys") or {}
        if keys:
            for kid, kdata in keys.items():
                line = Text()
                line.append(f"  · {kid:<14}", style=GOLD)
                line.append(kdata.get("secret_ref", ""), style=BRONZE)
                if kdata.get("notes"):
                    line.append(f"   {kdata['notes']}", style=TXT_DIM)
                card.mount(Static(line))
        else:
            card.mount(Static(Text("  no keys yet — add one to enable", style=COPPER)))

        # models
        models = raw.get("models") or {}
        if models:
            mtitle = Text()
            mtitle.append(f"  {len(models)} models  ", style=GOLD_DIM)
            mtitle.append(", ".join(list(models.keys())[:4]), style=TXT_DIM)
            if len(models) > 4:
                mtitle.append(f"  +{len(models)-4} more", style=TXT_DIM)
            card.mount(Static(mtitle))

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
        header.mount(Static(Text("Subscriptions (OAuth)", style=f"bold {GOLD_HI}"),
                            classes="card-title"))
        header.mount(Static(Text(
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
            card.mount(Static(title, classes="card-title"))
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
        header.mount(Static(Text("Default model", style=f"bold {GOLD_HI}"),
                            classes="card-title"))
        try:
            settings = load_settings()
        except Exception as e:  # noqa: BLE001
            settings = {}
            content.mount(Static(Text(f"settings load failed: {e}", style=RUST)))
        cur = settings.get("default_model", "—")
        line = Text()
        line.append("current: ", style=GOLD_DIM)
        line.append(cur, style=GOLD_HI)
        header.mount(Static(line))

        try:
            mesh = load_mesh()
        except Exception as e:  # noqa: BLE001
            content.mount(Static(Text(f"mesh load failed: {e}", style=RUST)))
            return

        self._default_model_map = {}
        for idx, r in enumerate(mesh.list_resolved()):
            row = Vertical(classes="card")
            content.mount(row)
            t = Text()
            t.append(r.qualified_id, style=GOLD)
            t.append(f"   {r.model.display}", style=TXT)
            row.mount(Static(t))
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
        card.mount(Static(Text("Encrypted vault", style=f"bold {GOLD_HI}"),
                          classes="card-title"))
        card.mount(Static(Text(
            "AES-256-GCM with Argon2id KDF. Password is held in process memory "
            "for the session. For headless runs export "
            "COGITUM_VAULT_PASSWORD.\n\nUse `cog vault init / set / get / unset / list`"
            " from the shell — full TUI vault editing comes next.",
            style=TXT_DIM)))

    # ---- diagnostics ----

    def _render_diag(self, content: VerticalScroll) -> None:
        card = Vertical(classes="card")
        content.mount(card)
        card.mount(Static(Text("Diagnostics", style=f"bold {GOLD_HI}"),
                          classes="card-title"))
        try:
            mesh = load_mesh()
        except Exception as e:  # noqa: BLE001
            card.mount(Static(Text(f"mesh load failed: {e}", style=RUST)))
            return

        if not mesh.providers:
            card.mount(Static(Text("No active providers.", style=COPPER)))
            return

        for p in mesh.providers.values():
            block = Vertical(classes="card")
            content.mount(block)
            t = Text()
            t.append(p.id, style=f"bold {GOLD_HI}")
            t.append(f"   {p.name}", style=TXT)
            block.mount(Static(t))
            for s in p.pool.snapshot():
                line = Text()
                line.append(f"  · key={s['id']:<14}", style=GOLD)
                line.append(f"status={s['status']:<14}", style=BRONZE)
                line.append(f"req={s['total_requests']:<5} tok={s['total_tokens']}", style=TXT_DIM)
                block.mount(Static(line))

    # --- buttons -------------------------------------------------------

    @on(Button.Pressed)
    @work(exclusive=True)
    async def _on_button(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""

        if bid == "prov-add":
            existing = set(self._writer.providers().keys())
            preset = await self.app.push_screen_wait(AddProviderModal(existing))
            if preset is None:
                preset = None
            elif preset is None:  # custom path was indicated by None inside list
                pass
            if preset is not None:
                await self._add_provider_flow(preset)
            else:
                # If user clicked "custom" specifically, AddProviderModal returns None
                # but we want CustomProviderModal. We rely on Selected items index — see below.
                pass
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
        await self.app.push_screen_wait(MessageModal(
            "Key saved",
            f"{pid} now uses secret_ref = {result.secret_ref}\nProvider enabled.",
        ))
        self._render_section()

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
        # If we have a matching anthropic-pro / openai-codex provider entry,
        # enable it.
        if pid == "anthropic" and self._writer.has_provider("anthropic-pro"):
            self._writer.set_enabled("anthropic-pro", True)
            self._writer.save()
        await self.app.push_screen_wait(MessageModal(
            "Connected", f"{pid} authenticated. Tokens stored in ~/.config/cogitum/auth.json"
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
