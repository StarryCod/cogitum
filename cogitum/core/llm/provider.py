"""
Provider abstraction & config dataclasses.

A `Provider` is the smallest unit that knows how to talk to one HTTP
endpoint (or one subscription session). It owns N keys via `KeyPool`,
exposes its models, and converts our normalized `Message` / `StreamChunk`
domain into and out of whatever wire format it speaks.

The mesh never instantiates HTTP clients directly — it always goes
through a Provider.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator

from .capabilities import ApiFormat, AuthMode, Capability

if TYPE_CHECKING:
    from ..events import Message, StreamChunk
    from .keypool import KeyPool, KeyLease


# ---------------------------------------------------------------------------
# Config dataclasses (deserialized from providers.toml)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class KeyConfig:
    """One credential entry inside a provider's pool."""
    id: str                             # short label, e.g. "primary", "team-2"
    secret_ref: str                     # how to resolve: "plain:<value>", "env:VAR", "keyring:service:user", "vault:<id>"
    weight: float = 1.0                 # for weighted routing (higher = preferred)
    rpm_limit: int | None = None        # requests per minute (per key)
    tpm_limit: int | None = None        # tokens per minute
    rpd_limit: int | None = None        # requests per day
    enabled: bool = True
    notes: str = ""

    # Optional override for orgs/projects (OpenAI org, Anthropic-Beta, etc.)
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ModelConfig:
    """One model surface exposed by a provider."""
    id: str                             # wire id, e.g. "moonshotai/kimi-k2.6"
    display: str = ""                   # pretty name for UI
    aliases: tuple[str, ...] = ()       # short forms: "kimi", "k2.6"
    capabilities: Capability = Capability.TEXT | Capability.STREAMING
    context_window: int = 8192
    max_output_tokens: int = 4096
    # Cost is per 1M tokens (USD). Used only for display.
    cost_input: float = 0.0
    cost_output: float = 0.0
    cost_cache_read: float = 0.0
    cost_cache_write: float = 0.0
    # Reasoning-specific.
    reasoning_effort_map: dict[str, str] = field(default_factory=dict)
    default_reasoning_effort: str | None = None
    # Provider-specific knobs the adapter understands.
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.display or self.id


@dataclass(slots=True)
class ProviderConfig:
    """Everything needed to instantiate a Provider."""
    id: str                             # short slug: "canopywave", "anthropic", "openrouter"
    name: str                           # display name
    format: ApiFormat
    base_url: str
    auth: AuthMode = "bearer"
    auth_header_name: str | None = None         # for header_custom
    auth_query_param: str | None = None         # for query_param
    keys: list[KeyConfig] = field(default_factory=list)
    models: list[ModelConfig] = field(default_factory=list)
    # Hard caps applied at provider level (sum across all keys).
    timeout_s: float = 600.0
    connect_timeout_s: float = 30.0
    # Per-provider override for the agent's max_tokens cap. The agent
    # uses cfg.max_tokens by default (32K), but some providers either
    # support more (Claude Sonnet 4 → 64K, GPT-5 → 128K) or impose a
    # hard ceiling lower than 32K (DeepSeek-R1 caps at 8K). Setting
    # this to a positive int makes the mesh substitute it into the
    # outgoing StreamRequest.max_tokens whenever it routes through
    # this provider. 0 = use agent default.
    max_tokens: int = 0
    # User-defined fallback chain: if every key/model on this provider
    # fails, mesh tries these provider ids in order.
    fallback_providers: tuple[str, ...] = ()
    # Routing strategy override; mesh-level default applies if None.
    routing_strategy: str | None = None
    enabled: bool = True
    # Adapter-specific extras (anthropic-version header, openai org, …).
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CompletionRequest:
    """Normalized request the mesh hands to a Provider."""
    model: ModelConfig
    messages: list["Message"]
    system: str | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)     # JSON-schema tool defs
    tool_choice: str | dict[str, Any] | None = None               # "auto" | "none" | {"name": "..."}
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stop: list[str] = field(default_factory=list)
    stream: bool = True
    reasoning_effort: str | None = None
    json_schema: dict[str, Any] | None = None
    # Free-form passthrough for adapter-specific knobs.
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Provider ABC
# ---------------------------------------------------------------------------

class Provider(ABC):
    """Adapter for one upstream API."""

    config: ProviderConfig
    pool: "KeyPool"

    def __init__(self, config: ProviderConfig, pool: "KeyPool") -> None:
        self.config = config
        self.pool = pool

    # --- discovery ----------------------------------------------------------

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def name(self) -> str:
        return self.config.name

    def list_models(self) -> list[ModelConfig]:
        return [m for m in self.config.models]

    def find_model(self, model_id_or_alias: str) -> ModelConfig | None:
        needle = model_id_or_alias.lower()
        for m in self.config.models:
            if m.id.lower() == needle or needle in (a.lower() for a in m.aliases):
                return m
        return None

    # --- I/O ----------------------------------------------------------------

    @abstractmethod
    async def stream(
        self,
        request: CompletionRequest,
        lease: "KeyLease",
    ) -> AsyncIterator["StreamChunk"]:
        """Yield StreamChunk events. Must close the lease via `lease.release()`
        even on exception (recommended pattern: `async with lease:`)."""
        raise NotImplementedError
        # pragma: no cover  (this yield makes the type checker accept the signature)
        if False:
            yield  # type: ignore[unreachable]

    async def aclose(self) -> None:
        """Release any underlying HTTP clients. Default: no-op."""
        return None

    # --- helpers ------------------------------------------------------------

    def supports(self, model: ModelConfig, cap: Capability) -> bool:
        return cap in model.capabilities
