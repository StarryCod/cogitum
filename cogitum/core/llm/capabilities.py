"""Capability flags & format identifiers used across the mesh."""

from __future__ import annotations

from enum import Flag, auto
from typing import Literal


class Capability(Flag):
    """What a model can do. Used for filtering in the picker and for
    feature-gating at request time (e.g. don't send images if !VISION)."""
    NONE = 0
    TEXT = auto()
    VISION = auto()
    AUDIO_INPUT = auto()
    REASONING = auto()       # explicit thinking blocks
    TOOLS = auto()           # native tool/function calling
    JSON_MODE = auto()       # structured JSON output
    STREAMING = auto()
    CACHING = auto()         # prompt caching (Anthropic-style)
    LONG_CONTEXT = auto()    # >128k

    @classmethod
    def from_strings(cls, items: list[str]) -> "Capability":
        result = cls.NONE
        for item in items:
            try:
                result |= cls[item.upper().replace("-", "_")]
            except KeyError:
                pass
        return result

    def to_strings(self) -> list[str]:
        return [c.name.lower() for c in Capability if c != Capability.NONE and c in self]


# Provider API formats. The mesh dispatches to the right adapter based on this.
ApiFormat = Literal[
    "openai_compat",        # OpenAI / Together / Groq / DeepInfra / Fireworks / Cerebras / Hyperbolic / SambaNova / Canopywave / OpenRouter / vLLM / Ollama
    "anthropic_native",     # Anthropic Messages API
    "google_genai",         # Gemini native
    "mistral_native",       # Mistral La Plateforme
    "cohere_native",        # Cohere Chat API
    "subscription",         # cookie/session-based web subscriptions
    "custom",               # user-defined adapter via plugin
]


# How auth is sent.
AuthMode = Literal[
    "bearer",          # Authorization: Bearer <key>
    "x_api_key",       # x-api-key: <key>  (Anthropic style)
    "header_custom",   # custom header name from config
    "query_param",     # ?key=<key>
    "subscription",    # cookies + session headers, no static key
    "none",
]
