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
import re
import time
import webbrowser
from dataclasses import dataclass
from typing import ClassVar, Awaitable, Callable

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
    BG,
    BG_SOFT,
    SURFACE,
    SURFACE_HI,
    RULE,
)
import logging

log = logging.getLogger(__name__)


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
    DEFAULT_CSS = f"""
    MessageModal {{ align: center middle; background: rgba(0,0,0,0.55); }}
    #msg-shell {{
        width: 60; padding: 1 2;
        background: {BG_SOFT}; border: round {GOLD_DIM};
    }}
    #msg-title {{ color: {GOLD_HI}; text-style: bold; }}
    #msg-body  {{ color: {TXT}; padding: 1 0; }}
    #msg-foot  {{ height: 3; align: right middle; }}
    """
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "dismiss", "close"), Binding("enter", "dismiss", "close")]

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
    DEFAULT_CSS = f"""
    ConfirmModal {{ align: center middle; background: rgba(0,0,0,0.55); }}
    #conf-shell {{ width: 64; padding: 1 2; background: {BG_SOFT}; border: round {BRONZE}; }}
    #conf-title {{ color: {GOLD_HI}; text-style: bold; }}
    #conf-body  {{ color: {TXT}; padding: 1 0; }}
    #conf-foot  {{ height: 3; align: right middle; }}
    #conf-foot Button {{ margin-left: 1; }}
    """
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "no", "cancel")]

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


