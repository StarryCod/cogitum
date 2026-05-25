"""
cogitum.setup_mcp
~~~~~~~~~~~~~~~~~

MCP servers section of the setup wizard.

Renders into the same Imperial-Fists card layout as the rest of the wizard.

UI flow:
  Setup → MCP servers
    ┌── card: header (count, default risk, sampling status) ─────────────┐
    │ + Add server   ⟳ Reload   path: ~/.config/cogitum/mcp.toml          │
    └────────────────────────────────────────────────────────────────────┘
    ┌── card per server ─────────────────────────────────────────────────┐
    │ ⬢ <name>  [stdio|http]  [connected|failed|disconnected] · N tools  │
    │     target line                                                    │
    │     env / headers (keys only)                                      │
    │     [ Edit ] [ Delete ] [ Test ] [ Disable ]                       │
    │                                                                    │
    │     tools (if connected):                                          │
    │     • <tool>           [low | medium | danger ]   (clickable cycle)│
    └────────────────────────────────────────────────────────────────────┘

Buttons emit `Button.Pressed` events whose ids are routed in
`MCPSetupHandler.handle_button` — wired into `SetupScreen._on_button`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from .core.mcp.config import (
    MCPConfig,
    MCPServerConfig,
    VALID_RISKS,
    config_path,
    load_config,
    save_config,
)
from .design import (
    BRONZE,
    COPPER,
    GOLD,
    GOLD_DIM,
    GOLD_HI,
    OK,
    RUST,
    TXT,
    TXT_DIM,
    BG_SOFT,
    LABEL,
    FORM_HELP,
)


log = logging.getLogger(__name__)


# Risk → glyph + tone
_RISK_STYLE = {
    "low":    (OK,    "▪"),
    "medium": (BRONZE, "◆"),
    "danger": (RUST,  "◉"),
}


# Background tasks spawned by the setup wizard (currently the "Test"
# button on an MCP server). asyncio only holds a weak reference to the
# task returned by ``create_task``, so an unstored fire-and-forget task
# can be GC'd mid-flight, producing a "Task was destroyed but it is
# pending!" warning. We hold a strong reference here and clear each
# task via ``add_done_callback`` once it completes. Same pattern used
# by ``gateway/telegram.py`` for ``shutdown_tasks``.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


_STATE_STYLE = {
    "connected":    (OK,    "●"),
    "connecting":   (BRONZE, "○"),
    "failed":       (RUST,  "✕"),
    "disconnected": (TXT_DIM, "·"),
    "unknown":      (TXT_DIM, "?"),
}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_mcp_section(content: VerticalScroll) -> None:
    """
    Render the MCP servers section of the setup wizard into ``content``.

    Called from SetupScreen._render_section when the active tab is "mcp".
    """
    cfg = load_config()
    statuses = _live_status_map()

    # ── Header card ──────────────────────────────────────────────────────
    header = Vertical(classes="card")
    content.mount(header)
    header.mount(_Static(Text("MCP Servers", style=f"bold {GOLD_HI}"),
                         classes="card-title"))
    header.mount(_Static(Text(
        "Plug any MCP server into Cogitum: filesystem, GitHub, Postgres, "
        "your own. Tools become regular agent tools (mcp_<server>_<tool>) "
        "with per-tool risk you control.",
        style=TXT_DIM)))
    info = Text()
    info.append(f"  {len(cfg.servers)} configured · ", style=TXT_DIM)
    info.append(f"default risk: ", style=TXT_DIM)
    info.append(cfg.default_risk, style=GOLD)
    info.append("  ·  sampling: ", style=TXT_DIM)
    info.append("on" if cfg.default_sampling.enabled else "off",
                style=OK if cfg.default_sampling.enabled else TXT_DIM)
    info.append(f"\n  config: {config_path()}", style=GOLD_DIM)
    header.mount(_Static(info))

    actions = Horizontal(classes="card-actions")
    header.mount(actions)
    actions.mount(Button("+ Add server", id="mcp-add", variant="primary"))
    actions.mount(Button("⟳ Reload", id="mcp-reload"))

    if not cfg.servers:
        empty = Vertical(classes="card")
        content.mount(empty)
        empty.mount(_Static(Text("No servers yet.", style=COPPER),
                            classes="card-title"))
        empty.mount(_Static(Text(
            "Add one above. Examples that work out of the box:\n"
            "  • time          stdio · uvx mcp-server-time\n"
            "  • filesystem    stdio · npx -y @modelcontextprotocol/server-filesystem <path>\n"
            "  • github        stdio · npx -y @modelcontextprotocol/server-github\n"
            "  • your own      stdio · /path/to/your-mcp-binary  or  http url",
            style=TXT_DIM)))
        return

    # ── One card per server ──────────────────────────────────────────────
    for name, srv in cfg.servers.items():
        card = Vertical(classes="card mcp-card")
        content.mount(card)

        st = statuses.get(name, {})
        state = st.get("state", "disconnected" if srv.enabled else "unknown")
        tools = list(st.get("tools", []) or [])
        state_color, state_glyph = _STATE_STYLE.get(state, _STATE_STYLE["unknown"])

        # Title row
        title = Text()
        title.append("⬢ ", style=GOLD)
        title.append(name, style=f"bold {GOLD_HI}")
        title.append(f"  {srv.transport}", style=TXT_DIM)
        title.append(f"  {state_glyph} {state}", style=state_color)
        if tools:
            title.append(f"  · {len(tools)} tools", style=TXT_DIM)
        if not srv.enabled:
            title.append("  · disabled", style=COPPER)
        card.mount(_Static(title, classes="card-title"))

        # Target line (command or URL)
        target = Text()
        if srv.transport == "stdio":
            target.append("    ", style=TXT_DIM)
            target.append(srv.command or "", style=GOLD)
            if srv.args:
                target.append(" " + " ".join(srv.args), style=TXT_DIM)
        elif srv.transport == "http":
            target.append("    ", style=TXT_DIM)
            target.append(srv.url or "", style=GOLD)
        card.mount(_Static(target))

        # Env / headers (keys only — never values)
        if srv.env:
            keys = ", ".join(sorted(srv.env.keys()))
            card.mount(_Static(Text(f"    env: {keys}", style=TXT_DIM)))
        if srv.headers:
            keys = ", ".join(sorted(srv.headers.keys()))
            card.mount(_Static(Text(f"    headers: {keys}", style=TXT_DIM)))

        # Last error if failed
        if state == "failed" and st.get("last_error"):
            card.mount(_Static(Text(f"    error: {st['last_error']}",
                                    style=RUST)))

        # Action buttons
        btns = Horizontal(classes="card-actions")
        card.mount(btns)
        btns.mount(Button("Edit", id=f"mcp-edit-{name}"))
        btns.mount(Button("Test", id=f"mcp-test-{name}"))
        if srv.enabled:
            btns.mount(Button("Disable", id=f"mcp-disable-{name}"))
        else:
            btns.mount(Button("Enable", id=f"mcp-enable-{name}"))
        btns.mount(Button("Delete", id=f"mcp-del-{name}", variant="error"))

        # Tools list with per-tool risk pickers
        if tools:
            card.mount(_Static(Text("    Tools:", style=GOLD_DIM)))
            for tool in tools:
                cur = srv.risks.get(tool, cfg.default_risk)
                # Build a single-line clickable row:
                #   • tool_name        low ◆medium  danger
                # Active level is shown bold in its colour, inactive levels
                # dimmed. Whole row is one line.
                row = _RiskRow(server=name, tool=tool, current=cur)
                card.mount(row)
        elif state == "connected":
            card.mount(_Static(Text("    (no tools exposed)", style=TXT_DIM)))


def _live_status_map() -> dict:
    try:
        from .core.mcp import mcp_status
        return {s["name"]: s for s in mcp_status()}
    except Exception:
        return {}


# Tiny shim so we don't need to import _Static from setup_flow (circular).
class _Static(Static):
    DEFAULT_CSS = ""


class _RiskRow(Static):
    """
    One-line clickable row: ``• tool_name        ▪ low  ◆ medium  ◉ danger``.

    Active level shown bold in its risk colour, inactive levels dimmed.
    Clicking on a level emits a synthetic Button.Pressed with the same
    id format the legacy buttons used (``mcp-risk-{server}-{tool}-{level}``)
    so the existing dispatcher in ``handle_mcp_button`` Just Works.
    """

    DEFAULT_CSS = """
    _RiskRow {
        height: 1;
        padding: 0 0 0 4;
        background: transparent;
    }
    """

    def __init__(self, *, server: str, tool: str, current: str, **kw) -> None:
        self._server = server
        self._tool = tool
        self._current = current
        super().__init__(self._render_text(), **kw)

    def _render_text(self) -> Text:
        """Build the row as a Rich Text object (no markup-string parsing
        — works on Textual 6.x where Static no longer auto-parses markup)."""
        out = Text()
        out.append("• ", style=GOLD_DIM)
        out.append(self._tool, style=TXT)
        out.append("   ")
        for level in VALID_RISKS:
            color, glyph = _RISK_STYLE[level]
            if level == self._current:
                out.append(f" {glyph} {level}", style=f"bold {color}")
            else:
                out.append(f" {glyph} {level}", style=TXT_DIM)
        return out

    async def on_click(self, event) -> None:
        """Cycle through risk levels on click; persist + hot-reload."""
        idx = VALID_RISKS.index(self._current) if self._current in VALID_RISKS else 0
        new_level = VALID_RISKS[(idx + 1) % len(VALID_RISKS)]
        self._current = new_level
        self.update(self._render_text())

        try:
            from .core.mcp.config import load_config, save_config
            from .core.mcp import discovery
            cfg = load_config()
            if self._server in cfg.servers:
                cfg.servers[self._server].risks[self._tool] = new_level
                save_config(cfg)
                discovery._LIVE_CONFIG = cfg  # type: ignore[attr-defined]
                self.app.notify(
                    f"{self._server}.{self._tool} → {new_level}",
                    severity="information",
                )
        except Exception as e:
            self.app.notify(f"save failed: {e}", severity="error")


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------


class AddMCPServerModal(ModalScreen[MCPServerConfig | None]):
    """
    Add or edit an MCP server.

    Result: validated MCPServerConfig or None on cancel.
    """

    DEFAULT_CSS = f"""
    AddMCPServerModal {{
        align: center middle;
    }}
    AddMCPServerModal > Vertical {{
        width: 80%;
        max-width: 90;
        padding: 1 2;
        background: {BG_SOFT};
        border: round {BRONZE};
    }}
    .mcp-form-row {{
        height: auto;
        margin-bottom: 1;
    }}
    .mcp-form-label {{
        width: 18;
        color: {LABEL};
    }}
    .mcp-form-help {{
        color: {FORM_HELP};
        margin-top: 0;
    }}
    """

    def __init__(self, existing: MCPServerConfig | None = None) -> None:
        super().__init__()
        self._existing = existing
        # If editing, lock the name field
        self._editing = existing is not None
        self._transport = existing.transport if existing else "stdio"

    def compose(self) -> ComposeResult:
        with Vertical():
            title_text = "Edit MCP server" if self._editing else "Add MCP server"
            yield _Static(Text(title_text, style=f"bold {GOLD_HI}"))
            yield _Static(Text(
                "Stdio launches a local subprocess (npx/uvx/binary). "
                "HTTP connects to a remote streaming server.",
                style=TXT_DIM,
            ), classes="mcp-form-help")

            # Name
            with Horizontal(classes="mcp-form-row"):
                yield _Static(Text("name", style=GOLD_DIM), classes="mcp-form-label")
                yield Input(
                    value=(self._existing.name if self._existing else ""),
                    placeholder="time, github, my-tool",
                    id="mcp-input-name",
                    disabled=self._editing,
                )

            # Transport toggle
            with Horizontal(classes="mcp-form-row"):
                yield _Static(Text("transport", style=GOLD_DIM), classes="mcp-form-label")
                yield Button("stdio", id="mcp-trans-stdio",
                             variant="primary" if self._transport == "stdio" else "default")
                yield Button("http", id="mcp-trans-http",
                             variant="primary" if self._transport == "http" else "default")

            # ── Stdio fields ──────────────────────────────────────────────
            with Vertical(id="mcp-stdio-fields"):
                with Horizontal(classes="mcp-form-row"):
                    yield _Static(Text("command", style=GOLD_DIM), classes="mcp-form-label")
                    yield Input(
                        value=(self._existing.command if self._existing else ""),
                        placeholder="uvx, npx, /path/to/binary",
                        id="mcp-input-command",
                    )
                with Horizontal(classes="mcp-form-row"):
                    yield _Static(Text("args", style=GOLD_DIM), classes="mcp-form-label")
                    yield Input(
                        value=(" ".join(self._existing.args) if self._existing else ""),
                        placeholder="-y @org/server-foo --flag",
                        id="mcp-input-args",
                    )
                with Horizontal(classes="mcp-form-row"):
                    yield _Static(Text("env", style=GOLD_DIM), classes="mcp-form-label")
                    yield Input(
                        value=_pairs_to_str(self._existing.env if self._existing else {}),
                        placeholder="API_KEY=vault:my_key, DEBUG=1",
                        id="mcp-input-env",
                    )

            # ── HTTP fields ──────────────────────────────────────────────
            with Vertical(id="mcp-http-fields"):
                with Horizontal(classes="mcp-form-row"):
                    yield _Static(Text("url", style=GOLD_DIM), classes="mcp-form-label")
                    yield Input(
                        value=(self._existing.url if self._existing else ""),
                        placeholder="https://mcp.example.com/mcp",
                        id="mcp-input-url",
                    )
                with Horizontal(classes="mcp-form-row"):
                    yield _Static(Text("headers", style=GOLD_DIM), classes="mcp-form-label")
                    yield Input(
                        value=_pairs_to_str(self._existing.headers if self._existing else {}),
                        placeholder="Authorization=Bearer vault:token",
                        id="mcp-input-headers",
                    )

            # Hint about vault: prefix
            from .core.platform_paths import get_config_dir
            yield _Static(Text(
                f"  tip: prefix any value with vault:KEY to look it up in "
                f"{get_config_dir() / 'secrets.env'} (no plaintext keys here)",
                style=GOLD_DIM,
            ), classes="mcp-form-help")

            # Buttons
            with Horizontal(classes="card-actions"):
                yield Button("Save", id="mcp-save", variant="primary")
                yield Button("Cancel", id="mcp-cancel")

    def on_mount(self) -> None:
        self._refresh_transport_visibility()

    def _refresh_transport_visibility(self) -> None:
        try:
            stdio = self.query_one("#mcp-stdio-fields", Vertical)
            http = self.query_one("#mcp-http-fields", Vertical)
        except Exception:
            return
        if self._transport == "stdio":
            stdio.display = True
            http.display = False
        else:
            stdio.display = False
            http.display = True

    @on(Button.Pressed)
    def _on_btn(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "mcp-cancel":
            self.dismiss(None)
            return
        if bid == "mcp-trans-stdio":
            self._transport = "stdio"
            self._refresh_transport_visibility()
            self.query_one("#mcp-trans-stdio", Button).variant = "primary"
            self.query_one("#mcp-trans-http", Button).variant = "default"
            return
        if bid == "mcp-trans-http":
            self._transport = "http"
            self._refresh_transport_visibility()
            self.query_one("#mcp-trans-stdio", Button).variant = "default"
            self.query_one("#mcp-trans-http", Button).variant = "primary"
            return
        if bid == "mcp-save":
            self._submit()
            return

    def _submit(self) -> None:
        name = self.query_one("#mcp-input-name", Input).value.strip()
        if not name:
            self.notify("name required", severity="error")
            return

        srv = MCPServerConfig(name=name)
        if self._transport == "stdio":
            srv.command = self.query_one("#mcp-input-command", Input).value.strip()
            args_str = self.query_one("#mcp-input-args", Input).value.strip()
            if args_str:
                srv.args = args_str.split()
            env_str = self.query_one("#mcp-input-env", Input).value.strip()
            srv.env = _str_to_pairs(env_str)
        else:
            srv.url = self.query_one("#mcp-input-url", Input).value.strip()
            hdr_str = self.query_one("#mcp-input-headers", Input).value.strip()
            srv.headers = _str_to_pairs(hdr_str)

        # Preserve existing risks/sampling/enabled when editing
        if self._existing:
            srv.risks = dict(self._existing.risks)
            srv.sampling = self._existing.sampling
            srv.enabled = self._existing.enabled
            srv.timeout = self._existing.timeout
            srv.connect_timeout = self._existing.connect_timeout

        errs = srv.validate()
        if errs:
            self.notify(errs[0], severity="error")
            return

        self.dismiss(srv)


# ---------------------------------------------------------------------------
# Button event handler — called from SetupScreen._on_button
# ---------------------------------------------------------------------------


async def handle_mcp_button(
    setup_screen,
    bid: str,
) -> bool:
    """
    Dispatch ``Button.Pressed`` from MCP cards.

    Returns True if the button was handled (and the section should re-render),
    False if the id is not an MCP button.
    """
    if not bid.startswith("mcp-"):
        return False

    # Add
    if bid == "mcp-add":
        srv = await setup_screen.app.push_screen_wait(AddMCPServerModal())
        if srv is not None:
            cfg = load_config()
            cfg.servers[srv.name] = srv
            save_config(cfg)
            _push_live_config(cfg)
            setup_screen.notify(f"server {srv.name!r} saved", severity="information")
        return True

    if bid == "mcp-reload":
        cfg = load_config()
        _push_live_config(cfg)
        # Trigger discover_mcp_tools on the running app if it exposes it
        try:
            app = setup_screen.app
            if hasattr(app, "_discover_mcp_tools"):
                # We don't have a feed object inside setup; just refresh config
                from .core.mcp import discover_mcp_tools
                from .core.mcp.sampling import build_sampling_callback
                from .core.tools import REGISTRY as _REG
                cb = None
                if getattr(app, "mesh", None) and getattr(app, "current_model", None):
                    try:
                        cb = build_sampling_callback(app.mesh, app.current_model)
                    except Exception as e:
                        # F37: surface sampling-callback failure to operator —
                        # silently nulling it left MCP servers thinking sampling
                        # was unavailable, and the user never knew why.
                        log.exception("build_sampling_callback failed during reload")
                        setup_screen.notify(
                            f"sampling disabled: {e}", severity="warning",
                        )
                        cb = None
                discover_mcp_tools(_REG, cfg, sampling_callback=cb)
        except Exception as e:
            # F36: was `except Exception: pass` — operator clicked "⟳ Reload"
            # and got "reloaded" toast even when the live config didn't take.
            log.exception("MCP reload (push live config / discover) failed")
            setup_screen.notify(
                f"reload failed: {e}", severity="error",
            )
            return False
        setup_screen.notify("mcp config reloaded", severity="information")
        return True

    # Per-server actions: mcp-edit-{name}, mcp-test-{name}, mcp-del-{name},
    # mcp-enable-{name}, mcp-disable-{name}
    for prefix, action in (
        ("mcp-edit-", "edit"),
        ("mcp-test-", "test"),
        ("mcp-del-", "delete"),
        ("mcp-enable-", "enable"),
        ("mcp-disable-", "disable"),
    ):
        if bid.startswith(prefix):
            name = bid[len(prefix):]
            await _server_action(setup_screen, action, name)
            return True

    # Risk pickers: mcp-risk-{server}-{tool}-{level}
    if bid.startswith("mcp-risk-"):
        rest = bid[len("mcp-risk-"):]
        # We can't naively split on '-' because tool names may contain '-'.
        # Strategy: the level is always the LAST segment, and we know the
        # set of valid levels.
        for level in VALID_RISKS:
            suffix = f"-{level}"
            if rest.endswith(suffix):
                head = rest[: -len(suffix)]
                # head is `{server}-{tool}` where server is one of the
                # currently configured servers — pick the longest prefix
                # match.
                cfg = load_config()
                server_match = None
                for sname in cfg.servers:
                    pat = f"{sname}-"
                    if head.startswith(pat):
                        if server_match is None or len(sname) > len(server_match):
                            server_match = sname
                if server_match is None:
                    setup_screen.notify(f"unknown server in {bid}",
                                        severity="warning")
                    return True
                tool = head[len(server_match) + 1:]
                cfg.servers[server_match].risks[tool] = level
                save_config(cfg)
                _push_live_config(cfg)
                setup_screen.notify(
                    f"{server_match}.{tool} → {level}",
                    severity="information",
                )
                return True

    return False


async def _server_action(setup_screen, action: str, name: str) -> None:
    cfg = load_config()
    if name not in cfg.servers:
        setup_screen.notify(f"server {name!r} not found", severity="error")
        return
    srv = cfg.servers[name]

    if action == "edit":
        new_srv = await setup_screen.app.push_screen_wait(AddMCPServerModal(srv))
        if new_srv is not None:
            cfg.servers[name] = new_srv
            save_config(cfg)
            _push_live_config(cfg)
            setup_screen.notify(f"server {name!r} updated", severity="information")
        return

    if action == "delete":
        del cfg.servers[name]
        save_config(cfg)
        _push_live_config(cfg)
        setup_screen.notify(f"server {name!r} deleted", severity="information")
        return

    if action == "enable":
        srv.enabled = True
        save_config(cfg)
        _push_live_config(cfg)
        setup_screen.notify(f"server {name!r} enabled (restart to connect)",
                            severity="information")
        return

    if action == "disable":
        srv.enabled = False
        save_config(cfg)
        _push_live_config(cfg)
        setup_screen.notify(f"server {name!r} disabled", severity="information")
        return

    if action == "test":
        # Run test in background to avoid blocking the UI
        async def _run_test():
            from .core.mcp.config import MCPConfig as _MC
            from .core.mcp.client import get_manager, shutdown_mcp
            from .core.mcp.discovery import _wait_for_initial_connections

            mgr = get_manager()
            if not mgr.is_available():
                setup_screen.notify("mcp package not installed", severity="error")
                return
            one = _MC(default_timeout=cfg.default_timeout,
                      default_connect_timeout=cfg.default_connect_timeout,
                      default_risk=cfg.default_risk)
            one.servers[name] = cfg.servers[name]
            try:
                mgr.start(one)
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: _wait_for_initial_connections(mgr, one, max_wait=30.0),
                )
                statuses = mgr.status()
                st = next((s for s in statuses if s["name"] == name), None)
                if st and st["state"] == "connected":
                    setup_screen.notify(
                        f"{name}: connected · {st['tool_count']} tools",
                        severity="information",
                    )
                else:
                    err = (st or {}).get("last_error") or "unknown"
                    setup_screen.notify(f"{name}: failed — {err}",
                                        severity="error")
            finally:
                # Don't shutdown the live manager — only one_test path tested
                # ↑ keep alive so other servers keep working
                pass

        _t = asyncio.create_task(_run_test())
        # Hold a strong ref so the task isn't GC'd mid-flight, then drop
        # it once it's done. See module-level note on ``_BACKGROUND_TASKS``.
        _BACKGROUND_TASKS.add(_t)
        _t.add_done_callback(_BACKGROUND_TASKS.discard)
        setup_screen.notify(f"testing {name}…", severity="information")
        return


def _push_live_config(cfg: MCPConfig) -> None:
    """Update discovery._LIVE_CONFIG so risk_for_mcp_tool sees changes."""
    try:
        from .core.mcp import discovery
        discovery._LIVE_CONFIG = cfg  # type: ignore[attr-defined]
    except Exception:
        # F36: surface this — silently swallowing meant a stale live
        # config kept routing tool calls with the old risk policy.
        log.exception("Failed to push live MCP config to discovery._LIVE_CONFIG")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pairs_to_str(d: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in d.items()) if d else ""


def _str_to_pairs(s: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not s:
        return out
    for pair in s.split(","):
        pair = pair.strip()
        if "=" in pair:
            k, _, v = pair.partition("=")
            out[k.strip()] = v.strip()
    return out
