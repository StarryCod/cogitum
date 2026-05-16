"""
Cogitum CLI.

Subcommands:
  cog                 — launch the TUI (default)
  cog setup           — interactive setup (providers, models, OAuth)
  cog models          — list models in the mesh
  cog auth login <id> — OAuth login for a subscription provider
  cog auth list       — list stored OAuth credentials
  cog auth logout <id>
  cog vault init / set / get / unset / list
  cog providers path  — print path to providers.toml

Run via the `cog` script entry, or `python -m cogitum`.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
import webbrowser
from pathlib import Path
from typing import Any

from .core.auth import storage as auth_storage
from .core.auth.registry import REGISTRY as OAUTH_REGISTRY, get_provider
from .core.auth.types import OAuthAuthInfo, OAuthPrompt
from .core.llm.credentials import CredentialResolver, default_resolver
from .core.llm.loader import (
    _PROVIDERS_PATH,
    _SETTINGS_PATH,
    load_mesh,
    load_settings,
    seed_default_config,
)


logger = logging.getLogger("cogitum.cli")


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------

def _setup_command(args: argparse.Namespace) -> int:
    """Launch the Textual setup wizard. Falls back to headless mode with --tty."""
    if getattr(args, "tty", False):
        return _setup_tty(args)
    seed_default_config(_PROVIDERS_PATH)
    from .app import CogitumApp
    from .setup_flow import SetupScreen

    class _SetupOnly(CogitumApp):
        def on_mount(self) -> None:  # type: ignore[override]
            self.push_screen(SetupScreen())

    _SetupOnly().run()
    return 0


def _setup_tty(args: argparse.Namespace) -> int:
    """Headless setup over plain stdin/stdout (legacy)."""
    print()
    _hr()
    print("  COGITUM — setup")
    _hr()
    print()

    seed_default_config(_PROVIDERS_PATH)
    print(f"  Config:   {_PROVIDERS_PATH}")
    print(f"  Settings: {_SETTINGS_PATH}")
    print()

    while True:
        print("  Select a step:")
        print("    1) Configure API providers (OpenAI, Anthropic, OpenRouter, …)")
        print("    2) Connect a subscription (Claude Pro/Max, ChatGPT Plus/Pro)")
        print("    3) Pick default model")
        print("    4) Show current mesh status")
        print("    5) Open providers.toml in $EDITOR")
        print("    q) Done")
        print()
        choice = input("  > ").strip().lower()
        print()
        if choice in ("q", "quit", "exit", ""):
            print("  Setup complete. Run `cog` to launch the TUI.")
            return 0
        if choice == "1":
            _setup_api_keys()
        elif choice == "2":
            asyncio.run(_setup_oauth())
        elif choice == "3":
            _setup_default_model()
        elif choice == "4":
            _print_mesh_status()
        elif choice == "5":
            _open_in_editor(_PROVIDERS_PATH)
        else:
            print("  ?  unknown choice")
        print()


def _setup_api_keys() -> None:
    print("  Which provider's API key do you want to set?")
    print("    a) OpenAI            (env:OPENAI_API_KEY)")
    print("    b) Anthropic         (env:ANTHROPIC_API_KEY)")
    print("    c) OpenRouter        (env:OPENROUTER_API_KEY)")
    print("    d) CanopyWave        (already configured with bundled key)")
    print("    e) Custom — paste a provider TOML block manually")
    print()
    pick = input("  > ").strip().lower()
    print()

    targets = {
        "a": ("openai", "OPENAI_API_KEY"),
        "b": ("anthropic", "ANTHROPIC_API_KEY"),
        "c": ("openrouter", "OPENROUTER_API_KEY"),
    }
    if pick in targets:
        provider_id, env_name = targets[pick]
        key = getpass.getpass(f"  Paste {provider_id} key (hidden): ").strip()
        if not key:
            print("  empty — skipped")
            return
        backend = input("  Storage [keyring/env-shell/plain] (default: keyring): ").strip().lower() or "keyring"
        if backend == "keyring":
            try:
                import keyring
                keyring.set_password("cogitum", env_name, key)
                print(f"  ✓ stored in system keyring as cogitum / {env_name}")
                print(f"    To use it, add to ~/.config/cogitum/providers.toml:")
                print(f'    secret_ref = "keyring:cogitum:{env_name}"')
            except Exception as e:  # noqa: BLE001
                print(f"  keyring failed: {e}")
                print("  Falling back to plain in providers.toml (NOT recommended).")
                _patch_provider_secret(provider_id, f"plain:{key}")
        elif backend == "env-shell":
            print(f"  Add to ~/.bashrc or ~/.zshrc:")
            print(f"    export {env_name}={key!r}")
            print(f"  providers.toml already references env:{env_name} for {provider_id}.")
        else:
            _patch_provider_secret(provider_id, f"plain:{key}")
            print(f"  ✓ written to providers.toml (plain — rotate later)")

        # Auto-enable the provider entry.
        _set_provider_enabled(provider_id, True)
    elif pick == "d":
        print("  CanopyWave already wired with the imported key.")
    elif pick == "e":
        print(f"  Open {_PROVIDERS_PATH} and append a [providers.<id>] block.")
        _open_in_editor(_PROVIDERS_PATH)
    else:
        print("  ?  unknown choice")


async def _setup_oauth() -> None:
    print("  Subscriptions:")
    for i, (pid, prov) in enumerate(OAUTH_REGISTRY.items(), start=1):
        existing = auth_storage.get(pid)
        marker = "  ✓ logged in" if existing else ""
        print(f"    {i}) {prov.name}{marker}")
    print()
    pick = input("  > ").strip()
    if not pick.isdigit():
        return
    idx = int(pick) - 1
    items = list(OAUTH_REGISTRY.items())
    if idx < 0 or idx >= len(items):
        return
    pid, prov = items[idx]

    async def on_auth(info: OAuthAuthInfo) -> None:
        print()
        print("  Open this URL in your browser (we will also try to launch it):")
        print(f"    {info.url}")
        if info.instructions:
            print(f"  {info.instructions}")
        print()
        try:
            webbrowser.open(info.url)
        except Exception:  # noqa: BLE001
            pass

    async def on_prompt(p: OAuthPrompt) -> str:
        return await asyncio.to_thread(input, f"  {p.message}\n  > ")

    async def on_progress(msg: str) -> None:
        print(f"  …{msg}")

    try:
        creds = await prov.login(on_auth=on_auth, on_prompt=on_prompt, on_progress=on_progress)
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ login failed: {e}")
        return

    auth_storage.set_(pid, creds)
    print(f"  ✓ {prov.name} authenticated. Tokens stored in ~/.config/cogitum/auth.json")

    # Auto-enable the matching provider entry.
    if pid == "anthropic":
        _set_provider_enabled("anthropic-pro", True)


def _setup_default_model() -> None:
    try:
        mesh = load_mesh()
    except Exception as e:  # noqa: BLE001
        print(f"  cannot load mesh: {e}")
        return

    pairs = mesh.list_resolved()
    if not pairs:
        print("  No models available. Configure a provider first.")
        return
    print("  Available models:")
    for i, r in enumerate(pairs, start=1):
        print(f"    {i:>2}) {r.qualified_id:<48} {r.model.display}")
    print()
    pick = input("  Default model number > ").strip()
    if not pick.isdigit():
        return
    idx = int(pick) - 1
    if idx < 0 or idx >= len(pairs):
        return
    chosen = pairs[idx]
    settings = load_settings()
    settings["default_model"] = chosen.qualified_id
    from .core.llm.loader import write_settings
    write_settings(settings)
    print(f"  ✓ default = {chosen.qualified_id}")


# ---------------------------------------------------------------------------
# Mesh status
# ---------------------------------------------------------------------------

def _print_mesh_status() -> None:
    mesh = load_mesh()
    if not mesh.providers:
        print("  No providers active.")
        return
    for p in mesh.providers.values():
        print(f"  {p.id} ({p.name}) — {p.config.format} — {p.config.base_url}")
        snap = p.pool.snapshot()
        if not snap:
            print("    (no keys)")
        for s in snap:
            print(
                f"    · key={s['id']:<14} status={s['status']:<14}"
                f" rpm={s['rpm']:<3} req={s['total_requests']:<5}"
                f" tok={s['total_tokens']}"
            )
        for m in p.list_models():
            print(f"    · model {m.id:<40} ctx={m.context_window} out={m.max_output_tokens}")
    print()
    print(f"  Active OAuth: {auth_storage.list_providers() or '(none)'}")


# ---------------------------------------------------------------------------
# providers.toml editing helpers
# ---------------------------------------------------------------------------

def _read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _patch_provider_secret(provider_id: str, secret_ref: str) -> None:
    """Replace the first `secret_ref = "..."` under a provider's `keys` block."""
    text = _read_text(_PROVIDERS_PATH)
    needle = f"[providers.{provider_id}.keys."
    idx = text.find(needle)
    if idx < 0:
        print(f"  ! no [providers.{provider_id}.keys.*] block to patch")
        return
    section_start = idx
    next_section = text.find("\n[", section_start + 1)
    end = next_section if next_section >= 0 else len(text)
    section = text[section_start:end]
    new_section = _replace_value(section, "secret_ref", secret_ref)
    text = text[:section_start] + new_section + text[end:]
    _PROVIDERS_PATH.write_text(text, encoding="utf-8")


