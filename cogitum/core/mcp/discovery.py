"""
cogitum.core.mcp.discovery
~~~~~~~~~~~~~~~~~~~~~~~~~~

Bridges MCP servers' tool catalogs into Cogitum's :class:`ToolRegistry`.

Each MCP tool is wrapped in a synthetic ``ToolSpec`` named
``mcp_{server}_{tool}`` whose ``fn`` synchronously delegates to the
:class:`MCPManager` singleton (which marshals onto its own loop).

Risk lookup is done lazily by :func:`risk_for_mcp_tool` so the agent's
``classify_danger`` can read it on every call without re-parsing TOML.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from typing import Any

from cogitum.core.tools import REGISTRY, ToolRegistry, ToolSpec

from .client import MCPManager, get_manager
from .config import MCPConfig, load_config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

# Once we've discovered tools, we hold onto the config so classify_danger
# can look up per-tool risks without rereading the TOML on every call.
_LIVE_CONFIG: MCPConfig | None = None
_LIVE_LOCK = threading.Lock()
_REGISTERED_NAMES: set[str] = set()


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_]")


def _safe_name_part(s: str) -> str:
    return _SAFE_NAME_RE.sub("_", s)


def tool_id(server: str, tool: str) -> str:
    """Build the registry-side name: ``mcp_{server}_{tool}``."""
    return f"mcp_{_safe_name_part(server)}_{_safe_name_part(tool)}"


_TOOL_ID_RE = re.compile(r"^mcp_([A-Za-z0-9]+)_(.+)$")


def parse_tool_id(name: str) -> tuple[str, str] | None:
    """
    Parse a Cogitum tool name back to ``(server, tool)``.

    Returns ``None`` if not an MCP tool. Note: because both server and
    tool names go through ``_safe_name_part``, the *parsed* names are
    sanitized — caller should match against the same sanitized form.
    """
    if not name.startswith("mcp_"):
        return None
    m = _TOOL_ID_RE.match(name)
    if not m:
        return None
    return m.group(1), m.group(2)


# ---------------------------------------------------------------------------
# JSON-Schema → ToolSpec.parameters
# ---------------------------------------------------------------------------


def _normalize_schema(schema: Any) -> dict[str, Any]:
    """
    Make sure the inputSchema we got from MCP is a JSON-schema dict that
    OpenAI/Anthropic tool-calling APIs accept.
    """
    if not schema or not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    out = dict(schema)
    out.setdefault("type", "object")
    out.setdefault("properties", {})
    return out


# ---------------------------------------------------------------------------
# Synthetic call wrapper
# ---------------------------------------------------------------------------


def _make_call_fn(server: str, tool: str, manager: MCPManager):
    """
    Return an async callable that the ToolSpec executor will invoke.

    Cogitum's ToolSpec.call accepts both sync and async; we make it async
    because MCP IO is async by nature, but we delegate to the manager's
    blocking call_tool inside an executor so the agent's outer asyncio
    loop never blocks.
    """

    async def _call(**kwargs: Any) -> str:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: manager.call_tool(server, tool, kwargs),
        )
        if "error" in result:
            return f"ERROR: {result['error']}"
        return result.get("result", "") or ""

    _call.__name__ = tool_id(server, tool)
    return _call


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def discover_mcp_tools(
    registry: ToolRegistry | None = None,
    config: MCPConfig | None = None,
    sampling_callback: Any = None,
) -> dict[str, Any]:
    """
    Connect to all configured MCP servers, discover their tools, and
    register them in ``registry`` (defaults to the global REGISTRY).

    Idempotent. Re-running:
      * connects new servers and registers their tools
      * unregisters tools whose servers were removed/disabled in config
      * triggers reconnect for servers whose transport config changed

    Parameters
    ----------
    registry
        Target registry. Defaults to ``cogitum.core.tools.REGISTRY``.
    config
        Pre-loaded MCPConfig. If omitted, loaded from
        ``~/.config/cogitum/mcp.toml``.
    sampling_callback
        Optional async ``(server_name, request_dict) -> dict`` invoked
        when an MCP server requests an LLM completion. If omitted,
        sampling is disabled regardless of per-server config.

    Returns
    -------
    dict
        ``{"servers": [...status entries], "registered": [tool_id, ...],
        "unregistered": [tool_id, ...]}``
    """
    global _LIVE_CONFIG

    target = registry or REGISTRY

    cfg = config or load_config()
    with _LIVE_LOCK:
        _LIVE_CONFIG = cfg

    manager = get_manager()
    if not manager.is_available():
        log.warning(
            "mcp package not installed — `pip install 'cogitum[mcp]'` to enable. "
            "MCP tool discovery skipped."
        )
        return {"servers": [], "registered": [], "unregistered": []}

    if sampling_callback is not None:
        manager.set_sampling_callback(sampling_callback)

    # Even with no servers configured, start (idempotent) so any later
    # hot-reload via reconcile works.
    manager.start(cfg)

    if not cfg.servers:
        # All servers removed: unregister everything we previously added.
        unregistered = _unregister_stale(target, cfg)
        log.info("mcp: no servers configured; %d tools unregistered",
                 len(unregistered))
        return {"servers": [], "registered": [], "unregistered": unregistered}

    # Wait briefly for initial connections so we can register their tools.
    _wait_for_initial_connections(manager, cfg, max_wait=5.0)

    # First pass: drop any tools whose server is gone or disabled.
    unregistered = _unregister_stale(target, cfg)

    registered: list[str] = []
    for server_name, srv_cfg in cfg.servers.items():
        if not srv_cfg.enabled:
            continue
        handle = manager.get_handle(server_name)
        if handle is None or handle.state != "connected":
            continue
        for mcp_tool in handle.tools:
            tname = tool_id(server_name, mcp_tool.name)
            if tname in _REGISTERED_NAMES:
                continue
            base_desc = (mcp_tool.description or "").strip()
            full_desc = (
                f"[MCP · server={server_name} · tool={mcp_tool.name}] "
                f"{base_desc or 'External MCP tool.'}"
            )[:1500]
            spec = ToolSpec(
                name=tname,
                description=full_desc,
                parameters=_normalize_schema(getattr(mcp_tool, "inputSchema", None)),
                fn=_make_call_fn(server_name, mcp_tool.name, manager),
                tags=["mcp", f"mcp:{server_name}", f"mcp_tool:{mcp_tool.name}"],
            )
            target.register(spec)
            _REGISTERED_NAMES.add(tname)
            registered.append(tname)
            log.info("mcp: registered tool %s", tname)

    return {
        "servers": manager.status(),
        "registered": registered,
        "unregistered": unregistered,
    }


def _unregister_stale(target: ToolRegistry, cfg: MCPConfig) -> list[str]:
    """
    Drop registered MCP tools whose server is no longer in ``cfg`` (or is
    disabled).

    Returns the list of tool names that were removed.
    """
    enabled_servers = {
        _safe_name_part(name)
        for name, srv in cfg.servers.items()
        if srv.enabled
    }

    removed: list[str] = []
    for tname in list(_REGISTERED_NAMES):
        parsed = parse_tool_id(tname)
        if parsed is None:
            continue
        server_part, _ = parsed
        if server_part not in enabled_servers:
            # Drop from registry
            target._tools.pop(tname, None)  # type: ignore[attr-defined]
            _REGISTERED_NAMES.discard(tname)
            removed.append(tname)
            log.info("mcp: unregistered tool %s (server gone/disabled)", tname)
    return removed


def mcp_status() -> list[dict[str, Any]]:
    """Return current connection status for each configured server."""
    manager = get_manager()
    if not manager.is_available():
        return []
    return manager.status()


def risk_for_mcp_tool(tool_name: str) -> str | None:
    """
    Return the configured risk level (``"low"|"medium"|"danger"``) for a
    Cogitum tool name like ``mcp_time_get_current_time``.

    Returns ``None`` if not an MCP tool. Returns the configured default
    risk if the specific tool isn't listed in the server's ``risks`` table.
    """
    parts = parse_tool_id(tool_name)
    if parts is None:
        return None
    server_part, tool_part = parts

    with _LIVE_LOCK:
        cfg = _LIVE_CONFIG

    if cfg is None:
        return None

    # Find the server whose sanitized name matches
    for srv_name, srv in cfg.servers.items():
        if _safe_name_part(srv_name) == server_part:
            # Try sanitized match first (we register sanitized names)
            for tname, risk in srv.risks.items():
                if _safe_name_part(tname) == tool_part or tname == tool_part:
                    return risk
            return cfg.default_risk
    return cfg.default_risk


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_for_initial_connections(
    manager: MCPManager,
    cfg: MCPConfig,
    max_wait: float = 5.0,
) -> None:
    """
    Block up to ``max_wait`` seconds for at-least-one round of connect
    attempts to land. Servers that take longer will simply register their
    tools later (next discover_mcp_tools call) — but for typical local
    stdio servers (uvx, npx) this is plenty.
    """
    import time as _time

    deadline = _time.monotonic() + max_wait
    enabled_count = sum(1 for s in cfg.servers.values() if s.enabled)
    if enabled_count == 0:
        return

    while _time.monotonic() < deadline:
        statuses = manager.status()
        terminal_count = sum(
            1 for s in statuses if s["state"] in ("connected", "failed")
        )
        if terminal_count >= enabled_count:
            return
        _time.sleep(0.1)
