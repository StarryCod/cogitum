"""Registry of OAuth providers Cogitum knows about."""

from __future__ import annotations

from .anthropic import anthropic_oauth
from .openai_codex import openai_codex_oauth
from .types import OAuthProvider


REGISTRY: dict[str, OAuthProvider] = {
    anthropic_oauth.id: anthropic_oauth,
    openai_codex_oauth.id: openai_codex_oauth,
}


def get_provider(provider_id: str) -> OAuthProvider | None:
    return REGISTRY.get(provider_id)


def list_providers() -> list[OAuthProvider]:
    return list(REGISTRY.values())


__all__ = ["REGISTRY", "get_provider", "list_providers"]
