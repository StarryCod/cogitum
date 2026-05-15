"""
Anthropic Messages API adapter.

Supports both:
  * x-api-key auth (regular API keys from console.anthropic.com)
  * OAuth bearer auth from Claude Pro/Max subscription (different beta header,
    different model availability, no system prompt overrides allowed in
    practice — but that's user code's problem, not ours).

Key dispatch is via ProviderConfig.auth:
  * "x_api_key" -> sets x-api-key header
  * "bearer"    -> sets Authorization: Bearer <access_token>; auto-refreshes
                   if a stored OAuthCredentials matches the key id.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, AsyncIterator

import httpx

from ..events_helpers import normalize_messages_anthropic
from ..keypool import LeaseOutcome
from ...auth import storage as auth_storage
from ...auth.registry import get_provider as get_oauth_provider
from ...events import ChunkKind, StreamChunk, Usage
from ..provider import CompletionRequest, Provider

if TYPE_CHECKING:
    from ..keypool import KeyLease


logger = logging.getLogger(__name__)


# Anthropic-Beta headers we send by default. Multiple values comma-separated.
_DEFAULT_BETAS = (
    "claude-code-20250219",          # required for OAuth subscription tokens
    "messages-2023-12-15",
    "tools-2024-04-04",
    "fine-grained-tool-streaming-2025-05-14",
    "prompt-caching-2024-07-31",
)


class AnthropicProvider(Provider):
    _client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.config.base_url.rstrip("/"),
                timeout=httpx.Timeout(
                    self.config.timeout_s, connect=self.config.connect_timeout_s
                ),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    async def _resolve_secret(self, lease: "KeyLease") -> tuple[str, dict[str, str]]:
        """Return (secret, extra_headers).

        For OAuth keys whose `secret_ref` was `oauth:anthropic`, we look up
        live credentials in auth/storage.py and refresh on demand.
        """
        ref = lease.state.config.secret_ref
        extra: dict[str, str] = {}
        if ref.startswith("oauth:"):
            provider_id = ref.split(":", 1)[1]
            creds = auth_storage.get(provider_id)
            if creds is None:
                raise RuntimeError(
                    f"OAuth credentials missing for {provider_id}. Run `cog setup`."
                )
            if creds.expired():
                oauth = get_oauth_provider(provider_id)
                if oauth is None:
                    raise RuntimeError(f"unknown oauth provider id: {provider_id}")
                creds = await oauth.refresh(creds)
                auth_storage.set_(provider_id, creds)
                # bump cached secret on the lease so subsequent pool
                # operations see the fresh token
                lease.state.secret = creds.access
            return creds.access, extra
        return lease.secret, extra

    def _auth_headers(self, secret: str) -> dict[str, str]:
        h: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "anthropic-version": self.config.extra.get(
                "anthropic_version", "2023-06-01"
            ),
        }
        betas = self.config.extra.get("betas", _DEFAULT_BETAS)
        if betas:
            h["anthropic-beta"] = ",".join(betas)

        if self.config.auth == "x_api_key":
            h["x-api-key"] = secret
        elif self.config.auth == "bearer":
            h["Authorization"] = f"Bearer {secret}"
        return h

    # ------------------------------------------------------------------
    # Body
    # ------------------------------------------------------------------

    def _build_body(self, req: CompletionRequest) -> dict[str, Any]:
        system, messages = normalize_messages_anthropic(req.messages, system=req.system)

        body: dict[str, Any] = {
            "model": req.model.id,
            "messages": messages,
            "max_tokens": req.max_tokens or req.model.max_output_tokens,
            "stream": req.stream,
        }
        if system:
            body["system"] = system
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.top_p is not None:
            body["top_p"] = req.top_p
        if req.stop:
            body["stop_sequences"] = req.stop

        if req.tools:
            body["tools"] = [_tool_to_anthropic(t) for t in req.tools]
            if req.tool_choice is not None:
                body["tool_choice"] = (
                    {"type": req.tool_choice}
                    if isinstance(req.tool_choice, str) and req.tool_choice in ("auto", "any", "none")
                    else req.tool_choice
                )

        # Reasoning -> Anthropic "thinking" parameter on supported models.
        if req.reasoning_effort and req.model.reasoning_effort_map:
            mapped = req.model.reasoning_effort_map.get(
                req.reasoning_effort, req.reasoning_effort
            )
            if mapped and mapped != "off":
                # Anthropic style: {"type":"enabled","budget_tokens": int}
                budget = {
                    "low": 4_000,
                    "medium": 8_000,
                    "high": 16_000,
                    "xhigh": 32_000,
                }.get(mapped, 8_000)
                body["thinking"] = {"type": "enabled", "budget_tokens": budget}

        for k, v in (req.extra or {}).items():
            body.setdefault(k, v)
        return body

    # ------------------------------------------------------------------
    # Stream
    # ------------------------------------------------------------------

    async def stream(
        self,
        request: CompletionRequest,
        lease: "KeyLease",
    ) -> AsyncIterator[StreamChunk]:
        try:
            secret, extra_headers = await self._resolve_secret(lease)
        except Exception as e:
            lease.record(LeaseOutcome.AUTH_ERROR, error=str(e))
            yield StreamChunk(kind=ChunkKind.ERROR, error=f"auth: {e}")
            return

        headers = {**self._auth_headers(secret), **extra_headers}
        body = self._build_body(request)
        client = self._http()

        try:
            async with client.stream("POST", "/v1/messages", headers=headers, json=body) as resp:
                if resp.status_code in (401, 403):
                    text = (await resp.aread()).decode("utf-8", "replace")[:400]
                    lease.record(LeaseOutcome.AUTH_ERROR, error=text)
                    yield StreamChunk(kind=ChunkKind.ERROR, error=f"auth {resp.status_code}: {text}")
                    return
                if resp.status_code == 429:
                    text = (await resp.aread()).decode("utf-8", "replace")[:400]
                    lease.record(LeaseOutcome.RATE_LIMITED, error=text)
                    yield StreamChunk(kind=ChunkKind.ERROR, error=f"rate limited: {text}")
                    return
                if resp.status_code >= 400:
                    text = (await resp.aread()).decode("utf-8", "replace")[:400]
                    lease.record(LeaseOutcome.ERROR, error=text)
                    yield StreamChunk(kind=ChunkKind.ERROR, error=f"http {resp.status_code}: {text}")
                    return

                async for chunk in self._parse_sse(resp, lease):
                    yield chunk

        except httpx.HTTPError as e:
            lease.record(LeaseOutcome.ERROR, error=str(e))
            yield StreamChunk(kind=ChunkKind.ERROR, error=f"network: {e}")

    async def _parse_sse(
        self, resp: httpx.Response, lease: "KeyLease"
    ) -> AsyncIterator[StreamChunk]:
        # Anthropic SSE has named events. We only read the JSON `data:` lines
        # and switch on payload["type"].
        usage = Usage()
        # Track partial tool_use blocks by their content-block index.
        active_tool: dict[int, dict[str, Any]] = {}
        stop_reason: str | None = None
        ok = False

        async for raw_line in resp.aiter_lines():
            if not raw_line or not raw_line.startswith("data:"):
                continue
            data = raw_line[5:].strip()
            if not data:
                continue
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue

            t = payload.get("type")

            if t == "message_start":
                u = (payload.get("message") or {}).get("usage") or {}
                usage = usage.merge(_anthropic_usage(u))
                continue

            if t == "content_block_start":
                idx = payload.get("index", 0)
                block = payload.get("content_block") or {}
                if block.get("type") == "tool_use":
                    active_tool[idx] = {
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "args": "",
                    }
                continue

            if t == "content_block_delta":
                idx = payload.get("index", 0)
                delta = payload.get("delta") or {}
                dt = delta.get("type")
                if dt == "text_delta":
                    txt = delta.get("text") or ""
                    if txt:
                        ok = True
                        yield StreamChunk(kind=ChunkKind.TEXT, text=txt)
                elif dt == "thinking_delta":
                    th = delta.get("thinking") or ""
                    if th:
                        ok = True
                        yield StreamChunk(kind=ChunkKind.THINKING, thinking=th)
                elif dt == "signature_delta":
                    sig = delta.get("signature") or ""
                    if sig:
                        yield StreamChunk(
                            kind=ChunkKind.THINKING,
                            thinking="",
                            thinking_signature=sig,
                        )
                elif dt == "input_json_delta":
                    if idx in active_tool:
                        active_tool[idx]["args"] += delta.get("partial_json") or ""
                        yield StreamChunk(
                            kind=ChunkKind.TOOL_CALL_DELTA,
                            tool_call_id=active_tool[idx]["id"],
                            tool_call_name=active_tool[idx]["name"],
                            tool_call_args_delta=delta.get("partial_json"),
                        )
                continue

            if t == "content_block_stop":
                idx = payload.get("index", 0)
                if idx in active_tool:
                    info = active_tool.pop(idx)
                    try:
                        args_obj = json.loads(info["args"]) if info["args"] else {}
                    except json.JSONDecodeError:
                        args_obj = {"_raw": info["args"]}
                    yield StreamChunk(
                        kind=ChunkKind.TOOL_CALL_DONE,
                        tool_call_id=info["id"],
                        tool_call_name=info["name"],
                        tool_call_args=args_obj,
                    )
                continue

            if t == "message_delta":
                d = payload.get("delta") or {}
                if d.get("stop_reason"):
                    stop_reason = d["stop_reason"]
                u = payload.get("usage") or {}
                usage = usage.merge(_anthropic_usage(u))
                continue

            if t == "message_stop":
                break

            if t == "error":
                err = (payload.get("error") or {}).get("message") or "anthropic error"
                lease.record(LeaseOutcome.ERROR, error=err)
                yield StreamChunk(kind=ChunkKind.ERROR, error=err)
                return

        if usage.total:
            yield StreamChunk(kind=ChunkKind.USAGE, usage=usage)
            lease.tokens_used = usage.input_tokens + usage.output_tokens

        yield StreamChunk(kind=ChunkKind.STOP, stop_reason=stop_reason or "end_turn")
        if ok or stop_reason:
            lease.record(LeaseOutcome.OK, tokens=lease.tokens_used)


def _anthropic_usage(u: dict[str, Any]) -> Usage:
    return Usage(
        input_tokens=int(u.get("input_tokens", 0) or 0),
        output_tokens=int(u.get("output_tokens", 0) or 0),
        cache_read_tokens=int(u.get("cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(u.get("cache_creation_input_tokens", 0) or 0),
    )


def _tool_to_anthropic(t: dict[str, Any]) -> dict[str, Any]:
    """Accept either an OpenAI-style {name,description,parameters} dict or the
    Anthropic native {name,description,input_schema} shape."""
    if "input_schema" in t:
        return t
    return {
        "name": t["name"],
        "description": t.get("description", ""),
        "input_schema": t.get("parameters") or {"type": "object", "properties": {}},
    }


# silence unused lint
_ = time