def _set_provider_enabled(provider_id: str, enabled: bool) -> None:
    text = _read_text(_PROVIDERS_PATH)
    header = f'[providers."{provider_id}"]' if "-" in provider_id else f"[providers.{provider_id}]"
    idx = text.find(header)
    if idx < 0:
        return
    next_section = text.find("\n[", idx + 1)
    end = next_section if next_section >= 0 else len(text)
    section = text[idx:end]
    if "enabled =" in section:
        new_section = _replace_value(section, "enabled", "true" if enabled else "false", quote=False)
    else:
        # insert after header
        nl = section.find("\n")
        new_section = section[:nl + 1] + f'enabled = {"true" if enabled else "false"}\n' + section[nl + 1:]
    text = text[:idx] + new_section + text[end:]
    _PROVIDERS_PATH.write_text(text, encoding="utf-8")


def _replace_value(section: str, key: str, new_value: str, *, quote: bool = True) -> str:
    import re
    pattern = re.compile(rf'^(\s*{re.escape(key)}\s*=\s*).*$', re.MULTILINE)
    rendered = f'"{new_value}"' if quote else new_value
    return pattern.sub(lambda m: m.group(1) + rendered, section, count=1)


def _open_in_editor(path: Path) -> None:
    import os
    import shutil
    import subprocess
    editor = os.environ.get("EDITOR") or shutil.which("nvim") or shutil.which("vim") or shutil.which("nano")
    if not editor:
        print(f"  no $EDITOR; open {path} manually.")
        return
    subprocess.call([editor, str(path)])


