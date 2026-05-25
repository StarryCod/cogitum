"""Helpers to convert between our domain `Message` types and provider
wire formats. Kept separate so adapters stay thin.
"""

from __future__ import annotations

import json
from typing import Any

from ..events import (
    ImagePart,
    Message,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolResultPart,
)


# ---------------------------------------------------------------------------
# OpenAI chat-completions
# ---------------------------------------------------------------------------

def normalize_messages_openai(
    messages: list[Message],
    *,
    system: str | None = None,
) -> list[dict[str, Any]]:
    """Render our Messages into OpenAI chat-completions wire format."""
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})

    for msg in messages:
        if msg.role == "tool":
            for part in msg.parts:
                if isinstance(part, ToolResultPart):
                    out.append({
                        "role": "tool",
                        "tool_call_id": part.tool_call_id,
                        "content": part.content,
                    })
            continue

        role = msg.role
        content_parts: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []
        plain_text_chunks: list[str] = []

        for part in msg.parts:
            if isinstance(part, TextPart):
                plain_text_chunks.append(part.text)
                content_parts.append({"type": "text", "text": part.text})
            elif isinstance(part, ImagePart):
                if part.url:
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {"url": part.url},
                    })
                elif part.data:
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{part.mime};base64,{part.data}"
                        },
                    })
            elif isinstance(part, ThinkingPart):
                continue
            elif isinstance(part, ToolCallPart):
                tool_calls.append({
                    "id": part.id,
                    "type": "function",
                    "function": {
                        "name": part.name,
                        "arguments": json.dumps(part.arguments, ensure_ascii=False),
                    },
                })

        wire: dict[str, Any] = {"role": role}
        has_image = any(p.get("type") == "image_url" for p in content_parts)
        if has_image:
            wire["content"] = content_parts
        else:
            joined = "".join(plain_text_chunks)
            # F12 fix: when an assistant message carries tool_calls but no
            # text body, some OpenAI-compat providers (older llama.cpp HTTP
            # servers, a handful of gateways) reject `content: ""` with
            # HTTP 400 even though the spec allows it. Per the OpenAI
            # spec, `content: null` is valid and unambiguous when
            # tool_calls are present, so emit None instead of an empty
            # string. Plain assistant text (no tool_calls, no text) keeps
            # the empty-string shape — that's the legacy behavior other
            # callers may rely on.
            if role == "assistant" and tool_calls and not joined:
                wire["content"] = None
            else:
                wire["content"] = joined
        if tool_calls:
            wire["tool_calls"] = tool_calls
        out.append(wire)
    return out


# ---------------------------------------------------------------------------
# Anthropic Messages
# ---------------------------------------------------------------------------

def normalize_messages_anthropic(
    messages: list[Message],
    *,
    system: str | None = None,
    current_model: str | None = None,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Render messages for Anthropic. Returns (system, messages).

    ``current_model`` is the model id that will receive the request.
    When a ThinkingPart was produced by a different model (the user
    ran ``/model`` mid-conversation), its cryptographic signature is
    no longer valid — Anthropic returns HTTP 400 on cross-model
    signature replay. We drop the signature in that case so the
    thinking content is preserved as text but the (now-unsigned)
    block is filtered out by the existing ``if part.signature``
    guard below. See audit_r2_history.md GAP-9.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "system":
            continue

        if msg.role == "tool":
            content_blocks = []
            for part in msg.parts:
                if isinstance(part, ToolResultPart):
                    # Anthropic rejects messages with empty tool_result
                    # content blocks (HTTP 400). Substitute a placeholder
                    # so the wire stays valid even if a tool legitimately
                    # produced no output.
                    body = part.content
                    if body is None or (isinstance(body, str) and not body.strip()):
                        body = "(no output)"
                    content_blocks.append({
                        "type": "tool_result",
                        "tool_use_id": part.tool_call_id,
                        "content": body,
                        "is_error": part.is_error,
                    })
            if content_blocks:
                out.append({"role": "user", "content": content_blocks})
            continue

        blocks: list[dict[str, Any]] = []
        for part in msg.parts:
            if isinstance(part, TextPart):
                # Anthropic also rejects empty text blocks on assistant
                # messages, so coerce them to a single space placeholder
                # rather than dropping (drop would corrupt block ordering
                # vs sibling tool_use blocks if any).
                text = part.text
                if msg.role == "assistant" and (text is None or not text.strip()):
                    text = " "
                blocks.append({"type": "text", "text": text})
            elif isinstance(part, ThinkingPart):
                # GAP-9 (audit_r2_history.md): if the part was produced
                # by a different model than the one we're calling now,
                # the signature is cryptographically bound to the old
                # model and Anthropic will 400 with "signature from a
                # different model". Drop the signature so the block is
                # filtered out below (Anthropic refuses unsigned
                # thinking blocks anyway, but the run survives the
                # switch instead of crashing).
                sig = part.signature
                if (
                    sig
                    and current_model
                    and part.model
                    and part.model != current_model
                ):
                    sig = None
                if sig:
                    blocks.append({
                        "type": "thinking",
                        "thinking": part.text,
                        "signature": sig,
                    })
            elif isinstance(part, ImagePart):
                if part.data:
                    blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": part.mime,
                            "data": part.data,
                        },
                    })
                elif part.url:
                    blocks.append({
                        "type": "image",
                        "source": {"type": "url", "url": part.url},
                    })
            elif isinstance(part, ToolCallPart):
                blocks.append({
                    "type": "tool_use",
                    "id": part.id,
                    "name": part.name,
                    "input": part.arguments,
                })

        if msg.role == "assistant" and not blocks:
            # R3 fix (audit GAP-5a): an assistant message that contains
            # ONLY ThinkingPart entries with no signature drops every
            # part above (Anthropic refuses unsigned thinking blocks),
            # leaving an empty content[] which Anthropic 400-rejects.
            # Mirror hermes-agent/agent/anthropic_adapter.py:1560-1563
            # by injecting an "(empty)" sentinel so the wire stays
            # valid. The sentinel only appears on this rare branch —
            # well-formed assistant turns with text or tool_use are
            # unaffected.
            blocks = [{"type": "text", "text": "(empty)"}]

        if blocks:
            out.append({"role": msg.role, "content": blocks})

    return system, out
