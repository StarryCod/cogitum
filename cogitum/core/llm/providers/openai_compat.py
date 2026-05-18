"""
Universal OpenAI-compatible adapter.

Covers: OpenAI, Together, Groq, DeepInfra, Fireworks, Cerebras, SambaNova,
Hyperbolic, OpenRouter, Canopywave, vLLM, llama.cpp server, Ollama (in
OpenAI mode).

The adapter speaks `/v1/chat/completions` with SSE streaming. Differences
between vendors are handled via flags on `ProviderConfig.extra` and
`ModelConfig.extra`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, AsyncIterator

import httpx

from ..capabilities import Capability
from ..events_helpers import normalize_messages_openai
from ..keypool import LeaseOutcome
from ..prompt_caching import apply_cache_control, should_cache
from ...events import ChunkKind, StreamChunk, Usage
from ..provider import CompletionRequest, Provider

if TYPE_CHECKING:
    from ..keypool import KeyLease


logger = logging.getLogger(__name__)


class OpenAICompatProvider(Provider):
    """One adapter, many backends."""

    _client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # HTTP client lifecycle
    # ------------------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.config.base_url.rstrip("/"),
                timeout=httpx.Timeout(
                    self.config.timeout_s,
                    connect=self.config.connect_timeout_s,
                    # Read timeout: max time between receiving chunks.
                    # If no data arrives for 120s, the connection is stale.
                    read=min(self.config.timeout_s, 120.0),
                ),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Auth headers
    # ------------------------------------------------------------------

    def _auth_headers(self, lease: "KeyLease") -> dict[str, str]:
        h: dict[str, str] = {}
        mode = self.config.auth
        secret = lease.secret

        if mode == "bearer":
            h["Authorization"] = f"Bearer {secret}"
        elif mode == "x_api_key":
            h["x-api-key"] = secret
        elif mode == "header_custom":
            name = self.config.auth_header_name or "Authorization"
            h[name] = secret
        elif mode == "query_param":
            pass  # handled in URL build
        elif mode == "none":
            pass

        # Per-provider extras (org id, etc.)
        for k, v in self.config.extra.get("headers", {}).items():
            h[k] = str(v)
        # Per-key extras override.
        for k, v in lease.state.config.extra_headers.items():
            h[k] = v
        return h

    # ------------------------------------------------------------------
    # Request body
    # ------------------------------------------------------------------

    def _build_body(self, req: CompletionRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": req.model.id,
            "messages": normalize_messages_openai(req.messages, system=req.system),
            "stream": req.stream,
        }

        # Apply prompt caching if provider supports it
        if should_cache(self.config.base_url, req.model.id):
            body["messages"] = apply_cache_control(body["messages"])

        if req.tools:
            body["tools"] = req.tools
            if req.tool_choice is not None:
                body["tool_choice"] = req.tool_choice

        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.top_p is not None:
            body["top_p"] = req.top_p
        if req.max_tokens is not None:
            body["max_tokens"] = req.max_tokens
        if req.stop:
            body["stop"] = req.stop

        if req.json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": req.json_schema,
            }

        # Reasoning effort: map via model's reasoning_effort_map.
        if req.reasoning_effort and Capability.REASONING in req.model.capabilities:
            mapped = req.model.reasoning_effort_map.get(
                req.reasoning_effort, req.reasoning_effort
            )
            if mapped and mapped != "off":
                body["reasoning_effort"] = mapped

        # Stream usage tracking (OpenAI-style).
        if req.stream:
            body["stream_options"] = {"include_usage": True}

        # Pass-through extras.
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
        url = "/chat/completions"
        if self.config.auth == "query_param":
            param = self.config.auth_query_param or "key"
            url = f"{url}?{param}={lease.secret}"

        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **self._auth_headers(lease),
        }
        body = self._build_body(request)

        client = self._http()
        try:
            async with client.stream(
                "POST", url, headers=headers, json=body
            ) as resp:
                if resp.status_code in (401, 403):
                    body_text = (await resp.aread()).decode("utf-8", "replace")[:400]
                    lease.record(LeaseOutcome.AUTH_ERROR, error=body_text)
                    yield StreamChunk(
                        kind=ChunkKind.ERROR,
                        error=f"auth error {resp.status_code}: {body_text}",
                    )
                    return

                if resp.status_code == 429:
                    body_text = (await resp.aread()).decode("utf-8", "replace")[:400]
                    # Parse Retry-After header — providers serve it
                    # in two forms: "<seconds>" or "<HTTP-date>". We
                    # only handle the seconds form here; the date
                    # form is rare on chat APIs.
                    retry_after_raw = resp.headers.get("retry-after", "").strip()
                    cooldown_hint = 0.0
                    if retry_after_raw:
                        try:
                            cooldown_hint = float(retry_after_raw)
                        except ValueError:
                            cooldown_hint = 0.0
                        body_text = f"[Retry-After: {retry_after_raw}] {body_text}"
                    lease.record(
                        LeaseOutcome.RATE_LIMITED,
                        error=body_text,
                        cooldown_hint=cooldown_hint,
                    )
                    yield StreamChunk(
                        kind=ChunkKind.ERROR,
                        error=f"rate limited (429): {body_text}",
                    )
                    return

                if resp.status_code >= 400:
                    body_text = (await resp.aread()).decode("utf-8", "replace")[:400]
                    lease.record(LeaseOutcome.ERROR, error=body_text)
                    yield StreamChunk(
                        kind=ChunkKind.ERROR,
                        error=f"http {resp.status_code}: {body_text}",
                    )
                    return

                # parse SSE
                try:
                    async for chunk in self._parse_sse(resp, lease):
                        yield chunk
                except GeneratorExit:
                    return

        except GeneratorExit:
            return
        except asyncio.CancelledError:
            lease.record(LeaseOutcome.CANCELLED)
            raise
        except httpx.HTTPError as e:
            lease.record(LeaseOutcome.ERROR, error=str(e))
            yield StreamChunk(kind=ChunkKind.ERROR, error=f"network: {e}")

    # ------------------------------------------------------------------
    # SSE parsing
    # ------------------------------------------------------------------

    async def _parse_sse(
        self,
        resp: httpx.Response,
        lease: "KeyLease",
    ) -> AsyncIterator[StreamChunk]:
        # Track in-progress tool calls by their index since OpenAI streams
        # arguments as deltas.
        tool_buffers: dict[int, dict[str, Any]] = {}
        total_usage = Usage()
        stop_reason: str | None = None
        ok = False
        # Buffer for detecting inline tool call markers (Kimi K2.6, etc.)
        # These models emit tool calls as special tokens in content/reasoning
        # instead of using the standard delta.tool_calls field.
        _inline_tc_buf: str = ""
        _in_inline_tc: bool = False

        async for raw_line in resp.aiter_lines():
            if not raw_line:
                continue
            if not raw_line.startswith("data:"):
                continue
            data = raw_line[5:].strip()
            if data == "[DONE]":
                break
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                logger.debug("non-JSON SSE line: %s", data[:120])
                continue

            # Top-level usage frame (after stream_options.include_usage)
            if payload.get("usage") and not payload.get("choices"):
                u = payload["usage"]
                total_usage = _usage_from_openai(u)
                continue

            for choice in payload.get("choices") or []:
                delta = choice.get("delta") or {}

                # 1) text content
                if isinstance(delta.get("content"), str) and delta["content"]:
                    text_chunk = delta["content"]
                    # Detect inline tool call markers (Kimi K2.6, Moonshot)
                    # Format: <|tool_calls_section_begin|>...<|tool_calls_section_end|>
                    if "<|tool_calls_section_begin|>" in text_chunk or _in_inline_tc:
                        _in_inline_tc = True
                        _inline_tc_buf += text_chunk
                        if "<|tool_calls_section_end|>" in _inline_tc_buf:
                            _in_inline_tc = False
                            # Parse inline tool calls
                            for tc_chunk in self._parse_inline_tool_calls(_inline_tc_buf, tool_buffers):
                                yield tc_chunk
                            _inline_tc_buf = ""
                            ok = True
                        # Don't yield as TEXT while buffering tool calls
                    else:
                        ok = True
                        yield StreamChunk(kind=ChunkKind.TEXT, text=text_chunk)

                # 2) reasoning (OpenAI o-series + many compats use reasoning_content)
                rc = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(rc, str) and rc:
                    # Same inline tool call detection for reasoning stream
                    if "<|tool_calls_section_begin|>" in rc or _in_inline_tc:
                        _in_inline_tc = True
                        _inline_tc_buf += rc
                        if "<|tool_calls_section_end|>" in _inline_tc_buf:
                            _in_inline_tc = False
                            for tc_chunk in self._parse_inline_tool_calls(_inline_tc_buf, tool_buffers):
                                yield tc_chunk
                            _inline_tc_buf = ""
                            ok = True
                    else:
                        ok = True
                        yield StreamChunk(kind=ChunkKind.THINKING, thinking=rc)

                # 3) tool calls (deltas)
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    buf = tool_buffers.setdefault(
                        idx, {"id": None, "name": None, "args": ""}
                    )
                    if tc.get("id"):
                        buf["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        buf["name"] = fn["name"]
                    if fn.get("arguments"):
                        buf["args"] += fn["arguments"]
                    yield StreamChunk(
                        kind=ChunkKind.TOOL_CALL_DELTA,
                        tool_call_id=buf["id"],
                        tool_call_name=buf["name"],
                        tool_call_args_delta=fn.get("arguments"),
                    )

                fr = choice.get("finish_reason")
                if fr:
                    stop_reason = fr
                    # Emit completed tool calls.
                    for buf in tool_buffers.values():
                        try:
                            args_obj = json.loads(buf["args"]) if buf["args"] else {}
                        except json.JSONDecodeError:
                            args_obj = {"_raw": buf["args"]}
                        yield StreamChunk(
                            kind=ChunkKind.TOOL_CALL_DONE,
                            tool_call_id=buf["id"],
                            tool_call_name=buf["name"],
                            tool_call_args=args_obj,
                        )

        if total_usage.total:
            yield StreamChunk(kind=ChunkKind.USAGE, usage=total_usage)
            lease.tokens_used = total_usage.input_tokens + total_usage.output_tokens

        yield StreamChunk(
            kind=ChunkKind.STOP,
            stop_reason=stop_reason or "end_turn",
        )
        # Only record OK if we actually got meaningful content.
        # Empty responses (out=0) should NOT reset error_streak — they indicate
        # the model refused or the provider returned nothing useful.
        if ok or tool_buffers:
            lease.record(LeaseOutcome.OK, tokens=lease.tokens_used)
        elif stop_reason:
            # Got a stop but no content — record as OK but don't reset streak
            # (lease default is ERROR, which would penalize — use OK but the
            # streak reset is handled by the OK branch in _release)
            # Actually: treat empty-but-stopped as OK to not penalize the key.
            # The key worked fine, the model just chose to say nothing.
            lease.record(LeaseOutcome.OK, tokens=lease.tokens_used)

    # ------------------------------------------------------------------
    # Inline tool call parsing (Kimi K2.6, Moonshot, etc.)
    # ------------------------------------------------------------------

    def _parse_inline_tool_calls(
        self, buf: str, tool_buffers: dict[int, dict[str, Any]]
    ) -> list[StreamChunk]:
        """Parse tool calls from inline markers in content/reasoning stream.

        Format:
          <|tool_calls_section_begin|>
          <|tool_call_begin|> functions.tool_name:index <|tool_call_argument_begin|>
          {"arg": "value"}
          <|tool_call_end|>
          <|tool_calls_section_end|>
        """
        import re
        chunks: list[StreamChunk] = []

        # Extract individual tool calls
        tc_pattern = re.compile(
            r"<\|tool_call_begin\|>\s*"
            r"(?:functions\.)?(\w+)(?::(\d+))?\s*"
            r"<\|tool_call_argument_begin\|>\s*"
            r"(.*?)\s*"
            r"<\|tool_call_end\|>",
            re.DOTALL,
        )

        for match in tc_pattern.finditer(buf):
            name = match.group(1)
            idx = int(match.group(2)) if match.group(2) else len(tool_buffers)
            raw_args = match.group(3).strip()

            try:
                args_obj = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                args_obj = {"_raw": raw_args}

            # Generate a call ID if not provided
            call_id = f"call_inline_{name}_{idx}"

            # Register in tool_buffers so finish_reason logic works
            tool_buffers[idx] = {"id": call_id, "name": name, "args": raw_args}

            chunks.append(StreamChunk(
                kind=ChunkKind.TOOL_CALL_DONE,
                tool_call_id=call_id,
                tool_call_name=name,
                tool_call_args=args_obj,
            ))

        return chunks


def _usage_from_openai(u: dict[str, Any]) -> Usage:
    details = u.get("prompt_tokens_details") or {}
    completion_details = u.get("completion_tokens_details") or {}
    return Usage(
        input_tokens=int(u.get("prompt_tokens", 0)),
        output_tokens=int(u.get("completion_tokens", 0)),
        cache_read_tokens=int(details.get("cached_tokens", 0)),
        cache_write_tokens=0,
        reasoning_tokens=int(completion_details.get("reasoning_tokens", 0)),
    )
