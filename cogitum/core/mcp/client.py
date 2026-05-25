"""
cogitum.core.mcp.client
~~~~~~~~~~~~~~~~~~~~~~~

Long-lived MCP connection manager.

Design
------
- A single :class:`MCPManager` owns a dedicated asyncio event loop on a
  background daemon thread (``_LoopThread``). All MCP I/O happens there.
- Each configured server has a :class:`ServerHandle` with its own
  ``ClientSession``, kept open for the lifetime of the agent process.
- Tool calls are submitted from synchronous Python via
  :py:meth:`MCPManager.call_tool`, which marshals onto the loop with
  ``asyncio.run_coroutine_threadsafe`` and waits for the result.
- On connect failure or session drop, an exponential backoff reconnect
  task is spawned (max 5 retries, capped at 60s).
- Sampling requests from the server are routed back to the Cogitum
  Mesh via a callable provided by the caller.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .config import MCPConfig, MCPServerConfig, SamplingConfig
from .security import filter_env, resolve_mapping, resolve_secret, redact_secrets

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional MCP SDK
# ---------------------------------------------------------------------------

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.types import Tool as MCPTool, CallToolResult
    _MCP_AVAILABLE = True
except ImportError:  # pragma: no cover
    ClientSession = None  # type: ignore
    StdioServerParameters = None  # type: ignore
    stdio_client = None  # type: ignore
    MCPTool = None  # type: ignore
    CallToolResult = None  # type: ignore
    _MCP_AVAILABLE = False

try:  # HTTP transport is in newer mcp versions
    from mcp.client.streamable_http import streamablehttp_client
    _MCP_HTTP_AVAILABLE = True
except ImportError:  # pragma: no cover
    streamablehttp_client = None  # type: ignore
    _MCP_HTTP_AVAILABLE = False


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------


class _LoopThread:
    """A daemon thread running a dedicated asyncio loop."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="mcp-loop", daemon=True
        )

    def start(self) -> None:
        if self._thread.is_alive():
            return
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def _run(self) -> None:  # pragma: no cover (thread entry)
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise RuntimeError("loop not started")
        return self._loop

    def submit(self, coro: Awaitable[Any]) -> Any:
        """Submit a coroutine to the loop and block for its result."""
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result()

    def submit_async(self, coro: Awaitable[Any]):
        """Submit and return a concurrent.futures.Future (do not block)."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)


# ---------------------------------------------------------------------------
# Server handle
# ---------------------------------------------------------------------------


@dataclass
class ServerHandle:
    name: str
    config: MCPServerConfig
    session: Any = None  # ClientSession, when connected
    tools: list[Any] = field(default_factory=list)  # MCPTool list
    state: str = "disconnected"  # disconnected|connecting|connected|failed
    last_error: str | None = None
    connect_attempts: int = 0
    _stack: AsyncExitStack | None = None
    _reconnect_task: asyncio.Task | None = None


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


# Sampling callback signature:
#   (server_name, request_dict) -> awaitable[CreateMessageResult-shaped dict]
SamplingCallback = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class MCPManager:
    """Singleton manager for all configured MCP servers."""

    _MAX_RECONNECT_ATTEMPTS = 5
    _BASE_BACKOFF = 1.0
    _MAX_BACKOFF = 60.0

    def __init__(self) -> None:
        self._loop_thread = _LoopThread()
        self._handles: dict[str, ServerHandle] = {}
        self._config: MCPConfig | None = None
        self._sampling_cb: SamplingCallback | None = None
        self._started = False
        self._sampling_buckets: dict[str, list[float]] = {}  # server -> timestamps
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        return _MCP_AVAILABLE

    def set_sampling_callback(self, cb: SamplingCallback | None) -> None:
        """Register a callback that satisfies server-initiated sampling."""
        self._sampling_cb = cb

    def start(self, config: MCPConfig) -> None:
        """Start the loop thread and connect to all enabled servers."""
        if not _MCP_AVAILABLE:
            log.warning(
                "mcp package not installed — skipping MCP startup. "
                "Install with `pip install 'cogitum[mcp]'`"
            )
            return
        with self._lock:
            if self._started:
                # idempotent: connect any new servers
                self._reconcile(config)
                return
            self._config = config
            self._loop_thread.start()
            self._started = True

        # Initial connections
        for name, srv in config.servers.items():
            if not srv.enabled:
                log.info("mcp: server %r disabled in config; skipping", name)
                continue
            self._handles[name] = ServerHandle(name=name, config=srv)
            self._loop_thread.submit_async(self._connect_with_retry(name))

    def _reconcile(self, new_config: MCPConfig) -> None:
        """
        Bring live state in sync with ``new_config``.

        Handles:
          * server removed from config → close session + drop handle
          * server disabled            → close session + keep handle (state=disconnected)
          * server config changed      → close + reconnect with new config
          * new enabled server         → create handle + connect
        """
        old_config = self._config
        self._config = new_config

        old_names = set(self._handles.keys())
        new_names = set(new_config.servers.keys())

        # 1. Removed: close + drop
        for name in old_names - new_names:
            self._loop_thread.submit_async(self._teardown(name, drop=True))

        # 2. Existing: detect changes / enable-disable transitions
        for name in old_names & new_names:
            new_srv = new_config.servers[name]
            handle = self._handles[name]
            old_srv = handle.config

            if not new_srv.enabled and handle.state in ("connected", "connecting"):
                self._loop_thread.submit_async(self._teardown(name, drop=False))
                handle.config = new_srv
                continue

            # Update reference (risk overrides etc) cheaply
            handle.config = new_srv

            # If a structural field changed and the server is/should be running,
            # disconnect and reconnect.
            if new_srv.enabled and _server_needs_reconnect(old_srv, new_srv):
                self._loop_thread.submit_async(
                    self._teardown_and_reconnect(name)
                )
                continue

            # If it was disabled and is now enabled — connect.
            if (
                new_srv.enabled
                and handle.state in ("disconnected", "failed")
            ):
                self._loop_thread.submit_async(self._connect_with_retry(name))

        # 3. New servers
        for name in new_names - old_names:
            srv = new_config.servers[name]
            if not srv.enabled:
                continue
            self._handles[name] = ServerHandle(name=name, config=srv)
            self._loop_thread.submit_async(self._connect_with_retry(name))

    async def _teardown(self, name: str, *, drop: bool) -> None:
        """Close a server's session. If drop=True, remove the handle entirely."""
        handle = self._handles.get(name)
        if handle is None:
            return
        if handle._stack is not None:
            try:
                await handle._stack.aclose()
            except Exception as e:
                log.debug("mcp teardown %s: %s", name, e)
        handle._stack = None
        handle.session = None
        handle.tools = []
        handle.state = "disconnected"
        if drop:
            self._handles.pop(name, None)
            log.info("mcp: dropped server %r", name)
        else:
            log.info("mcp: disconnected server %r", name)

    async def _teardown_and_reconnect(self, name: str) -> None:
        await self._teardown(name, drop=False)
        await self._connect_with_retry(name)

    def shutdown(self) -> None:
        """Close all sessions and stop the loop thread."""
        if not self._started:
            return
        try:
            self._loop_thread.submit(self._close_all())
        except Exception as e:  # pragma: no cover
            log.debug("mcp shutdown: %s", e)
        self._loop_thread.stop()
        self._started = False

    async def _close_all(self) -> None:
        for name, handle in list(self._handles.items()):
            try:
                if handle._stack is not None:
                    await handle._stack.aclose()
            except Exception as e:
                log.debug("mcp: error closing %s: %s", name, e)
            handle.session = None
            handle.state = "disconnected"
            handle._stack = None

    # ------------------------------------------------------------------
    # Connection logic
    # ------------------------------------------------------------------

    async def _connect_with_retry(self, name: str) -> None:
        """Connect ``name``; on failure, exponential backoff up to N tries."""
        handle = self._handles.get(name)
        if handle is None:
            return

        for attempt in range(1, self._MAX_RECONNECT_ATTEMPTS + 1):
            handle.connect_attempts = attempt
            handle.state = "connecting"
            try:
                await self._connect_one(handle)
                handle.state = "connected"
                handle.last_error = None
                log.info(
                    "mcp: connected to %r (transport=%s, %d tools)",
                    name, handle.config.transport, len(handle.tools),
                )
                return
            except Exception as e:
                err = redact_secrets(repr(e))
                handle.last_error = err
                handle.state = "failed"
                log.warning(
                    "mcp: connect attempt %d/%d for %r failed: %s",
                    attempt, self._MAX_RECONNECT_ATTEMPTS, name, err,
                )
                # Cleanup partial state
                if handle._stack is not None:
                    try:
                        await handle._stack.aclose()
                    except Exception:
                        pass
                    handle._stack = None
                handle.session = None

                if attempt >= self._MAX_RECONNECT_ATTEMPTS:
                    log.error("mcp: giving up on %r after %d attempts", name, attempt)
                    return

                backoff = min(
                    self._BASE_BACKOFF * (2 ** (attempt - 1)),
                    self._MAX_BACKOFF,
                )
                await asyncio.sleep(backoff)

    async def _connect_one(self, handle: ServerHandle) -> None:
        """Open a session for a single server and discover its tools."""
        srv = handle.config
        stack = AsyncExitStack()
        handle._stack = stack

        connect_timeout = srv.connect_timeout

        async def _do_connect() -> None:
            transport = srv.transport
            if transport == "stdio":
                # Resolve env (vault: / env: prefixes)
                resolved_env = resolve_mapping(srv.env or {})
                full_env = filter_env(resolved_env)
                params = StdioServerParameters(
                    command=srv.command,
                    args=list(srv.args or []),
                    env=full_env,
                )
                read, write = await stack.enter_async_context(stdio_client(params))
            elif transport == "http":
                if not _MCP_HTTP_AVAILABLE:
                    raise RuntimeError(
                        "MCP HTTP transport unavailable; upgrade `mcp` package"
                    )
                resolved_headers = resolve_mapping(srv.headers or {})
                # streamablehttp_client returns (read, write, get_session_id)
                ctx = await stack.enter_async_context(
                    streamablehttp_client(srv.url, headers=resolved_headers)
                )
                if isinstance(ctx, tuple) and len(ctx) >= 2:
                    read, write = ctx[0], ctx[1]
                else:  # pragma: no cover
                    raise RuntimeError("unexpected streamablehttp_client return")
            else:
                raise ValueError(f"invalid transport for {srv.name!r}")

            # Optional sampling callback
            sampling_cb = None
            sampling_cfg = self._config.effective_sampling(srv.name) if self._config else SamplingConfig()
            if sampling_cfg.enabled and self._sampling_cb is not None:
                sampling_cb = self._build_sampling_handler(srv.name, sampling_cfg)

            session = await stack.enter_async_context(
                ClientSession(read, write, sampling_callback=sampling_cb)
            )
            await session.initialize()
            handle.session = session

            # Discover tools
            result = await session.list_tools()
            handle.tools = list(result.tools or [])

        await asyncio.wait_for(_do_connect(), timeout=connect_timeout)

    # ------------------------------------------------------------------
    # Sampling bridge (server → Cogitum LLM)
    # ------------------------------------------------------------------

    def _build_sampling_handler(self, server_name: str, cfg: SamplingConfig):
        """Build the per-server sampling_callback that ClientSession expects."""

        async def _handler(context, params):
            # Rate-limit per server
            if not self._check_rate_limit(server_name, cfg.max_rpm):
                raise RuntimeError(
                    f"sampling rate limit exceeded for server {server_name!r}"
                )
            if self._sampling_cb is None:
                raise RuntimeError("no sampling callback registered")

            # Convert params to a dict the bridge can consume
            req = {
                "messages": [
                    {
                        "role": m.role,
                        "content": _content_to_dict(m.content),
                    }
                    for m in (params.messages or [])
                ],
                "system_prompt": getattr(params, "systemPrompt", None),
                "max_tokens": min(
                    int(getattr(params, "maxTokens", cfg.max_tokens_cap) or cfg.max_tokens_cap),
                    cfg.max_tokens_cap,
                ),
                "temperature": getattr(params, "temperature", None),
                "model_preferences": _model_prefs_to_dict(
                    getattr(params, "modelPreferences", None)
                ),
                "stop_sequences": list(getattr(params, "stopSequences", []) or []),
                "model_override": cfg.model,
                "allowed_models": list(cfg.allowed_models or []),
                "timeout": cfg.timeout,
            }
            try:
                result = await asyncio.wait_for(
                    self._sampling_cb(server_name, req),
                    timeout=cfg.timeout,
                )
            except asyncio.TimeoutError:
                raise RuntimeError(f"sampling timed out after {cfg.timeout}s")

            # Convert back into the SDK's CreateMessageResult shape
            from mcp.types import CreateMessageResult, TextContent
            text = result.get("text", "") or ""
            return CreateMessageResult(
                role="assistant",
                content=TextContent(type="text", text=text),
                model=result.get("model", "cogitum"),
                stopReason=result.get("stop_reason", "endTurn"),
            )

        return _handler

    def _check_rate_limit(self, server: str, max_rpm: int) -> bool:
        if max_rpm <= 0:
            return True
        now = time.monotonic()
        bucket = self._sampling_buckets.setdefault(server, [])
        # Drop entries older than 60s
        cutoff = now - 60.0
        bucket[:] = [t for t in bucket if t >= cutoff]
        if len(bucket) >= max_rpm:
            return False
        bucket.append(now)
        return True

    # ------------------------------------------------------------------
    # Public sync API (called from agent / tool layer)
    # ------------------------------------------------------------------

    def call_tool(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Invoke ``tool`` on ``server`` synchronously.

        Returns ``{"result": "..."}`` on success or ``{"error": "..."}`` on
        failure. Errors are redacted of credential-like substrings.
        """
        if not self._started or not _MCP_AVAILABLE:
            return {"error": "MCP not available or not started"}
        handle = self._handles.get(server)
        if handle is None:
            return {"error": f"unknown MCP server {server!r}"}
        if handle.state != "connected" or handle.session is None:
            return {
                "error": (
                    f"MCP server {server!r} not connected "
                    f"(state={handle.state}, last_error={handle.last_error})"
                )
            }

        try:
            return self._loop_thread.submit(
                self._call_tool_async(handle, tool, arguments)
            )
        except Exception as e:
            return {"error": redact_secrets(repr(e))}

    async def _call_tool_async(
        self,
        handle: ServerHandle,
        tool: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            result = await asyncio.wait_for(
                handle.session.call_tool(tool, arguments=arguments),
                timeout=handle.config.timeout,
            )
        except asyncio.TimeoutError:
            return {"error": f"tool {tool!r} on {handle.name!r} timed out"}
        except Exception as e:
            # Auto-reconnect on session-dropped errors
            err = redact_secrets(repr(e))
            log.warning("mcp call_tool failed on %s/%s: %s", handle.name, tool, err)
            if any(x in err.lower() for x in ("closed", "broken", "eof", "reset")):
                # Schedule reconnect
                self._loop_thread.submit_async(self._connect_with_retry(handle.name))
            return {"error": err}

        return {"result": _call_tool_result_to_text(result)}

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def status(self) -> list[dict[str, Any]]:
        """Return a snapshot of all known servers."""
        out: list[dict[str, Any]] = []
        for name, h in self._handles.items():
            out.append({
                "name": name,
                "transport": h.config.transport,
                "state": h.state,
                "tool_count": len(h.tools),
                "tools": [t.name for t in h.tools],
                "attempts": h.connect_attempts,
                "last_error": h.last_error,
                "enabled": h.config.enabled,
            })
        return out

    def get_handle(self, name: str) -> ServerHandle | None:
        return self._handles.get(name)


# ---------------------------------------------------------------------------
# Helpers for SDK type conversion
# ---------------------------------------------------------------------------


def _server_needs_reconnect(old: MCPServerConfig, new: MCPServerConfig) -> bool:
    """
    Return True if the structural transport config changed enough that we
    must drop the existing session and reconnect.

    Risk overrides and ``enabled`` flips are handled elsewhere — this only
    checks fields that affect the live connection itself.
    """
    return (
        old.command != new.command
        or list(old.args) != list(new.args)
        or dict(old.env) != dict(new.env)
        or old.url != new.url
        or dict(old.headers) != dict(new.headers)
        or old.timeout != new.timeout
        or old.connect_timeout != new.connect_timeout
    )


def _call_tool_result_to_text(result: Any) -> str:
    """Render a CallToolResult as plain text for the LLM.

    Empty content + isError=False used to return "" — model thought the
    call succeeded with nothing to say. We now return "(no output)" so
    the model has an explicit success-but-empty signal AND so downstream
    LLM adapters never see an empty tool_result block (which Anthropic
    rejects with HTTP 400). Empty content + isError=True returns an
    explicit error placeholder for the same reason.
    """
    if result is None:
        return ""
    parts: list[str] = []
    content = getattr(result, "content", None) or []
    is_error = bool(getattr(result, "isError", False))
    if not content:
        if is_error:
            return "ERROR: tool returned an error with no message"
        return "(no output)"
    for item in content:
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
            continue
        item_type = getattr(item, "type", "unknown")
        # Image / audio / blob: preserve enough metadata so the model
        # can reason about it. Collapsing to a bare "[image content]"
        # token discarded the base64 payload AND the mime type, so a
        # screenshot tool round-tripped as a content-free placeholder
        # the model couldn't act on.
        if item_type in ("image", "audio"):
            data = getattr(item, "data", None) or ""
            mime = getattr(item, "mimeType", None) or "application/octet-stream"
            try:
                size = len(data)
            except Exception:
                size = 0
            parts.append(
                f"[{item_type} content: {size} bytes, type {mime}]"
            )
            continue
        # Embedded resource — try to surface the uri.
        if item_type == "resource":
            resource = getattr(item, "resource", None)
            uri = getattr(resource, "uri", None) if resource else None
            mime = (
                getattr(resource, "mimeType", None) if resource else None
            ) or "unknown"
            parts.append(
                f"[resource content: uri={uri or 'n/a'}, type {mime}]"
            )
            continue
        parts.append(f"[{item_type} content]")
    body = "\n".join(parts).strip()
    if is_error:
        return f"ERROR: {body or 'tool call returned isError=true'}"
    return body


def _content_to_dict(content: Any) -> Any:
    """Best-effort conversion of MCP content blocks to JSON-friendly dicts."""
    if content is None:
        return None
    if isinstance(content, list):
        return [_content_to_dict(c) for c in content]
    if hasattr(content, "model_dump"):
        try:
            return content.model_dump()
        except Exception:
            pass
    if hasattr(content, "text"):
        return {"type": "text", "text": content.text}
    return repr(content)


def _model_prefs_to_dict(prefs: Any) -> dict[str, Any] | None:
    if prefs is None:
        return None
    if hasattr(prefs, "model_dump"):
        try:
            return prefs.model_dump()
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


_MANAGER: MCPManager | None = None
_MANAGER_LOCK = threading.Lock()


def get_manager() -> MCPManager:
    """Return the process-wide MCPManager (creating it if needed)."""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = MCPManager()
        return _MANAGER


def shutdown_mcp() -> None:
    """Close all MCP sessions. Safe to call multiple times."""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is not None:
            try:
                _MANAGER.shutdown()
            finally:
                _MANAGER = None
