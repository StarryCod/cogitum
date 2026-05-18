"""Tests for cogitum.core.mcp.config (load/save/risk roundtrip)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from cogitum.core.mcp.config import (
    MCPConfig,
    MCPServerConfig,
    SamplingConfig,
    VALID_RISKS,
    config_path,
    load_config,
    save_config,
)


@pytest.fixture()
def tmp_cfg(tmp_path, monkeypatch):
    """Force the Cogitum config dir to a tmp dir so config_path() points there."""
    monkeypatch.setenv("COGITUM_CONFIG_DIR", str(tmp_path))
    return tmp_path / "cogitum" / "mcp.toml"


def test_config_path_uses_override(tmp_cfg):
    assert config_path() == tmp_cfg


def test_load_returns_empty_when_missing(tmp_cfg):
    cfg = load_config()
    assert isinstance(cfg, MCPConfig)
    assert cfg.servers == {}
    assert cfg.default_risk == "medium"


def test_save_then_load_stdio(tmp_cfg):
    cfg = MCPConfig()
    cfg.servers["time"] = MCPServerConfig(
        name="time",
        command="uvx",
        args=["mcp-server-time"],
        env={"TZ": "UTC"},
        risks={"get_current_time": "low"},
    )
    p = save_config(cfg)
    assert p == tmp_cfg
    loaded = load_config()
    assert "time" in loaded.servers
    s = loaded.servers["time"]
    assert s.transport == "stdio"
    assert s.command == "uvx"
    assert s.args == ["mcp-server-time"]
    assert s.env == {"TZ": "UTC"}
    assert s.risks == {"get_current_time": "low"}


def test_save_then_load_http(tmp_cfg):
    cfg = MCPConfig()
    cfg.servers["company"] = MCPServerConfig(
        name="company",
        url="https://mcp.example.com/mcp",
        headers={"Authorization": "Bearer X"},
        timeout=300,
        risks={"delete_record": "danger", "list_records": "low"},
    )
    save_config(cfg)
    loaded = load_config()
    s = loaded.servers["company"]
    assert s.transport == "http"
    assert s.url == "https://mcp.example.com/mcp"
    assert s.headers["Authorization"] == "Bearer X"
    assert s.timeout == 300
    assert s.risks["delete_record"] == "danger"


def test_validate_rejects_both_command_and_url():
    srv = MCPServerConfig(name="bad", command="x", url="https://y")
    errs = srv.validate()
    assert any("both" in e for e in errs)


def test_validate_rejects_missing_transport():
    srv = MCPServerConfig(name="bad")
    errs = srv.validate()
    assert any("either" in e for e in errs)


def test_validate_rejects_invalid_risk():
    srv = MCPServerConfig(name="bad", command="x", risks={"foo": "extreme"})
    errs = srv.validate()
    assert any("risk" in e for e in errs)


def test_disabled_server_persists(tmp_cfg):
    cfg = MCPConfig()
    cfg.servers["x"] = MCPServerConfig(name="x", command="cmd", enabled=False)
    save_config(cfg)
    loaded = load_config()
    assert loaded.servers["x"].enabled is False


def test_file_is_chmod_0600(tmp_cfg):
    cfg = MCPConfig()
    cfg.servers["x"] = MCPServerConfig(name="x", command="echo")
    p = save_config(cfg)
    mode = os.stat(p).st_mode & 0o777
    assert mode == 0o600


def test_default_risk_inheritance():
    """tools without explicit risk should fall back to defaults.default_risk."""
    cfg = MCPConfig(default_risk="danger")
    cfg.servers["x"] = MCPServerConfig(name="x", command="cmd")
    # Helper not on cfg; we check directly via discovery's risk_for_mcp_tool
    from cogitum.core.mcp import discovery
    discovery._LIVE_CONFIG = cfg  # type: ignore[attr-defined]
    assert discovery.risk_for_mcp_tool("mcp_x_anything") == "danger"


def test_per_tool_risk_overrides_default():
    cfg = MCPConfig(default_risk="medium")
    cfg.servers["x"] = MCPServerConfig(
        name="x", command="cmd",
        risks={"safe_tool": "low"},
    )
    from cogitum.core.mcp import discovery
    discovery._LIVE_CONFIG = cfg  # type: ignore[attr-defined]
    assert discovery.risk_for_mcp_tool("mcp_x_safe_tool") == "low"
    assert discovery.risk_for_mcp_tool("mcp_x_other_tool") == "medium"


def test_sampling_config_defaults():
    s = SamplingConfig()
    assert s.enabled is True
    assert s.max_tokens_cap == 4096
    assert s.timeout == 30


def test_sampling_inheritance():
    """Per-server sampling should override defaults."""
    cfg = MCPConfig(default_sampling=SamplingConfig(enabled=True, max_tokens_cap=4096))
    cfg.servers["x"] = MCPServerConfig(
        name="x", command="cmd",
        sampling=SamplingConfig(enabled=False),
    )
    eff = cfg.effective_sampling("x")
    assert eff.enabled is False  # per-server override wins