class MaxTokensModal(ModalScreen[int | None]):
    """Prompt for a per-provider max_tokens override.

    Returns the new int value (>= 0) or None if the user cancels.
    Caller writes it to providers.toml via ConfigWriter.set_max_tokens.
    Empty input or 0 means "use the agent default" — caller should
    treat that as "clear the override".
    """

    DEFAULT_CSS = f"""
    MaxTokensModal {{ align: center middle; background: rgba(0,0,0,0.55); }}
    #mt-shell {{
        width: 70; padding: 1 2;
        background: {BG_SOFT}; border: round {GOLD_DIM};
    }}
    #mt-title  {{ color: {GOLD_HI}; text-style: bold; height: 1; margin-bottom: 1; }}
    #mt-sub    {{ color: {TXT_DIM}; height: auto; margin-bottom: 1; }}
    #mt-row    {{ height: 4; margin-bottom: 1; }}
    #mt-row Label {{ width: 18; color: {TXT_DIM}; content-align: left middle; height: 100%; padding: 0 1 0 0; }}
    #mt-row Input {{
        width: 1fr; background: {SURFACE}; border: round {RULE};
        color: {TXT}; height: 3;
    }}
    #mt-row Input:focus {{ border: round {BRONZE}; }}
    #mt-hint {{ color: {GOLD_DIM}; height: auto; margin: 1 0; }}
    #mt-foot {{ height: 3; align: right middle; }}
    #mt-foot Button {{ margin-left: 1; min-width: 10; }}
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "cancel"),
        Binding("ctrl+s", "save", "save"),
    ]

    def __init__(self, pid: str, current: int) -> None:
        super().__init__()
        self._pid = pid
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="mt-shell"):
            yield _Static(
                Text(f"Max tokens for {self._pid}", style=f"bold {GOLD_HI}"),
                id="mt-title",
            )
            yield _Static(Text(
                "Override the agent's per-turn max_tokens cap when "
                "routing through this provider. Leave empty (or 0) to "
                "fall back to the agent default (32768).",
                style=TXT_DIM,
            ), id="mt-sub")
            with Horizontal(id="mt-row"):
                yield Label("max_tokens")
                init = "" if self._current <= 0 else str(self._current)
                yield Input(
                    placeholder="32768",
                    value=init,
                    id="mt-input",
                )
            yield _Static(Text(
                "Hints: Sonnet 4 → 64000, GPT-5 → 128000, Qwen → 32768, "
                "DeepSeek-R1 → 8192. Don't set above what your model "
                "actually supports — the provider will reject the call.",
                style=GOLD_DIM,
            ), id="mt-hint")
            with Horizontal(id="mt-foot"):
                yield Button("Cancel", id="mt-cancel")
                yield Button("Save", id="mt-save", variant="primary")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        self._save()

    @on(Button.Pressed, "#mt-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#mt-save")
    def _save_btn(self) -> None:
        self._save()

    @on(Input.Submitted, "#mt-input")
    def _submit(self) -> None:
        self._save()

    def _save(self) -> None:
        raw = self.query_one("#mt-input", Input).value.strip()
        if not raw:
            self.dismiss(0)  # clear override
            return
        try:
            value = int(raw)
        except ValueError:
            # Bounce — leave the modal open with the bad value still
            # in the input so the user can fix it.
            return
        if value < 0:
            return
        self.dismiss(value)


class KeyEntryModal(ModalScreen[KeyEntryResult | None]):
    """Collect an API key + storage backend choice."""

    DEFAULT_CSS = f"""
    KeyEntryModal {{ align: center middle; background: rgba(0,0,0,0.55); }}
    #key-shell {{
        width: 78; padding: 1 2;
        background: {BG_SOFT}; border: round {GOLD_DIM};
    }}
    #key-title  {{ color: {GOLD_HI}; text-style: bold; height: 1; margin-bottom: 1; }}
    #key-sub    {{ color: {TXT_DIM}; height: 1; margin-bottom: 1; }}
    .krow       {{ height: 4; margin-bottom: 0; }}
    .krow Label {{ width: 18; color: {TXT_DIM}; content-align: left middle; height: 100%; padding: 0 1 0 0; }}
    .krow Input {{
        width: 1fr; background: {SURFACE}; border: round {RULE};
        color: {TXT}; height: 3;
    }}
    .krow Input:focus {{ border: round {BRONZE}; }}
    #backend-section {{ height: auto; margin: 1 0; }}
    #backend-label {{ color: {TXT_DIM}; height: 1; margin-bottom: 1; }}
    .be-option {{
        height: 2; padding: 0 1; margin-bottom: 0;
        color: {TXT_DIM};
    }}
    .be-option:hover {{ background: {SURFACE}; }}
    .be-option.selected {{ color: {GOLD_HI}; background: {BG_SOFT}; }}
    #key-hint {{ color: {GOLD_DIM}; height: auto; margin: 1 0; }}
    #key-foot {{ height: 3; align: right middle; margin-top: 1; }}
    #key-foot Button {{ margin-left: 1; min-width: 10; }}
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "cancel"),
        Binding("ctrl+s", "save", "save"),
    ]

    BACKENDS_BASE = (
        ("env",     "Environment variable",      "Set in your shell rc — most portable, recommended"),
        ("vault",   "Encrypted local vault",     "AES-GCM encrypted file in ~/.config/cogitum/"),
        ("keyring", "System keyring",            "OS password manager (libsecret/KWallet/macOS)"),
        ("plain",   "Plain text in config",      "▲ Stored unencrypted — dev/testing only"),
    )

    @classmethod
    def available_backends(cls):
        """Return only backends that work on this system."""
        out = []
        for bid, name, desc in cls.BACKENDS_BASE:
            if bid == "keyring":
                try:
                    import keyring  # type: ignore[import-not-found]
                    # Test that a backend exists
                    _ = keyring.get_keyring()
                    out.append((bid, name, desc))
                except Exception:
                    out.append((bid, name, desc + " (not installed — pip install keyring)"))
            else:
                out.append((bid, name, desc))
        return out

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
        self.BACKENDS = self.available_backends()
        # Default to first non-disabled backend (usually 'env')
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
                yield _Static(Text("Where to store the key", style=GOLD_DIM), id="backend-label")
                for i, (bid, name, desc) in enumerate(self.BACKENDS):
                    cls = "be-option selected" if i == 0 else "be-option"
                    yield _Static(self._render_backend(i, bid, name, desc),
                                 classes=cls, id=f"be-{bid}")

            yield _Static(self._hint(), id="key-hint")

            with Horizontal(id="key-foot"):
                yield Button("Cancel", id="key-cancel")
                yield Button("Test", id="key-test")
                yield Button("Save",   id="key-save", variant="primary")

    def _render_backend(self, idx: int, bid: str, name: str, desc: str) -> Text:
        prefix = "● " if idx == self._backend_idx else "○ "
        out = Text()
        out.append(prefix, style=GOLD_HI if idx == self._backend_idx else GOLD_DIM)
        out.append(f"{name:<26}", style=GOLD if idx == self._backend_idx else TXT_DIM)
        out.append(desc, style=TXT if idx == self._backend_idx else TXT_DIM)
        return out

    def _refresh_backends(self) -> None:
        for i, (bid, name, desc) in enumerate(self.BACKENDS):
            w = self.query_one(f"#be-{bid}", _Static)
            w.update(self._render_backend(i, bid, name, desc))
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
            for i, (bid, _, _) in enumerate(self.BACKENDS):
                if tid == f"be-{bid}":
                    self._backend_idx = i
                    self._refresh_backends()
                    break

    def _hint(self) -> Text:
        backend = self.BACKENDS[self._backend_idx][0]
        out = Text()
        if backend == "env":
            env = self._env()
            out.append("Add to ~/.bashrc or ~/.zshrc:\n", style=TXT_DIM)
            out.append(f"  export {env}=…\n", style=GOLD)
            out.append("Then restart the shell. Cogitum will read it as ", style=TXT_DIM)
            out.append(f"env:{env}", style=GOLD)
        elif backend == "vault":
            out.append("Encrypted with AES-GCM, key derived from a master password.\n", style=TXT_DIM)
            out.append("File: ", style=TXT_DIM)
            from .core.platform_paths import get_config_dir
            out.append(str(get_config_dir() / "vault.enc"), style=GOLD)
            out.append("\nYou'll be asked for the password once per session.", style=TXT_DIM)
        elif backend == "keyring":
            try:
                import keyring as _k
                _ = _k.get_keyring()
                out.append("Stores in your OS password manager under ", style=TXT_DIM)
                out.append("cogitum / <env-var>", style=BRONZE)
                out.append("\nUnlocked automatically when you log in.", style=TXT_DIM)
            except Exception:
                out.append("▲ keyring package not installed.\n", style=RUST)
                out.append("Cogitum will offer to install it when you Save.", style=TXT_DIM)
        else:  # plain
            out.append("▲ The key will be written into providers.toml in clear text.\n", style=RUST)
            out.append("Use only for local testing. Rotate the key after.", style=TXT_DIM)
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

    @on(Button.Pressed, "#key-test")
    async def _test_btn(self) -> None:
        """Probe the API endpoint with the entered key — before saving."""
        secret = (self.query_one("#key-secret", Input).value or "").strip()
        if not secret:
            self.app.push_screen(MessageModal(
                "Empty key", "Paste the API key first, then click Test.",
                error=True))
            return
        # Find provider's base_url from config
        from cogitum.core.llm.config_writer import ConfigWriter
        from cogitum.core.llm.discovery import discover_models
        writer = ConfigWriter()
        raw = writer.provider(self._pid)
        if not raw:
            self.app.push_screen(MessageModal(
                "Provider missing",
                f"{self._pid} not in providers.toml. Add it first.",
                error=True))
            return
        base_url = raw.get("base_url", "")
        if not base_url:
            self.app.push_screen(MessageModal(
                "No base_url", f"{self._pid} has no base_url set.",
                error=True))
            return
        if raw.get("format") == "anthropic_native":
            self.app.push_screen(MessageModal(
                "Test skipped",
                "Anthropic API doesn't expose /v1/models. Save the key — "
                "the agent will validate it on first use.",
            ))
            return
        self.app.notify(f"Testing {base_url}…", timeout=2)
        try:
            models = await discover_models(base_url, secret, timeout=8.0)
        except Exception as e:
            self.app.push_screen(MessageModal(
                "Test failed",
                f"Could not reach {base_url}/models:\n\n{e}\n\n"
                "Check the key and base_url, then try again.",
                error=True))
            return
        if not models:
            self.app.push_screen(MessageModal(
                "Test inconclusive",
                f"{base_url}/models returned 0 models. The key may still "
                "be valid — save and try a request.",
            ))
            return
        sample = ", ".join(m["model_id"] for m in models[:3])
        more = f" +{len(models)-3} more" if len(models) > 3 else ""
        self.app.push_screen(MessageModal(
            "✓ Connection OK",
            f"Found {len(models)} models:\n  {sample}{more}\n\n"
            "Click Save to persist this key.",
        ))

    @on(Button.Pressed, "#key-save")
    def _save_btn(self) -> None:
        self._save()

    def _save(self) -> None:
        label = (self.query_one("#key-label", Input).value or "primary").strip()
        env_var = (self.query_one("#key-env", Input).value or "").strip()
        secret = (self.query_one("#key-secret", Input).value or "").strip()
        backend = self.BACKENDS[self._backend_idx][0]

        # Validation
        if backend in ("keyring", "vault", "plain") and not secret:
            self.app.push_screen(MessageModal(
                "Missing key",
                "Paste the API key first.",
                error=True))
            return
        if backend in ("keyring", "env", "vault") and not env_var:
            self.app.push_screen(MessageModal(
                "Missing name",
                "Provide a name for the key (e.g. CEREBRAS_API_KEY).",
                error=True))
            return

        # Backend-specific save logic
        if backend == "keyring":
            try:
                import keyring as _kr
                _kr.set_password("cogitum", env_var, secret)
            except ImportError:
                # Detect package manager and suggest the right install command
                import shutil as _sh
                hints = []
                if _sh.which("pacman"):
                    hints.append("Arch:   sudo pacman -S python-keyring")
                if _sh.which("apt"):
                    hints.append("Debian: sudo apt install python3-keyring")
                if _sh.which("brew"):
                    hints.append("macOS:  brew install python-keyring")
                hints.append("Other:  pipx install keyring  (or pip install --user keyring)")
                self.app.push_screen(MessageModal(
                    "keyring not installed",
                    "The 'keyring' Python package isn't available.\n\n"
                    + "\n".join(hints) +
                    "\n\nOr pick a different backend below — "
                    "'env' and 'vault' work without extra packages.",
                    error=True))
                return
            except Exception as e:
                self.app.push_screen(MessageModal(
                    "Keyring save failed",
                    f"Could not write to system keyring:\n\n{e}\n\n"
                    "On headless systems (no D-Bus), use 'env' or 'vault' instead.",
                    error=True))
                return
            ref = f"keyring:cogitum:{env_var}"

        elif backend == "vault":
            try:
                from cogitum.core.llm.credentials import CredentialResolver
                resolver = CredentialResolver()
                # Vault unlock will prompt for password if needed (first time → set up)
                resolver.vault_set(env_var, secret)
            except Exception as e:
                self.app.push_screen(MessageModal(
                    "Vault save failed",
                    f"Could not write to encrypted vault:\n\n{e}",
                    error=True))
                return
            ref = f"vault:{env_var}"

        elif backend == "env":
            # Persist the secret value to ~/.config/cogitum/secrets.env so it
            # survives across sessions and is picked up at next CLI start.
            from cogitum.core.llm.secrets_env import save_secret
            if secret:
                try:
                    save_secret(env_var, secret)
                except Exception as e:
                    self.app.push_screen(MessageModal(
                        "Save failed",
                        f"Could not write secrets.env: {e}",
                        error=True))
                    return
            ref = f"env:{env_var}"

        else:  # plain
            ref = f"plain:{secret}"

        self.dismiss(KeyEntryResult(
            secret_ref=ref, backend=backend,
            raw_value=secret if backend != "env" else None,
            label=label,
        ))


