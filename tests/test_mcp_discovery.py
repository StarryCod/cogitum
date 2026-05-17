"""Tests for cogitum.core.mcp.discovery."""
from __future__ import annotations

import pytest

from cogitum.core.mcp import discovery
from cogitum.core.mcp.config import MCPConfig, MCPServerConfig
from cogitum.core.mcp.discovery import (
    parse_tool_id,
    risk_for_mcp_tool,
    tool_id,
)


def test_tool_id_safe_chars():
    assert tool_id("time", "get_current") == "mcp_time_get_current"


def test_tool_id_sanitizes_hyphens():
    assert tool_id("my-server", "list-issues") == "mcp_my_server_list_issues"


def test_tool_id_sanitizes_dots():
    assert tool_id("my.api", "fetch.data") == "mcp_my_api_fetch_data"


def test_parse_tool_id_roundtrip():
    assert parse_tool_id("mcp_time_get_current") == ("time", "get_current")


def test_parse_tool_id_rejects_non_mcp():
    assert parse_tool_id("terminal") is None
    assert parse_tool_id("read_file") is None


def test_parse_tool_id_no_underscore():
    assert parse_tool_id("mcp_") is None


def test_risk_returns_none_for_non_mcp_tool(monkeypatch):
    monkeypatch.setattr(discovery, "_LIVE_CONFIG", None)
    assert risk_for_mcp_tool("terminal") is None


def test_risk_returns_default_when_no_override():
    cfg = MCPConfig(default_risk="medium")
    cfg.servers["time"] = MCPServerConfig(name="time", command="cmd")
    discovery._LIVE_CONFIG = cfg  # type: ignore[attr-defined]
    assert risk_for_mcp_tool("mcp_time_anything") == "medium"


def test_risk_per_tool_override():
    cfg = MCPConfig(default_risk="medium")
    cfg.servers["time"] = MCPServerConfig(
        name="time",
        command="cmd",
        risks={"get_current_time": "low", "destroy": "danger"},
    )
    discovery._LIVE_CONFIG = cfg  # type: ignore[attr-defined]
    assert risk_for_mcp_tool("mcp_time_get_current_time") == "low"
    assert risk_for_mcp_tool("mcp_time_destroy") == "danger"
    assert risk_for_mcp_tool("mcp_time_other") == "medium"


def test_risk_when_no_live_config():
    discovery._LIVE_CONFIG = None  # type: ignore[attr-defined]
    assert risk_for_mcp_tool("mcp_time_x") is None


def test_risk_unknown_server_returns_default():
    cfg = MCPConfig(default_risk="danger")
    cfg.servers["a"] = MCPServerConfig(name="a", command="cmd")
    discovery._LIVE_CONFIG = cfg  # type: ignore[attr-defined]
    # Tool from server that's not in cfg falls back to default
    assert risk_for_mcp_tool("mcp_b_anything") == "danger"


def test_classify_danger_uses_mcp_risk(monkeypatch):
    """The wired-in classify_danger should pick up MCP risks."""
    # Re-import inside the test: conftest clears cogitum.* from sys.modules
    # between tests so we want the *same* module instance as the lazy import
    # inside classify_danger.
    from cogitum.core.mcp import discovery as _disc
    from cogitum.core.mcp.config import MCPConfig as _Cfg, MCPServerConfig as _Srv
    from cogitum.core.builtin_tools import classify_danger

    cfg = _Cfg(default_risk="medium")
    cfg.servers["time"] = _Srv(
        name="time",
        command="cmd",
        risks={"get_current_time": "low"},
    )
    _disc._LIVE_CONFIG = cfg  # type: ignore[attr-defined]
    assert classify_danger("mcp_time_get_current_time", {}) == "low"
    assert classify_danger("mcp_time_unknown", {}) == "medium"
    # non-MCP tool path still works
    assert classify_danger("write_file", {"path": "/tmp/x.txt"}) == "low"


def test_classify_danger_unknown_mcp_tool_when_no_config(monkeypatch):
    """No config → unknown MCP tool defaults to medium (require approval)."""
    from cogitum.core.mcp import discovery as _disc
    from cogitum.core.builtin_tools import classify_danger

    _disc._LIVE_CONFIG = None  # type: ignore[attr-defined]
    assert classify_danger("mcp_random_thing", {}) == "medium"
