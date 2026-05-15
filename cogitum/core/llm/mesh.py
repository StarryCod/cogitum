"""
Mesh — the facade everything outside `core/llm/` talks to.

Responsibilities:
  * Hold the registry of `Provider` instances (loaded from config).
  * Resolve a user-typed model id (alias, slug, qualified `provider/model`)
    to one or more `(Provider, ModelConfig)` candidates.
  * Acquire a `KeyLease` from the chosen provider and stream the response.
  * Failover: on transient errors / no-key-available / specific model
    unavailable, walk a fallback chain transparently.

Public surface is intentionally tiny — `stream()` and `list_models()` —
so the agent loop and the TUI don't see plumbing.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator, Iterable

from ..events import ChunkKind, Message, StreamChunk
from .keypool import NoKeyAvailable
from .provider import CompletionRequest, ModelConfig, Provider

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ResolvedModel:
    """One concrete (provider, model) pair after resolution."""
    provider: Provider
    model: ModelConfig

    @property
    def qualified_id(self) -> str:
        return f"{self.provider.id}/{self.model.id}"


@dataclass(slots=True)
class StreamRequest:
    """High-level request the agent loop hands to the mesh."""
    messages: list[Message]
    model: str                                  # id, alias, or "provider/model"
    system: str | None = None
    tools: list[dict] = field(default_factory=list)
    tool_choice: str | dict | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stop: list[str] = field(default_factory=list)
    reasoning_effort: str | None = None
    json_schema: dict | None = None
    extra: dict = field(default_factory=dict)
    # Failover chain. Each element is a model id/alias/qualified id.
    # Mesh tries them in order if the primary fails.
    fallback_models: tuple[str, ...] = ()


class ModelNotFound(LookupError):
    """No provider in the mesh exposes the requested model."""


class Mesh:
    """Aggregate of providers."""

    providers: dict[str, Provider]

    def __init__(self, providers: Iterable[Provider]) -> None:
        self.providers = {}
        for p in providers:
            if not p.config.enabled:
                continue
            self.providers[p.id] = p

    # ------------------------------------------------------------------
    # discovery
    # ------------------------------------------------------------------

    def list_resolved(self) -> list[ResolvedModel]:
        out: list[ResolvedModel] = []
        for p in self.providers.values():
            for m in p.list_models():
                out.append(ResolvedModel(provider=p, model=m))
        return out

    def resolve(self, model_ref: str) -> list[ResolvedModel]:
        """Return all (provider, model) pairs matching the reference.

        Matching rules (in order):
          1. Qualified `provider/model` — exact, single result.
          2. Provider-prefixed alias `provider:alias` — exact within provider.
          3. Model id case-insensitive across all providers.
          4. Alias match across all providers.
        Multiple matches mean the caller picks one (or the picker UI does).
        """
        ref = model_ref.strip()
        if not ref:
            return []

        if "/" in ref:
            pid, _, mid = ref.partition("/")
            p = self.providers.get(pid)
            if not p:
                return []
            m = p.find_model(mid)
            return [ResolvedModel(p, m)] if m else []

        if ":" in ref:
            pid, _, alias = ref.partition(":")
            p = self.providers.get(pid)
            if not p:
                return []
            m = p.find_model(alias)
            return [ResolvedModel(p, m)] if m else []

        out: list[ResolvedModel] = []
        for p in self.providers.values():
            m = p.find_model(ref)
            if m is not None:
                out.append(ResolvedModel(p, m))
        return out

    # ------------------------------------------------------------------
    # streaming
    # ------------------------------------------------------------------

    async def stream(self, req: StreamRequest) -> AsyncIterator[StreamChunk]:
        """Stream chunks for `req`. Tries primary then fallback models."""
        attempts = (req.model, *req.fallback_models)
        last_error: str | None = None

        for ref in attempts:
            candidates = self.resolve(ref)
            if not candidates:
                last_error = f"model not found: {ref!r}"
                continue
            # Try every candidate (= every provider exposing this model).
            for resolved in candidates:
                async for chunk in self._try_one(resolved, req):
                    yield chunk
                    if chunk.kind == ChunkKind.STOP:
                        return
                    if chunk.kind == ChunkKind.ERROR:
                        last_error = chunk.error
                        break  # next candidate

        # If we drained everything and never hit STOP, bubble a final error.
        yield StreamChunk(
            kind=ChunkKind.ERROR,
            error=f"all providers exhausted: {last_error or 'unknown'}",
        )
        yield StreamChunk(kind=ChunkKind.STOP, stop_reason="error")

    async def _try_one(
        self, resolved: ResolvedModel, req: StreamRequest
    ) -> AsyncIterator[StreamChunk]:
        provider = resolved.provider
        cr = CompletionRequest(
            model=resolved.model,
            messages=req.messages,
            system=req.system,
            tools=req.tools,
            tool_choice=req.tool_choice,
            temperature=req.temperature,
            top_p=req.top_p,
            max_tokens=req.max_tokens,
            stop=list(req.stop),
            stream=True,
            reasoning_effort=req.reasoning_effort,
            json_schema=req.json_schema,
            extra=dict(req.extra),
        )

        try:
            lease = await provider.pool.lease()
        except NoKeyAvailable as e:
            yield StreamChunk(kind=ChunkKind.ERROR, error=f"{provider.id}: {e}")
            return

        async with lease:
            try:
                async for chunk in provider.stream(cr, lease):
                    # Stamp provider/model into chunks that are about to be
                    # rendered as messages. (Caller may re-stamp later.)
                    yield chunk
            except asyncio.CancelledError:
                yield StreamChunk(
                    kind=ChunkKind.ERROR,
                    error=f"{provider.id}: cancelled",
                )
                yield StreamChunk(kind=ChunkKind.STOP, stop_reason="interrupted")
                raise
            except Exception as e:  # noqa: BLE001
                logger.exception("provider %s stream raised", provider.id)
                yield StreamChunk(
                    kind=ChunkKind.ERROR,
                    error=f"{provider.id}: {e}",
                )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        await asyncio.gather(
            *(p.aclose() for p in self.providers.values()),
            return_exceptions=True,
        )


__all__ = [
    "Mesh",
    "ResolvedModel",
    "StreamRequest",
    "ModelNotFound",
]
