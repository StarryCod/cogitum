"""
Tiny localhost HTTP server for OAuth callbacks.

Used by both Anthropic and OpenAI Codex flows. Listens on a single port,
hands back the first matching `?code=&state=` it sees, then shuts down.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Awaitable, Callable

from .pages import oauth_error_html, oauth_success_html


@dataclass(slots=True)
class CallbackResult:
    code: str
    state: str | None


class CallbackServer:
    """Async one-shot HTTP server.

    Usage:
        async with CallbackServer(host, port, path) as srv:
            result = await srv.wait_for_code(timeout=300)
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int,
        path: str,
        expected_state: str | None = None,
        success_message: str = "Authentication completed. You can close this window.",
    ) -> None:
        self.host = host
        self.port = port
        self.path = path
        self.expected_state = expected_state
        self.success_message = success_message
        self._server: asyncio.base_events.Server | None = None
        self._future: asyncio.Future[CallbackResult | None] = (
            asyncio.get_event_loop().create_future()
            if False
            else None  # initialized in __aenter__
        )

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self.port}{self.path}"

    async def __aenter__(self) -> "CallbackServer":
        loop = asyncio.get_running_loop()
        self._future = loop.create_future()
        self._server = await asyncio.start_server(
            self._handle, host=self.host, port=self.port
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._server:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
        if self._future and not self._future.done():
            self._future.cancel()

    async def wait_for_code(self, *, timeout: float | None = None) -> CallbackResult | None:
        assert self._future is not None
        try:
            return await asyncio.wait_for(self._future, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        except asyncio.CancelledError:
            return None

    def cancel(self) -> None:
        if self._future and not self._future.done():
            self._future.set_result(None)

    # ------------------------------------------------------------------
    # raw HTTP request handler
    # ------------------------------------------------------------------

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                writer.close()
                return
            line = request_line.decode("iso-8859-1", "replace").strip()
            try:
                method, target, _ = line.split(" ", 2)
            except ValueError:
                await self._send_html(writer, 400, oauth_error_html("Bad request line"))
                return

            # drain headers
            while True:
                hl = await reader.readline()
                if hl in (b"\r\n", b"\n", b""):
                    break

            from urllib.parse import urlsplit, parse_qs
            split = urlsplit(target)
            if split.path != self.path:
                await self._send_html(writer, 404, oauth_error_html("Callback route not found."))
                return

            qs = parse_qs(split.query)
            error = (qs.get("error") or [""])[0]
            if error:
                await self._send_html(
                    writer, 400,
                    oauth_error_html("Authentication did not complete.", f"Error: {error}"),
                )
                if not self._future.done():
                    self._future.set_exception(RuntimeError(f"oauth error: {error}"))
                return

            code = (qs.get("code") or [""])[0]
            state = (qs.get("state") or [None])[0]

            if not code:
                await self._send_html(writer, 400, oauth_error_html("Missing authorization code."))
                return

            if self.expected_state is not None and state != self.expected_state:
                await self._send_html(writer, 400, oauth_error_html("State mismatch."))
                if not self._future.done():
                    self._future.set_exception(RuntimeError("state mismatch"))
                return

            await self._send_html(writer, 200, oauth_success_html(self.success_message))
            if not self._future.done():
                self._future.set_result(CallbackResult(code=code, state=state))
        except Exception as e:
            with contextlib.suppress(Exception):
                await self._send_html(writer, 500, oauth_error_html(f"Internal error: {e}"))
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    @staticmethod
    async def _send_html(writer: asyncio.StreamWriter, status: int, body: str) -> None:
        body_bytes = body.encode("utf-8")
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error"}.get(status, "OK")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("ascii")
        writer.write(head + body_bytes)
        await writer.drain()


__all__ = ["CallbackServer", "CallbackResult"]
# silence unused
_: Callable[[str], Awaitable[None]] | None = None
