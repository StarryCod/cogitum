"""
cogitum.cli_mcp
~~~~~~~~~~~~~~~

CLI subcommands for managing MCP servers.

Subcommands:
  cog mcp list                     — show configured servers + status
  cog mcp path                     — print path to mcp.toml
  cog mcp add <name>               — interactive add (stdio or http)
  cog mcp remove <name>            — delete a server entry
  cog mcp enable <name>            — re-enable a disabled server
  cog mcp disable <name>           — keep config but skip on startup
  cog mcp risk <server> <tool> <level>
                                   — set per-tool risk: low|medium|danger
  cog mcp test <name>              — connect & list tools (dry run)
  cog mcp tools [name]             — list discovered tools (per server)

All subcommands return 0 on success, 1 on failure.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .core.mcp.config import (
    MCPConfig,
    MCPServerConfig,
    SamplingConfig,
    VALID_RISKS,
    config_path,
    load_config,
    save_config,
)


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _err(msg: str) -> None:
    print(f"  ✗ {msg}", file=sys.stderr)


def _warn(msg: str) -> None:
    print(f"  ! {msg}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _cmd_path(args: argparse.Namespace) -> int:
    print(config_path())
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    cfg = load_config()
    if not cfg.servers:
        print(f"  (no servers configured at {config_path()})")
        print(f"  add one with `cog mcp add <name>`")
        return 0

    print(f"  MCP servers in {config_path()}:")
    print(f"  default risk: {cfg.default_risk}    sampling: "
          f"{'on' if cfg.default_sampling.enabled else 'off'}")
    print()
    for name, srv in cfg.servers.items():
        flag = "" if srv.enabled else "  [DISABLED]"
        if srv.transport == "stdio":
            target = f"{srv.command} {' '.join(srv.args or [])}".strip()
        elif srv.transport == "http":
            target = srv.url
        else:
            target = "<invalid>"
        print(f"  • {name}  ({srv.transport}){flag}")
        print(f"      target:  {target}")
        if srv.risks:
            risks = ", ".join(f"{t}={r}" for t, r in srv.risks.items())
            print(f"      risks:   {risks}")
        if srv.env:
            keys = ", ".join(srv.env.keys())
            print(f"      env:     {keys}")
        print()
    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    name = args.name.strip()
    if not name:
        _err("name required")
        return 1

    cfg = load_config()
    if name in cfg.servers and not args.force:
        _err(f"server {name!r} already exists (use --force to overwrite)")
        return 1

    print(f"  Configuring MCP server: {name}")
    print()

    transport = (args.transport or input("  transport [stdio/http] (stdio): ").strip() or "stdio").lower()
    if transport not in ("stdio", "http"):
        _err(f"transport must be 'stdio' or 'http', got {transport!r}")
        return 1

    srv = MCPServerConfig(name=name)

    if transport == "stdio":
        cmd = args.command or input("  command (e.g. uvx, npx): ").strip()
        if not cmd:
            _err("command required for stdio")
            return 1
        srv.command = cmd

        args_str = args.args
        if args_str is None:
            args_str = input("  args (space-separated, e.g. '-y @org/server-foo'): ").strip()
        if args_str:
            srv.args = args_str.split()

        env_str = args.env if args.env is not None else input(
            "  env vars (KEY=VAL,KEY2=VAL2 or vault:KEY,empty=skip): "
        ).strip()
        if env_str:
            for pair in env_str.split(","):
                if "=" in pair:
                    k, _, v = pair.partition("=")
                    srv.env[k.strip()] = v.strip()
    else:
        url = args.url or input("  url (https://...): ").strip()
        if not url:
            _err("url required for http")
            return 1
        srv.url = url

        hdr_str = args.headers if args.headers is not None else input(
            "  headers (KEY=VAL,KEY2=VAL2 or vault:KEY,empty=skip): "
        ).strip()
        if hdr_str:
            for pair in hdr_str.split(","):
                if "=" in pair:
                    k, _, v = pair.partition("=")
                    srv.headers[k.strip()] = v.strip()

    errs = srv.validate()
    if errs:
        for e in errs:
            _err(e)
        return 1

    cfg.servers[name] = srv
    path = save_config(cfg)
    _ok(f"saved server {name!r} to {path}")
    print(f"  edit per-tool risks with `cog mcp risk {name} <tool> low|medium|danger`")
    print(f"  restart Cogitum (or `cog tg restart`) to connect.")
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.name not in cfg.servers:
        _err(f"no server named {args.name!r}")
        return 1
    del cfg.servers[args.name]
    path = save_config(cfg)
    _ok(f"removed {args.name!r} from {path}")
    return 0


def _cmd_enable(args: argparse.Namespace) -> int:
    return _toggle_enabled(args.name, True)


def _cmd_disable(args: argparse.Namespace) -> int:
    return _toggle_enabled(args.name, False)


def _toggle_enabled(name: str, enabled: bool) -> int:
    cfg = load_config()
    if name not in cfg.servers:
        _err(f"no server named {name!r}")
        return 1
    cfg.servers[name].enabled = enabled
    path = save_config(cfg)
    _ok(f"server {name!r} {'enabled' if enabled else 'disabled'} in {path}")
    return 0


def _cmd_risk(args: argparse.Namespace) -> int:
    server = args.server
    tool = args.tool
    level = args.level.lower()
    if level not in VALID_RISKS:
        _err(f"level must be one of {VALID_RISKS}, got {args.level!r}")
        return 1
    cfg = load_config()
    if server not in cfg.servers:
        _err(f"no server named {server!r}")
        return 1
    srv = cfg.servers[server]
    srv.risks[tool] = level
    path = save_config(cfg)
    _ok(f"{server}.{tool} risk = {level}  →  {path}")
    return 0


def _cmd_test(args: argparse.Namespace) -> int:
    """Connect to one server, list tools, then close — dry run, no registry."""
    cfg = load_config()
    if args.name not in cfg.servers:
        _err(f"no server named {args.name!r}")
        return 1

    # Build a fresh single-server config
    one = MCPConfig(
        default_timeout=cfg.default_timeout,
        default_connect_timeout=cfg.default_connect_timeout,
        default_risk=cfg.default_risk,
    )
    one.servers[args.name] = cfg.servers[args.name]

    from .core.mcp.client import get_manager, shutdown_mcp

    mgr = get_manager()
    if not mgr.is_available():
        _err("mcp package not installed. Run: pip install 'cogitum[mcp]'")
        return 1

    print(f"  connecting to {args.name!r}…")
    try:
        mgr.start(one)
        # Block on the same wait loop discovery uses (but longer to allow
        # first-time uvx/npx package downloads).
        from .core.mcp.discovery import _wait_for_initial_connections
        _wait_for_initial_connections(mgr, one, max_wait=120.0)

        status = mgr.status()
        if not status:
            _err("no status returned (manager not started?)")
            return 1
        st = status[0]
        if st["state"] != "connected":
            _err(f"failed: state={st['state']}, last_error={st.get('last_error')}")
            return 1
        _ok(f"connected · {st['tool_count']} tools")
        for tool in st["tools"]:
            print(f"     • {tool}")
    finally:
        shutdown_mcp()
    return 0


def _cmd_tools(args: argparse.Namespace) -> int:
    """Show the live tool list (requires a running connection)."""
    cfg = load_config()
    if not cfg.servers:
        print("  (no servers configured)")
        return 0

    from .core.mcp.client import get_manager, shutdown_mcp
    from .core.mcp.discovery import _wait_for_initial_connections

    mgr = get_manager()
    if not mgr.is_available():
        _err("mcp package not installed. Run: pip install 'cogitum[mcp]'")
        return 1

    target_cfg = MCPConfig(
        default_timeout=cfg.default_timeout,
        default_connect_timeout=cfg.default_connect_timeout,
        default_risk=cfg.default_risk,
    )
    if args.name:
        if args.name not in cfg.servers:
            _err(f"no server named {args.name!r}")
            return 1
        target_cfg.servers[args.name] = cfg.servers[args.name]
    else:
        target_cfg.servers = dict(cfg.servers)

    try:
        mgr.start(target_cfg)
        _wait_for_initial_connections(mgr, target_cfg, max_wait=15.0)
        for st in mgr.status():
            print(f"  {st['name']}  ({st['state']})")
            if st["state"] == "connected":
                for tool in st["tools"]:
                    risk = "?"
                    srv = cfg.servers.get(st["name"])
                    if srv:
                        risk = srv.risks.get(tool, cfg.default_risk)
                    print(f"     • {tool}    [{risk}]")
            elif st.get("last_error"):
                print(f"     ! {st['last_error']}")
            print()
    finally:
        shutdown_mcp()
    return 0


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_DISPATCH = {
    "list": _cmd_list,
    "ls": _cmd_list,
    "path": _cmd_path,
    "add": _cmd_add,
    "remove": _cmd_remove,
    "rm": _cmd_remove,
    "enable": _cmd_enable,
    "disable": _cmd_disable,
    "risk": _cmd_risk,
    "test": _cmd_test,
    "tools": _cmd_tools,
}


def mcp_command(args: argparse.Namespace) -> int:
    action = getattr(args, "mcp_action", None)
    fn = _DISPATCH.get(action or "")
    if fn is None:
        _err(f"unknown mcp action: {action!r}")
        print("  Available: " + ", ".join(sorted(set(_DISPATCH))))
        return 1
    try:
        return fn(args)
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        _err(f"error: {e}")
        return 1


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------


def add_mcp_subparser(parent_sub: argparse._SubParsersAction) -> None:
    """Attach the `mcp` subparser to the main `cog` argparser."""
    mcp = parent_sub.add_parser("mcp", help="manage Model Context Protocol servers")
    mcp_sub = mcp.add_subparsers(dest="mcp_action", required=True)

    mcp_sub.add_parser("list", help="list configured servers and status")
    mcp_sub.add_parser("path", help="print path to mcp.toml")

    add = mcp_sub.add_parser("add", help="add a server (interactive)")
    add.add_argument("name")
    add.add_argument("--transport", choices=["stdio", "http"])
    add.add_argument("--command")
    add.add_argument("--args")
    add.add_argument("--env")
    add.add_argument("--url")
    add.add_argument("--headers")
    add.add_argument("--force", action="store_true")

    rm = mcp_sub.add_parser("remove", help="remove a server")
    rm.add_argument("name")

    en = mcp_sub.add_parser("enable", help="enable a server (default for new servers)")
    en.add_argument("name")
    di = mcp_sub.add_parser("disable", help="keep config but skip on startup")
    di.add_argument("name")

    risk = mcp_sub.add_parser("risk", help="set risk for a specific tool")
    risk.add_argument("server")
    risk.add_argument("tool")
    risk.add_argument("level", choices=list(VALID_RISKS))

    test = mcp_sub.add_parser("test", help="connect and list tools (dry run)")
    test.add_argument("name")

    tools = mcp_sub.add_parser("tools", help="list discovered tools per server")
    tools.add_argument("name", nargs="?")

    mcp.set_defaults(func=mcp_command)
