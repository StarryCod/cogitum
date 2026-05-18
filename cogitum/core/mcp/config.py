"""
cogitum.core.mcp.config
~~~~~~~~~~~~~~~~~~~~~~~

Load/save ``~/.config/cogitum/mcp.toml``.

Schema
------
::

    # Global defaults (all keys optional)
    [defaults]
    timeout = 120              # per-call timeout (seconds)
    connect_timeout = 60       # initial handshake timeout
    default_risk = "medium"    # fallback risk for any MCP tool: low|medium|danger

    # Optional: enable LLM sampling for servers that request it
    [defaults.sampling]
    enabled = true
    max_tokens_cap = 4096
    timeout = 30
    max_rpm = 10
    max_tool_rounds = 5
    log_level = "info"
    allowed_models = []        # empty = all models in mesh

    # ── Stdio server ──────────────────────────────────────────────
    [servers.time]
    command = "uvx"
    args = ["mcp-server-time"]
    # env passed verbatim; values prefixed "vault:" resolve from secrets.env
    env = { TZ = "UTC" }

    # Per-tool risk override (low/medium/danger).
    # Tool names here are the *bare* MCP tool name (no mcp_{server}_ prefix).
    [servers.time.risks]
    get_current_time = "low"

    # ── HTTP/StreamableHTTP server ────────────────────────────────
    [servers.company]
    url = "https://mcp.example.com/mcp"
    headers = { Authorization = "Bearer vault:company_token" }
    timeout = 180

    [servers.company.risks]
    delete_record = "danger"
    list_records  = "low"
    # everything else falls back to defaults.default_risk

    # Per-server sampling override (overrides defaults.sampling)
    [servers.company.sampling]
    enabled = false

The file may not exist; in that case ``load_config()`` returns an empty
config object and the manager simply discovers no servers.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

try:
    import tomllib  # py311+
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

VALID_RISKS = ("low", "medium", "danger")
DEFAULT_RISK = "medium"


def config_path() -> Path:
    """Return the path to mcp.toml (does not require it to exist)."""
    from ..platform_paths import get_config_dir
    return get_config_dir() / "mcp.toml"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class SamplingConfig:
    """Server-initiated LLM sampling capability."""
    enabled: bool = True
    model: str | None = None
    max_tokens_cap: int = 4096
    timeout: int = 30
    max_rpm: int = 10
    max_tool_rounds: int = 5
    log_level: str = "info"
    allowed_models: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SamplingConfig":
        if not data:
            return cls()
        return cls(
            enabled=bool(data.get("enabled", True)),
            model=data.get("model"),
            max_tokens_cap=int(data.get("max_tokens_cap", 4096)),
            timeout=int(data.get("timeout", 30)),
            max_rpm=int(data.get("max_rpm", 10)),
            max_tool_rounds=int(data.get("max_tool_rounds", 5)),
            log_level=str(data.get("log_level", "info")),
            allowed_models=list(data.get("allowed_models", [])),
        )

    def merged_with(self, override: "SamplingConfig | None") -> "SamplingConfig":
        if override is None:
            return self
        return SamplingConfig(
            enabled=override.enabled if override.enabled != SamplingConfig().enabled else self.enabled,
            model=override.model or self.model,
            max_tokens_cap=override.max_tokens_cap or self.max_tokens_cap,
            timeout=override.timeout or self.timeout,
            max_rpm=override.max_rpm or self.max_rpm,
            max_tool_rounds=override.max_tool_rounds or self.max_tool_rounds,
            log_level=override.log_level or self.log_level,
            allowed_models=override.allowed_models or self.allowed_models,
        )


@dataclass
class MCPServerConfig:
    """One MCP server entry."""

    name: str
    # transport: exactly one of (command, url) must be set
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    # operational
    timeout: int = 120
    connect_timeout: int = 60
    # per-tool risk overrides: { tool_name: "low"|"medium"|"danger" }
    risks: dict[str, str] = field(default_factory=dict)
    # sampling overrides (None = inherit defaults)
    sampling: SamplingConfig | None = None
    # whether this server is enabled at all
    enabled: bool = True

    @property
    def transport(self) -> str:
        if self.command and not self.url:
            return "stdio"
        if self.url and not self.command:
            return "http"
        return "invalid"

    def validate(self) -> list[str]:
        """Return list of error strings; empty means valid."""
        errs: list[str] = []
        if self.command and self.url:
            errs.append(f"server {self.name!r}: cannot set both 'command' and 'url'")
        if not self.command and not self.url:
            errs.append(f"server {self.name!r}: must set either 'command' or 'url'")
        if self.timeout <= 0:
            errs.append(f"server {self.name!r}: timeout must be positive")
        for tool, risk in self.risks.items():
            if risk not in VALID_RISKS:
                errs.append(
                    f"server {self.name!r}: tool {tool!r} risk {risk!r} not in "
                    f"{VALID_RISKS}"
                )
        return errs

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "MCPServerConfig":
        sampling_data = data.get("sampling")
        return cls(
            name=name,
            command=data.get("command"),
            args=list(data.get("args", [])),
            env=dict(data.get("env", {})),
            url=data.get("url"),
            headers=dict(data.get("headers", {})),
            timeout=int(data.get("timeout", 120)),
            connect_timeout=int(data.get("connect_timeout", 60)),
            risks=dict(data.get("risks", {})),
            sampling=SamplingConfig.from_dict(sampling_data) if sampling_data else None,
            enabled=bool(data.get("enabled", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.command:
            out["command"] = self.command
            if self.args:
                out["args"] = list(self.args)
            if self.env:
                out["env"] = dict(self.env)
        else:
            out["url"] = self.url
            if self.headers:
                out["headers"] = dict(self.headers)
        if self.timeout != 120:
            out["timeout"] = self.timeout
        if self.connect_timeout != 60:
            out["connect_timeout"] = self.connect_timeout
        if not self.enabled:
            out["enabled"] = False
        if self.risks:
            out["risks"] = dict(self.risks)
        if self.sampling is not None:
            out["sampling"] = {
                k: v for k, v in asdict(self.sampling).items()
                if v not in (None, [], "")
            }
        return out


@dataclass
class MCPConfig:
    """Top-level MCP config."""
    servers: dict[str, MCPServerConfig] = field(default_factory=dict)
    default_timeout: int = 120
    default_connect_timeout: int = 60
    default_risk: str = DEFAULT_RISK
    default_sampling: SamplingConfig = field(default_factory=SamplingConfig)

    def validate(self) -> list[str]:
        errs: list[str] = []
        if self.default_risk not in VALID_RISKS:
            errs.append(
                f"defaults.default_risk {self.default_risk!r} "
                f"not in {VALID_RISKS}"
            )
        for srv in self.servers.values():
            errs.extend(srv.validate())
        return errs

    def effective_sampling(self, server_name: str) -> SamplingConfig:
        srv = self.servers.get(server_name)
        if srv is None:
            return self.default_sampling
        return self.default_sampling.merged_with(srv.sampling)


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------


def load_config(path: Path | None = None) -> MCPConfig:
    """Load MCP config from ``mcp.toml`` (or return defaults if absent)."""
    p = path or config_path()
    if not p.exists():
        log.debug("no mcp.toml at %s; returning empty MCPConfig", p)
        return MCPConfig()

    try:
        with p.open("rb") as f:
            raw = tomllib.load(f)
    except Exception as e:
        log.error("failed to parse %s: %s", p, e)
        return MCPConfig()

    defaults = raw.get("defaults", {}) or {}
    cfg = MCPConfig(
        default_timeout=int(defaults.get("timeout", 120)),
        default_connect_timeout=int(defaults.get("connect_timeout", 60)),
        default_risk=str(defaults.get("default_risk", DEFAULT_RISK)),
        default_sampling=SamplingConfig.from_dict(defaults.get("sampling")),
    )

    servers_raw = raw.get("servers", {}) or {}
    for name, sdata in servers_raw.items():
        if not isinstance(sdata, dict):
            log.warning("mcp.toml: server %r is not a table; skipping", name)
            continue
        srv = MCPServerConfig.from_dict(name, sdata)
        # inherit defaults if unset
        if srv.timeout == 120 and cfg.default_timeout != 120:
            srv.timeout = cfg.default_timeout
        if srv.connect_timeout == 60 and cfg.default_connect_timeout != 60:
            srv.connect_timeout = cfg.default_connect_timeout
        cfg.servers[name] = srv

    errs = cfg.validate()
    for e in errs:
        log.warning("mcp.toml: %s", e)

    return cfg


def save_config(cfg: MCPConfig, path: Path | None = None) -> Path:
    """Write MCP config back to disk in TOML format. Returns the path written."""
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []

    # Defaults
    lines.append("# Cogitum MCP servers")
    lines.append("# Edit risks with `cog mcp risk <server> <tool> low|medium|danger`")
    lines.append("")
    lines.append("[defaults]")
    lines.append(f"timeout = {cfg.default_timeout}")
    lines.append(f"connect_timeout = {cfg.default_connect_timeout}")
    lines.append(f'default_risk = "{cfg.default_risk}"')
    lines.append("")

    # Sampling defaults (only if non-default)
    s = cfg.default_sampling
    default_s = SamplingConfig()
    sampling_changed = (
        s.enabled != default_s.enabled
        or s.model is not None
        or s.max_tokens_cap != default_s.max_tokens_cap
        or s.timeout != default_s.timeout
        or s.max_rpm != default_s.max_rpm
        or s.max_tool_rounds != default_s.max_tool_rounds
        or s.allowed_models
    )
    if sampling_changed:
        lines.append("[defaults.sampling]")
        lines.append(f"enabled = {str(s.enabled).lower()}")
        if s.model:
            lines.append(f'model = "{s.model}"')
        lines.append(f"max_tokens_cap = {s.max_tokens_cap}")
        lines.append(f"timeout = {s.timeout}")
        lines.append(f"max_rpm = {s.max_rpm}")
        lines.append(f"max_tool_rounds = {s.max_tool_rounds}")
        if s.allowed_models:
            arr = ", ".join(f'"{m}"' for m in s.allowed_models)
            lines.append(f"allowed_models = [{arr}]")
        lines.append("")

    # Servers
    for name, srv in cfg.servers.items():
        lines.append(f"[servers.{_safe_key(name)}]")
        if srv.command:
            lines.append(f'command = "{_esc(srv.command)}"')
            if srv.args:
                arr = ", ".join(f'"{_esc(a)}"' for a in srv.args)
                lines.append(f"args = [{arr}]")
            if srv.env:
                pairs = ", ".join(
                    f'{_safe_key(k)} = "{_esc(v)}"' for k, v in srv.env.items()
                )
                lines.append(f"env = {{ {pairs} }}")
        else:
            lines.append(f'url = "{_esc(srv.url or "")}"')
            if srv.headers:
                pairs = ", ".join(
                    f'"{_esc(k)}" = "{_esc(v)}"' for k, v in srv.headers.items()
                )
                lines.append(f"headers = {{ {pairs} }}")
        if srv.timeout != cfg.default_timeout:
            lines.append(f"timeout = {srv.timeout}")
        if srv.connect_timeout != cfg.default_connect_timeout:
            lines.append(f"connect_timeout = {srv.connect_timeout}")
        if not srv.enabled:
            lines.append("enabled = false")

        if srv.risks:
            lines.append("")
            lines.append(f"[servers.{_safe_key(name)}.risks]")
            for tool, risk in sorted(srv.risks.items()):
                lines.append(f'{_safe_key(tool)} = "{risk}"')

        if srv.sampling is not None:
            lines.append("")
            lines.append(f"[servers.{_safe_key(name)}.sampling]")
            lines.append(f"enabled = {str(srv.sampling.enabled).lower()}")
            if srv.sampling.model:
                lines.append(f'model = "{srv.sampling.model}"')

        lines.append("")

    p.write_text("\n".join(lines), encoding="utf-8")
    p.chmod(0o600)  # may contain bearer headers
    return p


def _esc(s: str) -> str:
    """Escape a string for TOML basic string."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _safe_key(s: str) -> str:
    """Quote a TOML key if it contains anything other than [A-Za-z0-9_-]."""
    if not s:
        return '""'
    if all(c.isalnum() or c in "_-" for c in s):
        return s
    return f'"{_esc(s)}"'
