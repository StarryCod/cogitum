"""
Configuration loader for the LLM mesh.

User config lives at ~/.config/cogitum/providers.toml. On first run we
seed it with sensible defaults (Canopywave + Kimi K2.6, Anthropic API,
Anthropic Pro/Max OAuth, ChatGPT Plus/Pro OAuth, OpenAI API). Users edit
either by hand, via `cog setup`, or via the in-TUI configurator.

The loader returns a fully wired `Mesh`. Provider construction picks the
right `Provider` subclass based on `format`.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from .capabilities import Capability
from .credentials import CredentialResolver, default_resolver
from .keypool import KeyPool
from .mesh import Mesh
from .provider import KeyConfig, ModelConfig, Provider, ProviderConfig
from .providers.anthropic_native import AnthropicProvider
from .providers.openai_compat import OpenAICompatProvider

logger = logging.getLogger(__name__)


_CONFIG_DIR = Path(
    os.environ.get("COGITUM_CONFIG_DIR")
    or os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
) / "cogitum"

_PROVIDERS_PATH = _CONFIG_DIR / "providers.toml"
_SETTINGS_PATH = _CONFIG_DIR / "settings.toml"


# ---------------------------------------------------------------------------
# Provider class dispatch
# ---------------------------------------------------------------------------

_PROVIDER_CLASSES: dict[str, type[Provider]] = {
    "openai_compat": OpenAICompatProvider,
    "anthropic_native": AnthropicProvider,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_mesh(
    *,
    config_path: Path | None = None,
    resolver: CredentialResolver | None = None,
) -> Mesh:
    """Load the user's mesh, seeding the config file on first run."""
    path = config_path or _PROVIDERS_PATH
    if not path.exists():
        seed_default_config(path)
    cfg = _read_toml(path)
    resolver = resolver or default_resolver()
    providers: list[Provider] = []
    for pid, raw in (cfg.get("providers") or {}).items():
        try:
            pcfg = _provider_config_from_dict(pid, raw)
        except Exception as e:  # noqa: BLE001
            logger.warning("provider %s skipped: bad config (%s)", pid, e)
            continue
        if not pcfg.enabled:
            continue
        cls = _PROVIDER_CLASSES.get(pcfg.format)
        if cls is None:
            logger.warning(
                "provider %s skipped: unsupported format %r", pid, pcfg.format
            )
            continue
        pool = KeyPool.from_configs(
            pcfg.keys, provider_id=pid, resolver=resolver
        )
        if pool.size == 0:
            logger.warning(
                "provider %s skipped: no usable keys (auth not configured?)", pid
            )
            continue
        providers.append(cls(pcfg, pool))
    return Mesh(providers)


def load_settings(path: Path | None = None) -> dict[str, Any]:
    """Read settings.toml (defaults if missing)."""
    p = path or _SETTINGS_PATH
    if not p.exists():
        return {
            "default_model": "kimi-k2.6",
            "default_reasoning_effort": "medium",
        }
    return _read_toml(p)