def _hr() -> None:
    print("  " + "─" * 60)


# ---------------------------------------------------------------------------
# Other subcommands
# ---------------------------------------------------------------------------

def _models_command(args: argparse.Namespace) -> int:
    mesh = load_mesh()
    pairs = mesh.list_resolved()
    if not pairs:
        print("  No models available. Run `cog setup`.")
        return 1
    for r in pairs:
        caps = ", ".join(r.model.capabilities.to_strings())
        print(
            f"  {r.qualified_id:<60} {r.model.display:<22}"
            f"  ctx={r.model.context_window:<6} out={r.model.max_output_tokens:<6}"
            f"  [{caps}]"
        )
    return 0


def _auth_command(args: argparse.Namespace) -> int:
    if args.auth_action == "list":
        for pid in auth_storage.list_providers():
            creds = auth_storage.get(pid)
            ttl = (creds.expires - __import__("time").time()) if creds else 0
            print(f"  {pid}  expires_in={ttl/60:.1f}m")
        return 0
    if args.auth_action == "logout":
        ok = auth_storage.remove(args.provider)
        print("  ✓ removed" if ok else "  not found")
        return 0
    if args.auth_action == "login":
        prov = get_provider(args.provider)
        if not prov:
            print(f"  unknown provider: {args.provider}")
            print(f"  known: {list(OAUTH_REGISTRY)}")
            return 1
        async def run():
            async def on_auth(info: OAuthAuthInfo) -> None:
                print(f"\n  Open: {info.url}\n  {info.instructions}\n")
                try:
                    webbrowser.open(info.url)
                except Exception:  # noqa: BLE001
                    pass
            async def on_prompt(p: OAuthPrompt) -> str:
                return await asyncio.to_thread(input, f"  {p.message}\n  > ")
            async def on_progress(msg: str) -> None:
                print(f"  …{msg}")
            return await prov.login(on_auth=on_auth, on_prompt=on_prompt, on_progress=on_progress)
        try:
            creds = asyncio.run(run())
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {e}")
            return 1
        auth_storage.set_(args.provider, creds)
        print(f"  ✓ logged in as {args.provider}")
        return 0
    print("  unknown auth action")
    return 1