# ---------------------------------------------------------------------------
# Add provider modal
# ---------------------------------------------------------------------------

class AddProviderModal(ModalScreen[ProviderPreset | str | None]):
    """Pick a preset or define a fully custom provider.

    Returns:
        ProviderPreset — preset chosen
        "custom"       — user picked the custom slot
        None           — user cancelled
    """

    DEFAULT_CSS = f"""
    AddProviderModal {{ align: center middle; background: rgba(0,0,0,0.55); }}
    #ap-shell {{
        width: 84; height: 32; padding: 1 2;
        background: {BG_SOFT}; border: round {GOLD_DIM};
    }}
    #ap-title {{ color: {GOLD_HI}; text-style: bold; height: 1; }}
    #ap-sub   {{ color: {TXT_DIM}; height: 1; padding-bottom: 1; }}
    #ap-list  {{
        height: 1fr; background: {BG}; border: round {RULE};
        padding: 0 1;
    }}
    #ap-list > ListItem.--highlight,
    #ap-list > ListItem:hover {{ background: {RULE}; }}
    #ap-foot {{ height: 3; align: right middle; margin-top: 1; }}
    #ap-foot Button {{ margin-left: 1; min-width: 12; }}
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "cancel"),
        Binding("enter", "select", "select"),
    ]

    def __init__(self, existing: set[str]) -> None:
        super().__init__()
        self._existing = existing
        # _items[i] is either a ProviderPreset or the string "custom"
        self._items: list[ProviderPreset | str] = []

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
        self._items.append("custom")
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

    DEFAULT_CSS = f"""
    CustomProviderModal {{ align: center middle; background: rgba(0,0,0,0.55); }}
    #cp-shell {{ width: 78; padding: 1 2; background: {BG_SOFT}; border: round {GOLD_DIM}; }}
    #cp-title {{ color: {GOLD_HI}; text-style: bold; height: 1; margin-bottom: 1; }}
    .cprow {{ height: 4; margin-bottom: 0; }}
    .cprow Label {{ width: 16; color: {TXT_DIM}; content-align: left middle; height: 100%; padding: 0 1 0 0; }}
    .cprow Input {{
        width: 1fr; background: {SURFACE}; border: round {RULE}; color: {TXT}; height: 3;
    }}
    .cprow Input:focus {{ border: round {BRONZE}; }}
    #cp-foot {{ height: 3; align: right middle; margin-top: 1; }}
    #cp-foot Button {{ margin-left: 1; min-width: 12; }}
    """

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "cancel")]

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
# Key manager — list / delete keys per provider
# ---------------------------------------------------------------------------

class KeyManagerModal(ModalScreen[str]):
    """Show all keys for a provider, allow removing them.

    Returns one of:
        "closed"     — user closed without changes
        "changed"    — keys were removed (caller should re-render)
        "add"        — user wants to add a new key (caller should chain add-key flow)
    """

    DEFAULT_CSS = f"""
    KeyManagerModal {{ align: center middle; background: rgba(0,0,0,0.55); }}
    #km-shell {{
        width: 88; height: 32; padding: 1 2;
        background: {BG_SOFT}; border: round {GOLD_DIM};
    }}
    #km-title {{ color: {GOLD_HI}; text-style: bold; height: 1; }}
    #km-sub   {{ color: {TXT_DIM}; height: 1; padding-bottom: 1; }}
    #km-list  {{
        height: 1fr; background: {BG}; border: round {RULE};
        padding: 0 1;
    }}
    #km-list > ListItem.--highlight,
    #km-list > ListItem:hover {{ background: {RULE}; }}
    #km-foot {{ height: 3; align: right middle; margin-top: 1; }}
    #km-foot Button {{ margin-left: 1; min-width: 12; }}
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "close"),
        Binding("delete", "remove_selected", "remove"),
        Binding("d", "remove_selected", "remove"),
    ]

    def __init__(self, pid: str, provider_name: str) -> None:
        super().__init__()
        self._pid = pid
        self._name = provider_name
        self._writer = ConfigWriter()
        self._key_ids: list[str] = []
        self._any_changed = False

    def compose(self) -> ComposeResult:
        with Vertical(id="km-shell"):
            yield _Static(Text(f"Manage keys — {self._name}",
                              style=f"bold {GOLD_HI}"), id="km-title")
            yield _Static(Text("Select a key, press Delete or 'd' to remove. Esc to close.",
                              style=TXT_DIM), id="km-sub")
            yield ListView(id="km-list")
            with Horizontal(id="km-foot"):
                yield Button("Remove", id="km-remove", variant="warning")
                yield Button("Add new", id="km-add")
                yield Button("Close", id="km-close", variant="primary")

    def on_mount(self) -> None:
        self._refresh_list()

    async def _refresh_list_async(self) -> None:
        """Async refresh — awaits clear() so old ids are gone before new mount."""
        lv = self.query_one("#km-list", ListView)
        await lv.clear()
        self._key_ids = []
        keys = self._writer.list_keys(self._pid)
        if not keys:
            await lv.append(ListItem(_Static(Text("  no keys — close and add one",
                                                style=COPPER))))
            return
        for kid, kdata in keys.items():
            self._key_ids.append(kid)
            row = self._render_key_row(kid, kdata)
            await lv.append(ListItem(_Static(row), id=f"km-row-{kid}"))
        lv.focus()

    def _refresh_list(self) -> None:
        """Sync wrapper — kicks off the async refresh as a task."""
        asyncio.ensure_future(self._refresh_list_async())

    def _render_key_row(self, kid: str, kdata) -> Text:
        out = Text()
        out.append(f"  {kid:<14}", style=GOLD)
        ref = kdata.get("secret_ref", "")
        # Mask secret references for safety in display
        if ref.startswith("env:"):
            out.append(ref, style=BRONZE)
        elif ref.startswith("vault:"):
            out.append(ref, style=BRONZE)
        elif ref.startswith("keyring:"):
            out.append(ref, style=BRONZE)
        elif ref.startswith("oauth:"):
            out.append(ref, style=GOLD_DIM)
        else:
            # plain — show truncated
            shown = ref[:6] + "…" + ref[-4:] if len(ref) > 12 else ref
            out.append(f"plain:{shown}", style=COPPER)
        notes = kdata.get("notes")
        if notes:
            out.append(f"   {notes}", style=TXT_DIM)
        return out

    @on(Button.Pressed, "#km-remove")
    async def _btn_remove(self) -> None:
        await self._do_remove_selected()

    @on(Button.Pressed, "#km-add")
    def _btn_add(self) -> None:
        # Signal caller to open add-key flow
        self.dismiss("add")

    @on(Button.Pressed, "#km-close")
    def _btn_close(self) -> None:
        self.dismiss("changed" if self._any_changed else "closed")

    def action_close(self) -> None:
        self.dismiss("changed" if self._any_changed else "closed")

    async def action_remove_selected(self) -> None:
        await self._do_remove_selected()

    async def _do_remove_selected(self) -> None:
        if not self._key_ids:
            self.app.notify("No keys to remove", severity="warning")
            return
        lv = self.query_one("#km-list", ListView)
        idx = lv.index
        if idx is None or idx >= len(self._key_ids):
            idx = 0
        kid = self._key_ids[idx]
        ok = await self.app.push_screen_wait(
            ConfirmModal("Remove key", f"Delete key '{kid}' from {self._name}?")
        )
        if not ok:
            return
        self._writer.remove_key(self._pid, kid)
        self._writer.save()
        self._any_changed = True
        await self._refresh_list_async()
        self.app.notify(f"Removed key '{kid}'", severity="information")


