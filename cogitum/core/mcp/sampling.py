"""
cogitum.core.mcp.sampling
~~~~~~~~~~~~~~~~~~~~~~~~~

Bridge MCP server-initiated ``sampling/createMessage`` requests to the
Cogitum :class:`Mesh`.

Usage
-----
At app startup, after the Mesh is built::

    from cogitum.core.mcp import discover_mcp_tools
    from cogitum.core.mcp.sampling import build_sampling_callback

    cb = build_sampling_callback(mesh, settings.model)
    discover_mcp_tools(REGISTRY, sampling_callback=cb)

The callback receives ``(server_name, request_dict)`` produced by
``MCPManager._build_sampling_handler`` and returns
``{"text": "...", "model": "...", "stop_reason": "endTurn"}``.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any, Awaitable, Callable

from ..events import ChunkKind, Message, TextPart
from .security import redact_secrets

log = logging.getLogger(__name__)

SamplingCallback = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

# Reflection guard. An MCP server can request sampling, which drives the
# Mesh, which can dispatch tool calls, which can include MCP tools, which
# can re-enter sampling. Without a depth bound this nests until OOM or
# until the user notices a runaway bill. We cap at 4 nested entries —
# enough for a handful of legitimate "ask the model to summarise the
# tool result" flows, far below anything pathological.
MAX_SAMPLING_DEPTH = 4
_mcp_sampling_depth: ContextVar[int] = ContextVar(
    "_mcp_sampling_depth", default=0
)


def build_sampling_callback(mesh: Any, default_model: str) -> SamplingCallback:
    """
    Construct an async ``(server_name, request) -> result`` callback that
    drives a one-shot completion through the given Mesh.
    """
    async def _cb(server_name: str, req: dict[str, Any]) -> dict[str, Any]:
        # Recursion guard — see MAX_SAMPLING_DEPTH note above.
        #
        # Tier-4 hardening: ContextVar values are inherited by every
        # ``asyncio.create_task`` (each Task copies the current
        # context). Without an explicit reset we'd leak the depth from
        # one logical request chain into a sibling chain that happened
        # to be spawned while a sampling call was in flight, and the
        # sibling would falsely trip the recursion guard. Pin the
        # bound: if the caller asserts they've come in with depth ==
        # 0 (no parent sampling frame on this chain) we honor that
        # via ``req["_recursion_depth"]`` and reset; otherwise we
        # accumulate.
        explicit_depth = req.get("_recursion_depth")
        if isinstance(explicit_depth, int) and explicit_depth >= 0:
            depth = explicit_depth
        else:
            depth = _mcp_sampling_depth.get()
        if depth >= MAX_SAMPLING_DEPTH:
            raise RuntimeError(
                f"mcp sampling recursion limit exceeded "
                f"({depth} >= {MAX_SAMPLING_DEPTH}); refusing to re-enter "
                f"from server {server_name!r}"
            )
        token = _mcp_sampling_depth.set(depth + 1)
        try:
            # Pick a model: per-server override > default, then enforce allowlist.
            model = req.get("model_override") or default_model
            allowed = set(req.get("allowed_models") or [])
            if allowed and model not in allowed:
                for cand in allowed:
                    if mesh.resolve(cand):
                        model = cand
                        break
                else:
                    raise RuntimeError(
                        f"sampling: no allowed model resolves on mesh "
                        f"(allowed={sorted(allowed)})"
                    )

            messages = _convert_messages(req.get("messages") or [])
            system = req.get("system_prompt")
            max_tokens = int(req.get("max_tokens") or 1024)
            temperature = req.get("temperature")
            stop = list(req.get("stop_sequences") or [])

            try:
                text, used_model, stop_reason = await _collect_stream(
                    mesh,
                    model=model,
                    messages=messages,
                    system=system,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    stop=stop,
                )
            except Exception as e:
                err = redact_secrets(repr(e))
                log.warning("mcp sampling failed for %r: %s", server_name, err)
                raise

            return {
                "text": text,
                "model": used_model or model,
                "stop_reason": stop_reason or "endTurn",
            }
        finally:
            _mcp_sampling_depth.reset(token)

    return _cb


# ---------------------------------------------------------------------------
# Stream collector
# ---------------------------------------------------------------------------


async def _collect_stream(
    mesh: Any,
    *,
    model: str,
    messages: list[Message],
    system: str | None,
    max_tokens: int,
    temperature: float | None,
    stop: list[str],
) -> tuple[str, str, str]:
    """
    Drive ``mesh.stream`` to completion and return ``(text, model, stop_reason)``.
    """
    from ..llm.mesh import StreamRequest  # local import: avoid cycle at module load
    from ..message_sanitization import sanitize_messages_for_provider

    req = StreamRequest(
        messages=sanitize_messages_for_provider(messages),
        model=model,
        system=system,
        tools=[],
        tool_choice=None,
        temperature=temperature,
        max_tokens=max_tokens,
        stop=stop,
    )

    text_chunks: list[str] = []
    used_model = model
    stop_reason = "endTurn"
    error: str | None = None

    async for chunk in mesh.stream(req):
        if chunk.kind == ChunkKind.TEXT and chunk.text:
            text_chunks.append(chunk.text)
        elif chunk.kind == ChunkKind.STOP:
            stop_reason = chunk.stop_reason or "endTurn"
            break
        elif chunk.kind == ChunkKind.ERROR:
            error = chunk.error
            stop_reason = "error"
            break
        # ignore THINKING/TOOL_CALL/USAGE for sampling

    if error and not text_chunks:
        raise RuntimeError(f"mesh stream errored: {error}")

    return "".join(text_chunks), used_model, stop_reason


# ---------------------------------------------------------------------------
# Message conversion helpers
# ---------------------------------------------------------------------------


def _convert_messages(items: list[dict[str, Any]]) -> list[Message]:
    """Convert MCP-shaped messages back into Cogitum :class:`Message` objects."""
    out: list[Message] = []
    for item in items:
        role = item.get("role", "user")
        if role not in ("user", "assistant", "system", "tool"):
            role = "user"
        text = _extract_text(item.get("content"))
        out.append(Message(role=role, parts=[TextPart(text=text)]))
    return out


def _extract_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_extract_text(c) for c in content)
    if isinstance(content, dict):
        if content.get("type") == "text":
            return content.get("text", "") or ""
        return f"[{content.get('type', 'unknown')} content]"
    return ""