def _vault_command(args: argparse.Namespace) -> int:
    resolver = default_resolver()
    if args.vault_action == "init":
        password = getpass.getpass("  Vault password: ")
        confirm = getpass.getpass("  Confirm: ")
        if password != confirm or not password:
            print("  ✗ password mismatch / empty")
            return 1
        resolver.vault_init(password)
        print(f"  ✓ vault initialized at {resolver.vault_path}")
        return 0
    if args.vault_action == "set":
        value = getpass.getpass(f"  Value for {args.key} (hidden): ")
        resolver.vault_set(args.key, value)
        print("  ✓")
        return 0
    if args.vault_action == "get":
        value = resolver._resolve_vault(args.key)
        print(value)
        return 0
    if args.vault_action == "unset":
        resolver.vault_unset(args.key)
        print("  ✓")
        return 0
    if args.vault_action == "list":
        for k in resolver.vault_keys():
            print(f"  {k}")
        return 0
    return 1


def _providers_command(args: argparse.Namespace) -> int:
    if args.providers_action == "path":
        print(_PROVIDERS_PATH)
        return 0
    if args.providers_action == "edit":
        _open_in_editor(_PROVIDERS_PATH)
        return 0
    return 1


# ---------------------------------------------------------------------------
# Telegram gateway
# ---------------------------------------------------------------------------

def _secret_command(args: argparse.Namespace) -> int:
    """Manage persistent secrets in ~/.config/cogitum/secrets.env."""
    from cogitum.core.llm.secrets_env import (
        SECRETS_PATH,
        list_secrets,
        remove_secret,
        save_secret,
    )

    action = args.secret_action

    if action == "path":
        print(SECRETS_PATH)
        return 0

    if action == "list":
        items = list_secrets()
        if not items:
            print("(no secrets stored)")
            return 0
        width = max(len(k) for k in items)
        for name, masked in items.items():
            print(f"  {name:<{width}}  {masked}")
        return 0

    if action == "set":
        value = args.value
        if value is None:
            # Read from stdin if piped, else prompt
            if not sys.stdin.isatty():
                value = sys.stdin.read().strip()
            else:
                import getpass
                value = getpass.getpass(f"{args.name}=")
        if not value:
            print("error: empty value", file=sys.stderr)
            return 1
        save_secret(args.name, value)
        print(f"✓ saved {args.name} to {SECRETS_PATH}")
        return 0

    if action == "unset":
        ok = remove_secret(args.name)
        if ok:
            print(f"✓ removed {args.name}")
            return 0
        print(f"(not present: {args.name})")
        return 1

    return 1