# ---------------------------------------------------------------------------
# Model manager — list / delete / add models per provider
# ---------------------------------------------------------------------------

class ManageModelsModal(ModalScreen[bool]):
    """Show all models for a provider, allow removing or adding manually."""

    DEFAULT_CSS = f"""
    ManageModelsModal {{ align: center middle; background: rgba(0,0,0,0.55); }}
    #mm-shell {{
        width: 96; height: 36; padding: 1 2;
        background: {BG_SOFT}; border: round {GOLD_DIM};
    }}
    #mm-title {{ color: {GOLD_HI}; text-style: bold; height: 1; }}
    #mm-sub   {{ color: {TXT_DIM}; height: 1; padding-bottom: 1; }}
    #mm-list  {{
        height: 1fr; background: {BG}; border: round {RULE};
        padding: 0 1;
    }}
    #mm-list > ListItem.--highlight,
    #mm-list > ListItem:hover {{ background: {RULE}; }}
    #mm-add-row {{ height: 3; margin-top: 1; }}
    #mm-add-row Input {{
        background: {SURFACE}; border: round {RULE}; color: {TXT};
        height: 3; width: 1fr;
    }}
    #mm-add-row Input:focus {{ border: round {BRONZE}; }}
    #mm-add-row Button {{ margin-left: 1; min-width: 10; height: 3; }}
    #mm-foot {{ height: 3; align: right middle; margin-top: 1; }}
    #mm-foot Button {{ margin-left: 1; min-width: 10; height: 3; }}
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "close"),
        Binding("delete", "remove_selected", "remove"),
        Binding("d", "remove_selected", "remove"),
    ]

    def __init__(self, pid: str, provider_name: str) -> None:
        super().__init__()
        self._pid = pid
        self._name = provider_name
        self._writer = ConfigWriter()
        self._model_ids: list[str] = []
        self._any_changed = False

    def compose(self) -> ComposeResult:
        with Vertical(id="mm-shell"):
            yield _Static(Text(f"Manage models — {self._name}",
                              style=f"bold {GOLD_HI}"), id="mm-title")
            yield _Static(Text("Delete/d to remove · Esc to close · "
                             "type below to add a model manually",
                             style=TXT_DIM), id="mm-sub")
            yield ListView(id="mm-list")
            with Horizontal(id="mm-add-row"):
                yield Input(placeholder="model_id (e.g. llama-3.1-8b)",
                            id="mm-add-input")
                yield Button("Add", id="mm-add", variant="primary")
            with Horizontal(id="mm-foot"):
                yield Button("Remove", id="mm-remove", variant="warning")
                yield Button("Close", id="mm-close", variant="primary")

    def on_mount(self) -> None:
        asyncio.ensure_future(self._refresh_async())

    async def _refresh_async(self) -> None:
        lv = self.query_one("#mm-list", ListView)
        await lv.clear()
        self._model_ids = []
        raw = self._writer.provider(self._pid)
        if not raw:
            await lv.append(ListItem(_Static(Text("  provider not found",
                                                style=RUST))))
            return
        models = raw.get("models") or {}
        if not models:
            await lv.append(ListItem(_Static(Text(
                "  no models — type one below or use Refresh models",
                style=COPPER))))
            return
        for mid, mdata in models.items():
            self._model_ids.append(mid)
            row = self._render_model_row(mid, mdata)
            await lv.append(ListItem(_Static(row), id=f"mm-row-{_safe_id(mid)}"))
        lv.focus()

    def _render_model_row(self, mid: str, mdata) -> Text:
        out = Text()
        out.append(f"  {mid}", style=GOLD)
        display = mdata.get("display", "")
        if display and display != mid:
            out.append(f"  · {display}", style=TXT_DIM)
        ctx = mdata.get("context_window")
        if ctx:
            out.append(f"   ctx={ctx//1000}k", style=BRONZE)
        caps = mdata.get("capabilities", [])
        if caps:
            out.append(f"   {','.join(caps[:3])}", style=GOLD_DIM)
        return out

    @on(Button.Pressed, "#mm-remove")
    async def _btn_remove(self) -> None:
        await self._do_remove_selected()

    @on(Button.Pressed, "#mm-add")
    async def _btn_add(self) -> None:
        inp = self.query_one("#mm-add-input", Input)
        mid = (inp.value or "").strip()
        if not mid:
            self.app.notify("Enter a model_id first", severity="warning")
            return
        raw = self._writer.provider(self._pid)
        if raw and mid in (raw.get("models") or {}):
            self.app.notify(f"Model '{mid}' already exists", severity="warning")
            return
        # Add with sensible defaults
        self._writer.add_model(
            self._pid, mid,
            display=_humanize(mid),
            capabilities=_infer_caps(mid),
            context_window=128000,
            max_output_tokens=8192,
        )
        self._writer.save()
        self._any_changed = True
        inp.value = ""
        await self._refresh_async()
        self.app.notify(f"Added {mid}", severity="information")

    @on(Button.Pressed, "#mm-close")
    def _btn_close(self) -> None:
        self.dismiss(self._any_changed)

    def action_close(self) -> None:
        self.dismiss(self._any_changed)

    async def action_remove_selected(self) -> None:
        await self._do_remove_selected()

    async def _do_remove_selected(self) -> None:
        if not self._model_ids:
            self.app.notify("No models to remove", severity="warning")
            return
        lv = self.query_one("#mm-list", ListView)
        idx = lv.index
        if idx is None or idx >= len(self._model_ids):
            idx = 0
        mid = self._model_ids[idx]
        ok = await self.app.push_screen_wait(
            ConfirmModal("Remove model", f"Delete model '{mid}' from {self._name}?")
        )
        if not ok:
            return
        # Remove via raw doc edit
        raw = self._writer.provider(self._pid)
        if raw:
            models = raw.get("models")
            if isinstance(models, dict) and mid in models:
                del models[mid]
            elif models is not None and mid in models:
                del models[mid]
        self._writer.save()
        self._any_changed = True
        await self._refresh_async()
        self.app.notify(f"Removed {mid}", severity="information")


def _safe_id(s: str) -> str:
    """Make a string safe for Textual widget ids (no slashes/colons/dots)."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", s)


