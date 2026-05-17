"""
cogitum.core.mcp
~~~~~~~~~~~~~~~~

Native MCP (Model Context Protocol) client.

Connects to MCP servers configured in ``~/.config/cogitum/mcp.toml``,
discovers their tools, and registers them into the global ``ToolRegistry``
with the prefix ``mcp_{server}_{tool}`` so the agent can call them
indistinguishably from built-in tools.

Public API
----------
- :func:`discover_mcp_tools` — idempotent startup hook
- :func:`mcp_status` — list servers + tool counts + state
- :func:`risk_for_mcp_tool` — per-tool risk override from config
- :func:`shutdown_mcp` — graceful close on app exit
- :class:`MCPManager` — long-lived connection manager (singleton)

Optional dependency: ``pip install 'cogitum[mcp]'``. If the ``mcp``
package is missing, ``discover_mcp_tools`` is a no-op and logs a warning.
"""
from __future__ import annotations

from .config import (
    MCPConfig,
    MCPServerConfig,
    SamplingConfig,
    config_path,
    load_config,
    save_config,
)
from .client import MCPManager, get_manager, shutdown_mcp
from .discovery import discover_mcp_tools, mcp_status, risk_for_mcp_tool
from .watcher import start_watcher

__all__ = [
    "MCPConfig",
    "MCPServerConfig",
    "SamplingConfig",
    "MCPManager",
    "config_path",
    "load_config",
    "save_config",
    "discover_mcp_tools",
    "mcp_status",
    "risk_for_mcp_tool",
    "get_manager",
    "shutdown_mcp",
    "start_watcher",
]