def write_settings(data: dict[str, Any], path: Path | None = None) -> None:
    p = path or _SETTINGS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_dump_toml(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _read_toml(path: Path) -> dict[str, Any]:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _provider_config_from_dict(pid: str, raw: dict[str, Any]) -> ProviderConfig:
    keys: list[KeyConfig] = []
    for kid, kraw in (raw.get("keys") or {}).items():
        keys.append(KeyConfig(
            id=kid,
            secret_ref=str(kraw["secret_ref"]),
            weight=float(kraw.get("weight", 1.0)),
            rpm_limit=kraw.get("rpm_limit"),
            tpm_limit=kraw.get("tpm_limit"),
            rpd_limit=kraw.get("rpd_limit"),
            enabled=bool(kraw.get("enabled", True)),
            notes=str(kraw.get("notes", "")),
            extra_headers=dict(kraw.get("extra_headers") or {}),
        ))

    models: list[ModelConfig] = []
    for mid, mraw in (raw.get("models") or {}).items():
        caps = Capability.from_strings(list(mraw.get("capabilities") or ["text"]))
        if Capability.STREAMING not in caps:
            caps |= Capability.STREAMING
        if int(mraw.get("context_window", 0)) >= 128_000:
            caps |= Capability.LONG_CONTEXT
        models.append(ModelConfig(
            id=mid,
            display=str(mraw.get("display", "")),
            aliases=tuple(mraw.get("aliases") or ()),
            capabilities=caps,
            context_window=int(mraw.get("context_window", 8192)),
            max_output_tokens=int(mraw.get("max_output_tokens", 4096)),
            cost_input=float(mraw.get("cost_input", 0.0)),
            cost_output=float(mraw.get("cost_output", 0.0)),
            cost_cache_read=float(mraw.get("cost_cache_read", 0.0)),
            cost_cache_write=float(mraw.get("cost_cache_write", 0.0)),
            reasoning_effort_map=dict(mraw.get("reasoning_effort_map") or {}),
            default_reasoning_effort=mraw.get("default_reasoning_effort"),
            extra=dict(mraw.get("extra") or {}),
        ))

    return ProviderConfig(
        id=pid,
        name=str(raw.get("name", pid)),
        format=str(raw.get("format", "openai_compat")),  # type: ignore[arg-type]
        base_url=str(raw["base_url"]),
        auth=str(raw.get("auth", "bearer")),  # type: ignore[arg-type]
        auth_header_name=raw.get("auth_header_name"),
        auth_query_param=raw.get("auth_query_param"),
        keys=keys,
        models=models,
        timeout_s=float(raw.get("timeout_s", 600.0)),
        connect_timeout_s=float(raw.get("connect_timeout_s", 30.0)),
        fallback_providers=tuple(raw.get("fallback_providers") or ()),
        routing_strategy=raw.get("routing_strategy"),
        enabled=bool(raw.get("enabled", True)),
        extra=dict(raw.get("extra") or {}),
    )


# ---------------------------------------------------------------------------
# TOML dumping (no third-party dep — write enough to round-trip our config)
# ---------------------------------------------------------------------------

def _dump_toml(data: dict[str, Any]) -> str:
    out: list[str] = []
    _dump_table(out, data, prefix="")
    return "\n".join(out).strip() + "\n"


def _dump_table(out: list[str], data: dict[str, Any], prefix: str) -> None:
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    if scalars:
        if prefix:
            out.append(f"[{prefix}]")
        for k, v in scalars.items():
            out.append(f"{k} = {_toml_value(v)}")
        out.append("")
    for k, v in tables.items():
        sub = f"{prefix}.{k}" if prefix else k
        _dump_table(out, v, sub)


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        body = ", ".join(f"{k} = {_toml_value(val)}" for k, val in v.items())
        return "{ " + body + " }"
    return _toml_string(str(v))


def _toml_string(s: str) -> str:
    if "\n" in s:
        escaped = s.replace("\\", "\\\\").replace('"""', '\\"""')
        return f'"""{escaped}"""'
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------

_DEFAULT_TOML = """\
# Cogitum providers configuration.
# Edit by hand, via `cog setup`, or through the TUI configurator.
#
# Secrets use reference schemes: env:VAR, keyring:service:user, vault:id,
# file:/path, plain:value (dev only). OAuth subscriptions use oauth:<id>.

# ------------------------------------------------------------------
# Canopywave — exotic OpenAI-compat host serving Moonshot Kimi K2.6.
# ------------------------------------------------------------------
[providers.canopywave]
name = "CanopyWave"
format = "openai_compat"
base_url = "https://inference.canopywave.io/v1"
auth = "bearer"

[providers.canopywave.keys.primary]
secret_ref = "plain:2hC_uRJSoTM65hNwNvetB7X5LWwbIrkZ2MEsqkZizf4"
notes = "imported from ~/.pi/agent/extensions/canopywave.ts"

[providers.canopywave.models."moonshotai/kimi-k2.6"]
display = "Kimi K2.6"
aliases = ["kimi", "kimi-k2.6", "k2.6"]
capabilities = ["text", "vision", "reasoning", "tools"]
context_window = 200000
max_output_tokens = 128000
reasoning_effort_map = { off = "off", minimal = "off", low = "low", medium = "medium", high = "high", xhigh = "high" }
default_reasoning_effort = "high"

# ------------------------------------------------------------------
# Anthropic — both API key and Claude Pro/Max subscription, side-by-side.
# Disable whichever you don't use.
# ------------------------------------------------------------------
[providers.anthropic]
name = "Anthropic API"
format = "anthropic_native"
base_url = "https://api.anthropic.com"
auth = "x_api_key"
enabled = false

[providers.anthropic.keys.primary]
secret_ref = "env:ANTHROPIC_API_KEY"

[providers.anthropic.models."claude-opus-4-5"]
display = "Claude Opus 4.5"
aliases = ["opus", "opus-4.5"]
capabilities = ["text", "vision", "reasoning", "tools", "caching"]
context_window = 200000
max_output_tokens = 32000
reasoning_effort_map = { low = "low", medium = "medium", high = "high", xhigh = "xhigh" }

[providers.anthropic.models."claude-sonnet-4-5"]
display = "Claude Sonnet 4.5"
aliases = ["sonnet", "sonnet-4.5"]
capabilities = ["text", "vision", "reasoning", "tools", "caching"]
context_window = 200000
max_output_tokens = 16000

# ------------------------------------------------------------------
# Anthropic Claude Pro/Max — subscription auth via OAuth.
# Run `cog setup` to authenticate, this entry will then activate.
# ------------------------------------------------------------------
[providers."anthropic-pro"]
name = "Claude Pro/Max"
format = "anthropic_native"
base_url = "https://api.anthropic.com"
auth = "bearer"
enabled = false

[providers."anthropic-pro".keys.subscription]
secret_ref = "oauth:anthropic"

[providers."anthropic-pro".models."claude-opus-4-5"]
display = "Claude Opus 4.5 (Pro)"
aliases = ["opus-pro"]
capabilities = ["text", "vision", "reasoning", "tools", "caching"]
context_window = 200000
max_output_tokens = 32000

[providers."anthropic-pro".models."claude-sonnet-4-5"]
display = "Claude Sonnet 4.5 (Pro)"
aliases = ["sonnet-pro"]
capabilities = ["text", "vision", "reasoning", "tools", "caching"]
context_window = 200000
max_output_tokens = 16000

# ------------------------------------------------------------------
# OpenAI direct API.
# ------------------------------------------------------------------
[providers.openai]
name = "OpenAI API"
format = "openai_compat"
base_url = "https://api.openai.com/v1"
auth = "bearer"
enabled = false

[providers.openai.keys.primary]
secret_ref = "env:OPENAI_API_KEY"

[providers.openai.models."gpt-5"]
display = "GPT-5"
aliases = ["gpt5"]
capabilities = ["text", "vision", "reasoning", "tools"]
context_window = 256000
max_output_tokens = 64000

[providers.openai.models."gpt-5-mini"]
display = "GPT-5 mini"
aliases = ["gpt5-mini", "mini"]
capabilities = ["text", "vision", "tools"]
context_window = 256000
max_output_tokens = 32000

# ------------------------------------------------------------------
# OpenRouter — easiest way to get hundreds of models behind one key.
# ------------------------------------------------------------------
[providers.openrouter]
name = "OpenRouter"
format = "openai_compat"
base_url = "https://openrouter.ai/api/v1"
auth = "bearer"
enabled = false

[providers.openrouter.extra.headers]
"HTTP-Referer" = "https://github.com/Starred/Cogitum"
"X-Title" = "Cogitum"

[providers.openrouter.keys.primary]
secret_ref = "env:OPENROUTER_API_KEY"

[providers.openrouter.models."anthropic/claude-opus-4-5"]
display = "Claude Opus 4.5 (OR)"
aliases = ["or-opus"]
capabilities = ["text", "vision", "reasoning", "tools"]
context_window = 200000
max_output_tokens = 32000

[providers.openrouter.models."x-ai/grok-4"]
display = "Grok 4"
aliases = ["grok4", "grok"]
capabilities = ["text", "tools"]
context_window = 128000
max_output_tokens = 16000

[providers.openrouter.models."deepseek/deepseek-v3.2"]
display = "DeepSeek V3.2"
aliases = ["dsv3", "deepseek"]
capabilities = ["text", "tools", "reasoning"]
context_window = 128000
max_output_tokens = 16000
"""


def seed_default_config(path: Path) -> None:
    """Write the default providers.toml. Refuses if file exists."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_DEFAULT_TOML, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


__all__ = [
    "load_mesh",
    "load_settings",
    "write_settings",
    "seed_default_config",
    "_PROVIDERS_PATH",
    "_SETTINGS_PATH",
]


# silence unused
_ = asdict