def _humanize(mid: str) -> str:
    name = mid.rsplit("/", 1)[-1].split(":")[0]
    name = re.sub(r"[-_]+", " ", name)
    return name.title()


def _infer_caps(mid: str) -> list[str]:
    caps = ["text", "tools"]
    lower = mid.lower()
    if any(t in lower for t in ("vision", "vl", "vlm")):
        caps.append("vision")
    if any(t in lower for t in ("thinking", "reasoning", "r1", "o1", "o3")):
        caps.append("reasoning")
    return caps


# ---------------------------------------------------------------------------
# OAuth login modal — drives anthropic / openai-codex
# ---------------------------------------------------------------------------

class OAuthLoginModal(ModalScreen[OAuthCredentials | None]):
    DEFAULT_CSS = f"""
    OAuthLoginModal {{ align: center middle; background: rgba(0,0,0,0.55); }}
    #oa-shell {{ width: 86; padding: 1 2; background: {BG_SOFT}; border: round {GOLD_DIM}; }}
    #oa-title {{ color: {GOLD_HI}; text-style: bold; height: 1; }}
    #oa-sub {{ color: {TXT_DIM}; padding-bottom: 1; }}
    #oa-url {{
        background: {BG}; border: round {RULE}; color: {GOLD};
        padding: 0 1; height: 3;
    }}
    #oa-instructions {{ color: {TXT_DIM}; padding: 1 0; }}
    #oa-progress {{
        background: {BG}; border: round {RULE}; color: {BRONZE};
        padding: 0 1; height: 1fr; min-height: 4;
    }}
    #oa-paste {{
        background: {SURFACE}; border: round {RULE}; color: {TXT};
        padding: 0 1; height: 3; margin-top: 1;
    }}
    #oa-paste:focus {{ border: round {BRONZE}; }}
    #oa-foot {{ height: 3; align: right middle; padding-top: 1; }}
    #oa-foot Button {{ margin-left: 1; }}
    """

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "cancel")]

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
            except Exception:
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
        except Exception as e:
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

    DEFAULT_CSS = f"""
    SetupScreen {{
        background: {BG};
        layout: vertical;
    }}
    #setup-banner {{
        height: 4; padding: 1 2; background: {BG}; color: {GOLD_HI};
        text-style: bold;
    }}
    #setup-tagline {{ color: {GOLD_DIM}; }}
    #setup-main {{
        layout: horizontal; height: 1fr;
    }}
    #setup-rail {{
        width: 28; min-width: 24;
        background: {BG_SOFT}; border-right: vkey {RULE};
        padding: 1 1;
    }}
    #setup-rail > .rail-item {{
        height: 3; padding: 1 1;
        color: {TXT_DIM};
    }}
    #setup-rail > .rail-item.active {{
        background: {RULE}; color: {GOLD_HI}; text-style: bold;
    }}
    #setup-content {{
        width: 1fr;
        padding: 1 2;
        background: {BG};
        overflow-y: auto;
    }}
    #setup-foot {{
        height: 1;
        background: {BG};
        color: {GOLD_DIM}; padding: 0 2;
    }}
    .card {{
        background: {BG_SOFT}; border: round {RULE};
        padding: 1 2; margin-bottom: 1;
    }}
    .card-title {{
        color: {GOLD_HI}; text-style: bold; padding-bottom: 1;
    }}
    .card-actions {{ height: 3; align: left middle; margin-top: 1; }}
    .card-actions Button {{ margin-right: 1; min-width: 10; }}
    """

    BINDINGS: ClassVar[list[Binding]] = [
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
        ("mcp", "MCP servers"),
        ("vault", "Vault"),
        ("themes", "Themes"),
        ("experimental", "Experimental"),
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
        # Always re-read config from disk so models/keys/providers reflect
        # the latest state after Save (auto-discovery, manual edits, etc.).
        self._writer = ConfigWriter()
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
        elif self._active == "mcp":
            from .setup_mcp import render_mcp_section
            render_mcp_section(content)
        elif self._active == "vault":
            self._render_vault(content)
        elif self._active == "themes":
            self._render_themes(content)
        elif self._active == "experimental":
            self._render_experimental(content)
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

    def _provider_status(self, pid: str, raw) -> tuple[str, str]:
        """Compute (icon, message) for the provider status line."""
        from cogitum.core.llm.discovery import resolve_secret_ref

        keys = raw.get("keys") or {}
        models = raw.get("models") or {}
        enabled = bool(raw.get("enabled", True))

        if not keys:
            return ("▲", "no key — click '+ Add key' to enable")
        if not enabled:
            return ("○", "disabled — click 'Enable' to use")

        # Check first key resolves
        first_key = next(iter(keys.values()))
        ref = first_key.get("secret_ref", "")
        if ref.startswith("oauth:"):
            from cogitum.core.auth import storage as auth_storage
            pid_oauth = ref.removeprefix("oauth:")
            creds = auth_storage.get(pid_oauth)
            if not creds:
                return ("▲", "OAuth not connected — click 'Connect' in Subscriptions")
        elif ref.startswith("env:") or ref.startswith("vault:") or ref.startswith("plain:"):
            try:
                value = resolve_secret_ref(ref)
                if not value:
                    return ("▲", f"key not resolvable ({ref}) — re-enter via 'Manage keys'")
            except Exception as e:
                return ("▲", f"key error: {e}")

        if not models:
            return ("▲", "no models — click 'Refresh models' to discover")

        return ("✓", f"ready · {len(models)} models")

    def _render_provider_card(self, content, pid: str, raw) -> None:
        card = Vertical(classes="card")
        content.mount(card)

        # Status line at top — at-a-glance health
        icon, status_msg = self._provider_status(pid, raw)
        status_style = OK if icon == "✓" else (COPPER if icon == "▲" else TXT_DIM)

        title = Text()
        title.append(f"{icon} ", style=status_style)
        title.append(f"{pid}", style=f"bold {GOLD_HI}")
        title.append(f"   {raw.get('name', '')}", style=TXT_DIM)
        title.append(f"   {raw.get('format', 'openai_compat')}", style=BRONZE)
        if not bool(raw.get("enabled", True)):
            title.append("   [disabled]", style=COPPER)
        card.mount(_Static(title, classes="card-title"))

        status = Text()
        status.append("  ", style=status_style)
        status.append(status_msg, style=status_style)
        card.mount(_Static(status))

        card.mount(_Static(Text(f"  {raw.get('base_url', '')}", style=TXT_DIM)))

        # Per-provider max_tokens override. Empty / 0 = "use the agent
        # default". Anything else clamps the request when the mesh
        # routes through this provider.
        max_tok_raw = int(raw.get("max_tokens", 0) or 0)
        if max_tok_raw > 0:
            mt_line = Text()
            mt_line.append("  max_tokens: ", style=TXT_DIM)
            mt_line.append(f"{max_tok_raw:,}", style=GOLD)
            mt_line.append("  (override)", style=TXT_DIM)
            card.mount(_Static(mt_line))

        # keys (compact: show count only, full details in Manage Keys)
        keys = raw.get("keys") or {}
        # models summary
        models = raw.get("models") or {}
        if models:
            mtitle = Text()
            mtitle.append(f"  {len(models)} models · ", style=GOLD_DIM)
            mtitle.append(", ".join(list(models.keys())[:4]), style=TXT_DIM)
            if len(models) > 4:
                mtitle.append(f"  +{len(models)-4} more", style=TXT_DIM)
            card.mount(_Static(mtitle))

        actions = Horizontal(classes="card-actions")
        card.mount(actions)
        actions.mount(Button("+ Add key", id=f"prov-key-{pid}"))
        if keys:
            actions.mount(Button(f"Manage keys ({len(keys)})", id=f"prov-keys-{pid}"))
            actions.mount(Button(f"Models ({len(models)})", id=f"prov-models-{pid}"))
            actions.mount(Button("Refresh", id=f"prov-refresh-{pid}"))
        actions.mount(Button("Max tokens", id=f"prov-maxtok-{pid}"))
        if bool(raw.get("enabled", True)):
            actions.mount(Button("Disable", id=f"prov-disable-{pid}"))
        else:
            actions.mount(Button("Enable", id=f"prov-enable-{pid}", variant="primary"))
        # Allow removing custom providers entirely (not preset OAuth ones)
        if pid not in OAUTH_REGISTRY:
            actions.mount(Button("Remove", id=f"prov-remove-{pid}", variant="warning"))

    # ---- subscriptions ----

    def _render_subs(self, content: VerticalScroll) -> None:
        from .core.platform_paths import get_config_dir
        header = Vertical(classes="card")
        content.mount(header)
        header.mount(_Static(Text("Subscriptions (OAuth)", style=f"bold {GOLD_HI}"),
                            classes="card-title"))
        header.mount(_Static(Text(
            "Use a Claude Pro/Max or ChatGPT Plus/Pro account instead of an API key. "
            "Cogitum opens your browser, completes the flow, and stores tokens in "
            f"{get_config_dir() / 'auth.json'} (mode 0600 on POSIX).",
            style=TXT_DIM)))

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
        except Exception as e:
            settings = {}
            content.mount(_Static(Text(f"settings load failed: {e}", style=RUST)))
        cur = settings.get("default_model", "—")
        line = Text()
        line.append("current: ", style=GOLD_DIM)
        line.append(cur, style=GOLD_HI)
        header.mount(_Static(line))

        try:
            mesh = load_mesh()
        except Exception as e:
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
        from .gateway.tg_config import load_tg_config, TG_CONFIG_PATH
        from .gateway.daemon import status_service, NotSupportedOnPlatform

        cfg = load_tg_config()
        try:
            status = status_service()
        except NotSupportedOnPlatform as e:
            # Show a friendly card on Windows instead of crashing the
            # whole setup wizard. The token / user-id config is still
            # editable; the user just runs the gateway manually.
            card = Vertical(classes="card")
            content.mount(card)
            card.mount(_Static(Text("Telegram gateway", style=f"bold {GOLD_HI}"),
                              classes="card-title"))
            card.mount(_Static(Text(str(e), style=TXT_DIM)))
            return

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
            status_card.mount(_Static(Text("  ▲ Not configured", style=RUST)))

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

    # ---- themes ----

    def _render_themes(self, content: VerticalScroll) -> None:
        """Pick the visual theme. WH40K-canon presets only.

        Each preset is a card showing a small palette swatch (3 hex
        squares from the theme's accent / surface / text tokens),
        the lore blurb, and an Apply button. The currently active
        theme is highlighted; clicking Apply on a different one
        writes ``[experimental] theme = "..."`` to settings.toml
        and prompts for a Cogitum restart (CSS literals bake into
        ``App.CSS`` at class load — full restart is the cleanest
        way to swap them).
        """
        from .themes import (
            THEMES, THEME_DISPLAY_NAMES, THEME_BLURBS,
            get_active_theme_name,
        )

        active = get_active_theme_name()

        # Section header.
        header = Vertical(classes="card")
        content.mount(header)
        header.mount(_Static(Text("Themes", style=f"bold {GOLD_HI}"),
                            classes="card-title"))
        header.mount(_Static(Text(
            "Visual presets, all WH40K-canon. Pick the colourway that "
            "reads best on your terminal — gold can be too bright on "
            "some monitors, the green / khaki / steel themes are easier "
            "on the eyes while staying in-universe. Restart Cogitum "
            "after changing for the palette to fully apply.",
            style=TXT_DIM)))

        # One card per theme.
        for theme_id, palette in THEMES.items():
            card = Vertical(classes="card")
            content.mount(card)

            display = THEME_DISPLAY_NAMES.get(theme_id, theme_id)
            blurb = THEME_BLURBS.get(theme_id, "")
            is_active = theme_id == active

            # Title row: name + 3-swatch palette preview + active marker.
            title = Text()
            title.append(display, style=f"bold {GOLD_HI}")
            title.append("    ")
            # Three accent swatches: the primary highlight, the surface,
            # and the text colour. Painted as filled blocks via the
            # background style so the user sees the actual hue.
            title.append("  ", style=f"on {palette['GOLD_HI']}")
            title.append(" ")
            title.append("  ", style=f"on {palette['BG_SOFT']}")
            title.append(" ")
            title.append("  ", style=f"on {palette['TXT']}")
            title.append("    ")
            if is_active:
                title.append("● active", style=OK)
            card.mount(_Static(title, classes="card-title"))

            if blurb:
                card.mount(_Static(Text(blurb, style=TXT_DIM)))

            actions = Horizontal(classes="card-actions")
            card.mount(actions)
            if is_active:
                actions.mount(Button("Active", id=f"theme-noop-{theme_id}",
                                    disabled=True))
            else:
                actions.mount(Button("Apply", id=f"theme-apply-{theme_id}",
                                    variant="primary"))

    async def _apply_theme(self, theme_id: str) -> None:
        from .themes import write_active_theme, THEME_DISPLAY_NAMES
        try:
            write_active_theme(theme_id)
        except ValueError as e:
            await self.app.push_screen_wait(MessageModal(
                "Theme error", str(e), error=True))
            return
        await self.app.push_screen_wait(MessageModal(
            "Theme applied",
            f"Active theme set to {THEME_DISPLAY_NAMES.get(theme_id, theme_id)}.\n\n"
            "Restart Cogitum for the palette to fully apply (CSS literals "
            "bake at app load).",
        ))
        self._render_section()

    # ---- experimental ----

    def _render_experimental(self, content: VerticalScroll) -> None:
        """Toggles for opt-in experimental features.

        These are features still under active development. Toggling
        them writes to ``settings.toml`` under the ``[experimental]``
        table; reading is opt-in by feature so a missing key always
        means "off". A full Cogitum restart is required for any
        change to take effect — components that read the flag do so
        at startup, not per-request, to keep hot paths cheap.
        """
        from .core.llm.loader import load_settings
        settings = load_settings()
        exp = settings.get("experimental", {}) if isinstance(settings, dict) else {}

        # Section header card.
        header = Vertical(classes="card")
        content.mount(header)
        header.mount(_Static(Text("Experimental features", style=f"bold {GOLD_HI}"),
                            classes="card-title"))
        header.mount(_Static(Text(
            "Opt-in toggles for features under active development. They "
            "may change shape, break, or be removed without notice. "
            "Restart Cogitum after toggling for changes to take effect.",
            style=TXT_DIM)))

        # ── Cogitator Legion ──────────────────────────────────────
        legion_on = bool(exp.get("legion_enabled", False))
        legion_card = Vertical(classes="card")
        content.mount(legion_card)

        title = Text()
        title.append("⚔ Cogitator Legion", style=f"bold {GOLD_HI}")
        title.append("   ", style=TXT_DIM)
        if legion_on:
            title.append("● enabled", style=OK)
        else:
            title.append("○ disabled", style=TXT_DIM)
        legion_card.mount(_Static(title, classes="card-title"))

        legion_card.mount(_Static(Text(
            "Recursive 2-level swarm: lead Cogitum spawns up to 5 parallel "
            "Cogitators (L1); each may spawn up to 3 sub-Cogitators (L2). "
            "Replaces the single-shot delegate_task with async sibling "
            "messaging and a live tree view (click the LEGION card in the "
            "feed to open it).",
            style=TXT_DIM)))

        legion_card.mount(_Static(Text(
            "Status: works for simple parallel tasks but the tree view is "
            "rough, recovery on hard mesh failures is best-effort, and the "
            "model can occasionally over-delegate trivial work. Use with "
            "care on production tasks.",
            style=COPPER)))

        actions = Horizontal(classes="card-actions")
        legion_card.mount(actions)
        if legion_on:
            actions.mount(Button("Disable", id="exp-legion-off", variant="warning"))
        else:
            actions.mount(Button("Enable", id="exp-legion-on", variant="primary"))

    def _set_experimental_flag(self, key: str, value: bool) -> None:
        """Persist one ``[experimental]`` flag and prompt for restart.

        Reads settings.toml, sets ``experimental.<key>``, writes back.
        Then surfaces a MessageModal telling the user to restart so
        the new state takes effect (we deliberately don't try to
        hot-swap the legion tool — feature flags read at startup are
        much simpler to reason about than mid-session reconfig).
        """
        from .core.llm.loader import load_settings, write_settings
        settings = load_settings()
        if not isinstance(settings, dict):
            settings = {}
        exp = settings.get("experimental")
        if not isinstance(exp, dict):
            exp = {}
        exp[key] = bool(value)
        settings["experimental"] = exp
        write_settings(settings)

    # ---- diagnostics ----

    def _render_diag(self, content: VerticalScroll) -> None:
        card = Vertical(classes="card")
        content.mount(card)
        card.mount(_Static(Text("Diagnostics", style=f"bold {GOLD_HI}"),
                          classes="card-title"))
        try:
            mesh = load_mesh()
        except Exception as e:
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

        # MCP buttons (Add server / Edit / Test / Delete / Risk picker / etc.)
        if bid.startswith("prov-maxtok-"):
            pid = bid.removeprefix("prov-maxtok-")
            current = int((self._writer.provider(pid) or {}).get("max_tokens", 0) or 0)
            new_value = await self.app.push_screen_wait(MaxTokensModal(pid, current))
            if new_value is None:
                return  # cancelled
            self._writer.set_max_tokens(pid, int(new_value))
            self._writer.save()
            self._render_section()
            return

        if bid.startswith("theme-apply-"):
            theme_id = bid[len("theme-apply-"):]
            await self._apply_theme(theme_id)
            return

        if bid == "exp-legion-on" or bid == "exp-legion-off":
            self._set_experimental_flag("legion_enabled", bid == "exp-legion-on")
            new_state = "enabled" if bid == "exp-legion-on" else "disabled"
            await self.app.push_screen_wait(MessageModal(
                f"Legion {new_state}",
                "Restart Cogitum for the change to take effect.\n\n"
                "After restart you'll see the legion tool "
                f"{'available to' if bid == 'exp-legion-on' else 'hidden from'} "
                "the agent.",
            ))
            self._render_section()
            return

        if bid.startswith("mcp-"):
            from .setup_mcp import handle_mcp_button
            handled = await handle_mcp_button(self, bid)
            if handled:
                self._render_section()
            return

        if bid == "prov-add":
            existing = set(self._writer.providers().keys())
            picked = await self.app.push_screen_wait(AddProviderModal(existing))
            if picked is None:
                # User cancelled — do nothing
                return
            if picked == "custom":
                # User explicitly chose the "custom" slot — open the form
                custom_preset = await self.app.push_screen_wait(CustomProviderModal())
                if custom_preset is not None:
                    await self._add_provider_flow(custom_preset)
            else:
                # ProviderPreset chosen
                await self._add_provider_flow(picked)
            self._render_section()
            return

        if bid == "prov-edit":
            self._open_in_editor()
            return

        if bid.startswith("prov-key-"):
            pid = bid.removeprefix("prov-key-")
            await self._add_key_flow(pid)
            return

        if bid.startswith("prov-keys-"):
            pid = bid.removeprefix("prov-keys-")
            raw = self._writer.provider(pid)
            name = raw.get("name", pid) if raw else pid
            result = await self.app.push_screen_wait(KeyManagerModal(pid, name))
            if result == "add":
                # User clicked "Add new" — chain into add-key flow
                await self._add_key_flow(pid)
            # Always re-render to reflect any deletions
            self._render_section()
            return

        if bid.startswith("prov-remove-"):
            pid = bid.removeprefix("prov-remove-")
            ok = await self.app.push_screen_wait(ConfirmModal(
                "Remove provider",
                f"Delete provider '{pid}' and all its keys? This does NOT "
                f"remove the secrets themselves (env vars, vault, keyring) — "
                f"only the config entry.",
            ))
            if ok:
                self._writer.remove_provider(pid)
                self._writer.save()
                self.app.notify(f"Removed {pid}", severity="information")
                self._render_section()
            return

        if bid.startswith("prov-models-"):
            pid = bid.removeprefix("prov-models-")
            raw = self._writer.provider(pid)
            name = raw.get("name", pid) if raw else pid
            await self.app.push_screen_wait(ManageModelsModal(pid, name))
            self._render_section()
            return

        if bid.startswith("prov-refresh-"):
            pid = bid.removeprefix("prov-refresh-")
            self.app.notify(f"Discovering models for {pid}…", timeout=2)
            from cogitum.core.llm.discovery import discover_models, resolve_secret_ref
            raw = self._writer.provider(pid)
            if not raw:
                self.app.notify(f"Provider {pid} not found", severity="error")
                return

            # Backfill from preset first — adds curated models the user
            # might have lost or never had (e.g. cerebras llama3.1-8b)
            preset_added = 0
            preset = preset_by_id(pid)
            if preset and preset.models:
                existing = raw.get("models") or {}
                for m in preset.models:
                    if m.id in existing:
                        continue
                    self._writer.add_model(
                        pid, m.id,
                        display=m.display,
                        aliases=list(m.aliases),
                        capabilities=list(m.capabilities),
                        context_window=m.context_window,
                        max_output_tokens=m.max_output_tokens,
                    )
                    preset_added += 1
                if preset_added:
                    self._writer.save()
                    raw = self._writer.provider(pid)  # refresh local copy

            keys = raw.get("keys") or {}
            if not keys:
                if preset_added:
                    await self.app.push_screen_wait(MessageModal(
                        "Backfilled",
                        f"Added {preset_added} known {pid} models from preset.\n"
                        f"Add a key to enable live /v1/models discovery.",
                    ))
                    self._render_section()
                    return
                await self.app.push_screen_wait(MessageModal(
                    "No key", f"{pid} has no API key. Click '+ Add key' first.",
                    error=True,
                ))
                return
            secret_ref = next(iter(keys.values())).get("secret_ref", "")
            if secret_ref.startswith("oauth:") or raw.get("format") == "anthropic_native":
                msg = f"{pid} uses {('OAuth' if secret_ref.startswith('oauth:') else 'Anthropic')} which doesn't expose /v1/models.\n"
                if preset_added:
                    msg += f"Backfilled {preset_added} known models from preset."
                else:
                    msg += "Models are pre-seeded for this provider."
                await self.app.push_screen_wait(MessageModal("Cannot discover", msg))
                self._render_section()
                return
            try:
                api_key = resolve_secret_ref(secret_ref)
            except Exception as e:
                await self.app.push_screen_wait(MessageModal(
                    "Key error", f"Could not resolve {secret_ref}: {e}", error=True,
                ))
                return
            if not api_key:
                msg = f"{secret_ref} resolves to empty. Re-enter the key via 'Manage keys'."
                if preset_added:
                    msg = f"Backfilled {preset_added} known {pid} models from preset.\n\n" + msg
                await self.app.push_screen_wait(MessageModal(
                    "Key empty" if not preset_added else "Backfilled (key empty)",
                    msg,
                ))
                self._render_section()
                return
            base_url = raw.get("base_url", "")
            try:
                models = await discover_models(base_url, api_key, timeout=10.0)
            except Exception as e:
                msg = f"Endpoint: {base_url}\nError: {e}\n\nPossible causes: invalid key, wrong base_url, network down."
                if preset_added:
                    msg = f"Backfilled {preset_added} known models from preset.\n\nLive discovery still failed:\n{msg}"
                await self.app.push_screen_wait(MessageModal(
                    "Discovery failed" if not preset_added else "Backfilled (discovery failed)",
                    msg, error=not preset_added,
                ))
                self._render_section()
                return
            existing = self._writer.provider(pid).get("models") or {}
            added = 0
            for m in models:
                mid = m.get("model_id")
                if not mid or mid in existing:
                    continue
                self._writer.add_model(
                    pid, mid,
                    display=m.get("display", mid),
                    capabilities=m.get("capabilities", ["text", "tools"]),
                    context_window=m.get("context_window", 128000),
                    max_output_tokens=m.get("max_output_tokens", 16000),
                )
                added += 1
            self._writer.save()
            total = len(self._writer.provider(pid).get("models") or {})
            body_lines = []
            if preset_added:
                body_lines.append(f"Backfilled {preset_added} from preset.")
            body_lines.append(
                f"Live discovery: {len(models)} returned, {added} new added."
            )
            body_lines.append(f"Total models: {total}.")
            await self.app.push_screen_wait(MessageModal(
                "Refresh complete",
                "\n".join(body_lines),
            ))
            self._render_section()
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
        count, discovery_msg = await self._auto_discover_models(pid, result.secret_ref)

        # Build final message
        body_lines = [
            f"{pid} now uses secret_ref = {result.secret_ref}",
            "Provider enabled.",
            "",
        ]
        if count > 0:
            body_lines.append(f"✓ {discovery_msg}")
        else:
            body_lines.append(f"ℹ models: {discovery_msg}")
            if result.backend == "env":
                body_lines.append("  (if env var isn't exported yet, restart shell and reopen setup)")

        await self.app.push_screen_wait(MessageModal(
            "Key saved",
            "\n".join(body_lines),
        ))
        self._render_section()

    async def _auto_discover_models(self, pid: str, secret_ref: str) -> tuple[int, str]:
        """Try to discover models from /v1/models endpoint.

        Returns:
            (count, message) — count of models added, status message for the user.
        """
        from .core.llm.discovery import discover_models, resolve_secret_ref

        provider_data = self._writer.provider(pid)
        if not provider_data:
            return (0, "provider record missing")

        # Skip if provider already has models defined
        existing_models = provider_data.get("models", {})
        if existing_models and len(existing_models) > 0:
            return (0, f"provider already has {len(existing_models)} models — skipped discovery")

        base_url = provider_data.get("base_url", "")
        if not base_url:
            return (0, "no base_url — cannot discover")

        # Resolve the key
        try:
            api_key = resolve_secret_ref(secret_ref)
        except Exception as e:
            return (0, f"could not resolve key ({secret_ref}): {e}")
        if not api_key:
            return (0, "key not yet available (env var not exported?)")

        try:
            models = await discover_models(base_url, api_key)
        except Exception as e:
            return (0, f"discovery request failed: {e}")

        if not models:
            return (0, "endpoint returned no models — add manually via providers.toml")

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
        return (len(models), f"discovered {len(models)} models")

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
        import os
        import shutil
        import subprocess
        import sys

        # Per-platform editor candidates. On Windows, $EDITOR is rarely
        # set — fall back to notepad which always exists. On POSIX, try
        # nvim → vim → nano in order.
        ed = os.environ.get("EDITOR")
        if not ed:
            if sys.platform == "win32":
                ed = (
                    shutil.which("nvim")
                    or shutil.which("code")    # VS Code in PATH (-w to wait)
                    or shutil.which("notepad")
                    or "notepad.exe"
                )
            else:
                ed = (
                    shutil.which("nvim")
                    or shutil.which("vim")
                    or shutil.which("nano")
                )
        if not ed:
            self.app.push_screen(MessageModal(
                "No editor", "$EDITOR not set; open the file from your shell.", error=True
            ))
            return
        # VS Code needs -w/--wait to block until the file is closed.
        cmd = [ed, str(_PROVIDERS_PATH)]
        if ed.endswith("code") or ed.endswith("code.exe") or ed.endswith("code.cmd"):
            cmd = [ed, "-w", str(_PROVIDERS_PATH)]
        # We need to suspend the textual app briefly.
        with self.app.suspend():
            subprocess.call(cmd, timeout=3600)
        self._writer = ConfigWriter()
        self._render_section()


__all__ = [
    "SetupScreen",
    "AddProviderModal",
    "CustomProviderModal",
    "KeyEntryModal",
    "KeyManagerModal",
    "OAuthLoginModal",
    "MessageModal",
    "ConfirmModal",
]


# silence unused
_: Callable[[str], Awaitable[None]] | None = None
_ = _SETTINGS_PATH