def _tg_command(args: argparse.Namespace) -> int:
    from .gateway.tg_config import load_tg_config, save_tg_config, TelegramConfig, TG_CONFIG_PATH
    from .gateway.daemon import (
        start_service, stop_service, restart_service,
        status_service, enable_service, disable_service,
    )

    action = args.tg_action

    if action == "setup":
        print()
        _hr()
        print("  COGITUM — Telegram Gateway Setup")
        _hr()
        print()
        cfg = load_tg_config()
        print(f"  Config: {TG_CONFIG_PATH}")
        if cfg.bot_token:
            print(f"  Current token: {cfg.bot_token[:8]}...{cfg.bot_token[-4:]}")
        if cfg.allowed_user_id:
            print(f"  Current user ID: {cfg.allowed_user_id}")
        print()

        token = input("  Bot token (from @BotFather): ").strip()
        if not token:
            if cfg.bot_token:
                print("  (keeping existing token)")
                token = cfg.bot_token
            else:
                print("  ✗ Token required.")
                return 1

        user_id_str = input("  Your Telegram user ID: ").strip()
        if not user_id_str:
            if cfg.allowed_user_id:
                print(f"  (keeping existing: {cfg.allowed_user_id})")
                user_id = cfg.allowed_user_id
            else:
                print("  ✗ User ID required. Send /start to @userinfobot to get it.")
                return 1
        else:
            try:
                user_id = int(user_id_str)
            except ValueError:
                print("  ✗ User ID must be a number.")
                return 1

        # Test connection
        print()
        print("  Testing connection...")
        import httpx
        try:
            resp = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
            data = resp.json()
            if data.get("ok"):
                bot_name = data["result"].get("username", "?")
                print(f"  ✓ Connected to @{bot_name}")
            else:
                print(f"  ✗ API error: {data.get('description')}")
                return 1
        except Exception as e:
            print(f"  ✗ Connection failed: {e}")
            return 1

        # Save config
        cfg = TelegramConfig(
            bot_token=token,
            allowed_user_id=user_id,
            enabled=True,
            show_thinking=True,
            show_tool_calls=True,
            default_model=cfg.default_model,
        )
        save_tg_config(cfg)
        print()
        print(f"  ✓ Config saved to {TG_CONFIG_PATH}")
        print()

        # Offer to start
        start = input("  Start the daemon now? [Y/n] ").strip().lower()
        if start in ("", "y", "yes"):
            result = enable_service()
            print(f"  {result}")
            result = start_service()
            print(f"  {result}")
        print()
        print("  Done! Send a message to your bot.")
        return 0

    elif action == "start":
        cfg = load_tg_config()
        if not cfg.is_valid():
            print("  Not configured. Run `cog tg setup` first.")
            return 1
        print(f"  {start_service()}")
        return 0

    elif action == "stop":
        print(f"  {stop_service()}")
        return 0

    elif action == "restart":
        cfg = load_tg_config()
        if not cfg.is_valid():
            print("  Not configured. Run `cog tg setup` first.")
            return 1
        print(f"  {restart_service()}")
        return 0

    elif action == "status":
        status = status_service()
        print(f"  Active:  {status['active']}")
        print(f"  Enabled: {status['enabled']}")
        print(f"  Service: {status['service_path']}")
        cfg = load_tg_config()
        if cfg.is_valid():
            print(f"  Token:   {cfg.bot_token[:8]}...{cfg.bot_token[-4:]}")
            print(f"  User ID: {cfg.allowed_user_id}")
        else:
            print("  Config:  NOT CONFIGURED")
        return 0

    elif action == "enable":
        print(f"  {enable_service()}")
        return 0

    elif action == "disable":
        print(f"  {disable_service()}")
        return 0

    elif action == "run":
        # Run in foreground (for debugging)
        cfg = load_tg_config()
        if not cfg.is_valid():
            print("  Not configured. Run `cog tg setup` first.")
            return 1
        print("  Running in foreground (Ctrl+C to stop)...")
        from .gateway.telegram import run_bot
        asyncio.run(run_bot(cfg))
        return 0

    print(f"  Unknown action: {action}")
    return 1


# ---------------------------------------------------------------------------
# TUI launcher (default)
# ---------------------------------------------------------------------------

