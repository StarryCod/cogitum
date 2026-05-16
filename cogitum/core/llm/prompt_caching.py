"""Prompt caching — inject cache_control breakpoints for supported providers.

Strategy: system_and_3 — up to 4 breakpoints:
  1. System prompt (always cached — it never changes between turns)
  2. Last 3 non-system messages (sliding window — recent context stays hot)

This reduces input token costs by ~75% on multi-turn conversations.
Anthropic (native + OpenRouter), Qwen/DashScope, and omniroute all honor
cache_control markers on the OpenAI chat-completions wire format.

Pure functions — no state, no side effects.
"""

from __future__ import annotations

from typing import Any


_CACHE_MARKER = {"type": "ephemeral"}


def should_cache(base_url: str, model: str) -> bool:
    """Determine if this provider/model combo supports prompt caching.

    Decision is made by base_url, NOT by model name. Many providers serve
    Claude/Qwen models without honoring cache_control (Cerebras, Groq,
    DeepInfra, vLLM-local, plain OpenAI-compat) — sending the marker to
    them yields HTTP 400. Only enable for endpoints we have verified.

    Currently enabled for:
      - Anthropic native API (api.anthropic.com)
      - OpenRouter (openrouter.ai)
      - DashScope (Qwen's official cloud)
      - Omniroute / Kiro proxies
    """
    url_lower = (base_url or "").lower()
    model_lower = (model or "").lower()

    # Anthropic native API
    if "api.anthropic.com" in url_lower:
        return True

    # OpenRouter — supports cache_control for Claude/Qwen routes
    if "openrouter.ai" in url_lower:
        return True

    # DashScope (Qwen official) — supports caching for qwen models
    if "dashscope" in url_lower and "qwen" in model_lower:
        return True

    # Omniroute / Kiro — proxy fronts that pass cache_control through.
    # Detect by URL marker OR by kiro model prefix (kr/, kc/, kiro-*) since
    # users sometimes self-host on plain localhost ports.
    if "omniroute" in url_lower or "kiro" in url_lower:
        return True
    if model_lower.startswith(("kr/", "kc/", "kiro-")):
        return True

    # Everything else (Cerebras, Groq, DeepInfra, vLLM, plain OpenAI,
    # CanopyWave, Fireworks, etc.) — no caching, marker would 400.
    return False


def apply_cache_control(
    messages: list[dict[str, Any]],
    *,
    max_breakpoints: int = 4,
) -> list[dict[str, Any]]:
    """Apply cache_control breakpoints to messages (OpenAI wire format).

    Places markers on:
      1. System message (if present)
      2. Last N non-system messages (to fill remaining breakpoints)

    The marker is placed on the last content block of each message.
    Returns a shallow copy — original messages are not mutated.
    """
    if not messages:
        return messages

    # Shallow copy outer list, deep copy only messages we modify
    result = list(messages)
    breakpoints_used = 0

    # 1. Cache system prompt
    if result[0].get("role") == "system":
        result[0] = _add_marker(result[0])
        breakpoints_used += 1

    # 2. Cache last N non-system messages
    remaining = max_breakpoints - breakpoints_used
    non_sys_indices = [
        i for i in range(len(result))
        if result[i].get("role") != "system"
    ]

    for idx in non_sys_indices[-remaining:]:
        result[idx] = _add_marker(result[idx])

    return result


def _add_marker(msg: dict[str, Any]) -> dict[str, Any]:
    """Add cache_control marker to a message. Returns a copy."""
    msg = dict(msg)  # shallow copy
    content = msg.get("content")

    if content is None or content == "":
        # Empty content — put marker on message level
        msg["cache_control"] = _CACHE_MARKER
        return msg

    if isinstance(content, str):
        # String content — wrap in content block with marker
        msg["content"] = [
            {"type": "text", "text": content, "cache_control": _CACHE_MARKER}
        ]
        return msg

    if isinstance(content, list) and content:
        # List of content blocks — mark the last one
        content = list(content)  # shallow copy
        last = dict(content[-1])  # copy last block
        last["cache_control"] = _CACHE_MARKER
        content[-1] = last
        msg["content"] = content
        return msg

    return msg