def _tui_command(args: argparse.Namespace) -> int:
    from .app import CogitumApp
    CogitumApp().run()
    return 0


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cog", description="Cogitum — sovereign agentic CLI")
    sub = p.add_subparsers(dest="command")

    setup_p = sub.add_parser("setup", help="interactive provider/auth wizard (TUI)")
    setup_p.add_argument("--tty", action="store_true",
                         help="use plain stdin/stdout instead of the TUI wizard")
    setup_p.set_defaults(func=_setup_command)
    sub.add_parser("models", help="list models").set_defaults(func=_models_command)

    auth = sub.add_parser("auth", help="manage OAuth subscriptions")
    auth_sub = auth.add_subparsers(dest="auth_action", required=True)
    auth_login = auth_sub.add_parser("login")
    auth_login.add_argument("provider", help="anthropic | openai-codex")
    auth_logout = auth_sub.add_parser("logout")
    auth_logout.add_argument("provider")
    auth_sub.add_parser("list")
    auth.set_defaults(func=_auth_command)

    vault = sub.add_parser("vault", help="manage encrypted credential vault")
    vault_sub = vault.add_subparsers(dest="vault_action", required=True)
    vault_sub.add_parser("init")
    vset = vault_sub.add_parser("set"); vset.add_argument("key")
    vget = vault_sub.add_parser("get"); vget.add_argument("key")
    vunset = vault_sub.add_parser("unset"); vunset.add_argument("key")
    vault_sub.add_parser("list")
    vault.set_defaults(func=_vault_command)

    pp = sub.add_parser("providers", help="providers.toml utilities")
    pp_sub = pp.add_subparsers(dest="providers_action", required=True)
    pp_sub.add_parser("path")
    pp_sub.add_parser("edit")
    pp.set_defaults(func=_providers_command)

    # Telegram gateway
    tg = sub.add_parser("tg", help="Telegram gateway daemon")
    tg_sub = tg.add_subparsers(dest="tg_action", required=True)
    tg_sub.add_parser("start", help="start the gateway daemon")
    tg_sub.add_parser("stop", help="stop the gateway daemon")
    tg_sub.add_parser("restart", help="restart the gateway daemon")
    tg_sub.add_parser("status", help="show daemon status")
    tg_sub.add_parser("setup", help="configure bot token and user ID")
    tg_sub.add_parser("enable", help="enable auto-start on login")
    tg_sub.add_parser("disable", help="disable auto-start")
    tg_sub.add_parser("run", help="run in foreground (for debugging)")
    tg.set_defaults(func=_tg_command)

    # secret store (~/.config/cogitum/secrets.env)
    sec = sub.add_parser("secret", help="manage persistent API key store (secrets.env)")
    sec_sub = sec.add_subparsers(dest="secret_action", required=True)
    s_set = sec_sub.add_parser("set", help="store a secret (prompts for value)")
    s_set.add_argument("name", help="env var name, e.g. CEREBRAS_API_KEY")
    s_set.add_argument("value", nargs="?", default=None,
                       help="value (omit to read from stdin / prompt)")
    s_unset = sec_sub.add_parser("unset", help="remove a secret")
    s_unset.add_argument("name")
    sec_sub.add_parser("list", help="list stored secret names (values masked)")
    sec_sub.add_parser("path", help="print the secrets.env file path")
    sec.set_defaults(func=_secret_command)

    return p


def main(argv: list[str] | None = None) -> int:
    # Load persisted secrets from ~/.config/cogitum/secrets.env BEFORE
    # any provider/credential resolution. Real-environment variables win
    # by default (override=False), so user's .bashrc keys still take priority.
    try:
        from cogitum.core.llm.secrets_env import load_secrets_into_environ
        load_secrets_into_environ(override=False)
    except Exception:
        pass

    # Configure logging to file (not stderr) to avoid breaking TUI rendering
    log_dir = Path(
        os.environ.get("COGITUM_CONFIG_DIR")
        or os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    ) / "cogitum"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        filename=str(log_dir / "cogitum.log"),
        filemode="a",
    )
    # Suppress UserWarnings in TUI mode (they go to stderr and break rendering)
    import warnings
    warnings.filterwarnings("ignore", category=UserWarning)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        return _tui_command(args)
    func = getattr(args, "func", None)
    if not callable(func):
        parser.print_help()
        return 1
    return func(args)


if __name__ == "__main__":
    sys.exit(main())


# silence unused
_ = json
_ = Any
