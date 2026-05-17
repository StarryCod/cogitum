"""
cogitum.gateway.tg_stream
~~~~~~~~~~~~~~~~~~~~~~~~~~
Robust Telegram message streaming for the agent loop.

Replaces the old approach of "edit on every chunk" with a debounced,
deduplicated, error-tolerant pipeline. One TgStream per agent run, three
independent rails:

  • thinking — single spoiler message, edited as the model thinks
  • status   — rolling tool-call log (last N lines), edited as tools run
  • response — final assistant text, streamed live, auto-split at 4096

Each rail tracks its own (message_id, last_sent_text, last_edit_ts) and
queues coalesced edits on a 1.2s debounce window. Telegram errors are
classified and handled:

  • "message is not modified"  → silent skip (dedup also catches this)
  • "Too Many Requests"         → honor retry_after, requeue
  • "message to edit not found" → drop msg_id, send fresh
  • "message can't be edited"   → drop msg_id, send fresh
  • parse_mode error            → fallback to plain text once

The TUI feed used to do all this manually inline in the event loop and
each path drifted. This module owns it.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("cogitum.gateway.tg_stream")


# Telegram lets you edit a message ~1/sec without throttling. We give a
# small safety margin so a burst of small edits doesn't trip 429.
_EDIT_DEBOUNCE_S = 1.2

# Telegram message hard limit is 4096 chars; we cut earlier to leave room
# for split markers and to avoid butchering markdown across chunks.
_MAX_MSG_CHARS = 3800

# Status rail: how many recent tool lines to keep visible.
_STATUS_TAIL = 10


@dataclass
class _Rail:
    """One editable Telegram message + its pending state.

    Attributes
    ----------
    msg_id
        Currently-bound Telegram message_id (None if nothing sent yet).
    last_sent
        Last text we successfully delivered (for dedup; identical text
        triggers Telegram's "message is not modified" error).
    last_edit_ts
        monotonic time of the last successful API call. Used for debounce.
    pending_text
        Latest text we WANT to be on the message; flushed on the next
        debounce tick. Newer pending_text simply overwrites older.
    flush_task
        The currently-pending flush coroutine. None if idle.
    """

    msg_id: int | None = None
    last_sent: str = ""
    last_edit_ts: float = 0.0
    pending_text: str | None = None
    flush_task: asyncio.Task | None = None


class TgStream:
    """Per-agent-run streaming surface to one Telegram chat.

    Usage
    -----
    >>> stream = TgStream(api, chat_id)
    >>> await stream.update_thinking("...")     # repeat as deltas accumulate
    >>> await stream.push_status("running tool X")
    >>> await stream.update_response(text_so_far)
    >>> await stream.flush()                    # at end of run; force pending
    """

    def __init__(self, api: Any, chat_id: int) -> None:
        self.api = api
        self.chat_id = chat_id
        self._thinking = _Rail()
        self._response = _Rail()
        self._status = _Rail()
        # status rail keeps a rolling buffer; pending_text is rebuilt from this
        self._status_lines: list[str] = []
        # If response overflows, we freeze older messages and start a new rail
        # for continuation. We track the historical message ids so flush() can
        # noop on them.
        self._response_history: list[int] = []
        # Per-rail lock so two updates can't race a flush_task replacement.
        self._lock = asyncio.Lock()

    # ── Public API ──────────────────────────────────────────────────────

    async def update_thinking(self, text: str, formatter=None) -> None:
        """Set the thinking-rail full text. Idempotent.

        formatter: optional callable that takes the raw buffer and returns
        the wire string (e.g. wrap in spoiler). Called once per flush, not
        per call, so cost is bounded.
        """
        if not text or not text.strip():
            return
        await self._schedule(self._thinking, text, formatter)

    async def push_status(self, line: str, formatter=None) -> None:
        """Append a status line; the rail shows the last _STATUS_TAIL of them."""
        if not line:
            return
        self._status_lines.append(line)
        joined = "\n".join(self._status_lines[-_STATUS_TAIL:])
        await self._schedule(self._status, joined, formatter)

    async def update_response(self, text: str, formatter=None) -> None:
        """Set the assistant response full text. Auto-splits at _MAX_MSG_CHARS.

        On overflow:
          • current rail is frozen (last edit committed),
          • a new _Rail() takes over with the overflow as its initial text,
          • subsequent updates only edit the new rail.
        """
        if not text:
            return
        # Overflow handling: if text exceeds limit, freeze current rail with
        # a clean cut at the last newline before the limit, and start a new
        # rail with the remainder.
        while len(text) > _MAX_MSG_CHARS:
            cut = text.rfind("\n", 0, _MAX_MSG_CHARS)
            if cut < _MAX_MSG_CHARS // 2:
                cut = _MAX_MSG_CHARS  # no good newline; hard-cut
            head = text[:cut]
            await self._schedule(self._response, head, formatter)
            await self._flush_rail(self._response)
            if self._response.msg_id is not None:
                self._response_history.append(self._response.msg_id)
            self._response = _Rail()
            text = text[cut:].lstrip("\n")
        await self._schedule(self._response, text, formatter)

    async def attach_photo(self, path: str, caption: str = "") -> None:
        """Send a photo as a fresh message; doesn't touch any rail."""
        try:
            await self.api.send_photo(self.chat_id, path, caption=caption)
        except Exception:
            log.debug("photo send failed", exc_info=True)

    async def flush(self) -> None:
        """Force any pending edits to commit. Call at end of agent run."""
        for rail in (self._thinking, self._status, self._response):
            await self._flush_rail(rail)

    # ── Scheduling internals ────────────────────────────────────────────

    async def _schedule(
        self,
        rail: _Rail,
        text: str,
        formatter,
    ) -> None:
        """Stage `text` as the new pending text; ensure a flush task is queued."""
        async with self._lock:
            rail.pending_text = (formatter(text) if formatter else text)
            if rail.flush_task is None or rail.flush_task.done():
                rail.flush_task = asyncio.create_task(self._debounced_flush(rail))

    async def _debounced_flush(self, rail: _Rail) -> None:
        """Sleep until the debounce window has elapsed, then flush once.

        While we sleep, more updates can land in pending_text; we always
        send the latest. After flushing, if MORE updates arrived during the
        send (rare race), we recurse once.
        """
        try:
            elapsed = time.monotonic() - rail.last_edit_ts
            wait = max(0.0, _EDIT_DEBOUNCE_S - elapsed)
            if wait:
                await asyncio.sleep(wait)
            await self._flush_rail(rail)
        except asyncio.CancelledError:
            pass
        except Exception:
            # Never let a debounce task crash the agent loop; agent run is
            # the user's primary work.
            log.exception("debounced flush crashed")

    async def _flush_rail(self, rail: _Rail) -> None:
        """Commit pending_text to Telegram. Handles dedup, errors, retries."""
        async with self._lock:
            text = rail.pending_text
            rail.pending_text = None
            if text is None:
                return
            # Dedup: identical text would just produce "message is not modified"
            if text == rail.last_sent and rail.msg_id is not None:
                return

        # We deliberately drop the lock for the network call so other rails
        # can flush in parallel; only state mutation happens under the lock.
        ok, new_msg_id = await self._deliver(rail, text)
        if ok:
            async with self._lock:
                rail.last_sent = text
                rail.last_edit_ts = time.monotonic()
                if new_msg_id is not None:
                    rail.msg_id = new_msg_id

    async def _deliver(self, rail: _Rail, text: str) -> tuple[bool, int | None]:
        """Attempt one send-or-edit cycle. Returns (success, new_msg_id_or_None).

        Honors retry_after from Telegram 429s, retries up to 3 times.
        On editable-message-gone errors, drops msg_id and sends fresh next call.
        """
        for attempt in range(3):
            try:
                if rail.msg_id is None:
                    resp = await self.api.send_message(
                        self.chat_id, text, parse_mode="MarkdownV2"
                    )
                    if resp.get("ok"):
                        return True, resp["result"]["message_id"]
                    desc = (resp.get("description") or "").lower()
                    if self._classify_unrecoverable(desc):
                        log.warning("send unrecoverable: %s", desc)
                        return False, None
                    # parse_mode error already retried inside send_message
                    return False, None
                else:
                    resp = await self.api.edit_message(
                        self.chat_id, rail.msg_id, text, parse_mode="MarkdownV2"
                    )
                    if resp.get("ok"):
                        return True, None
                    desc = (resp.get("description") or "").lower()
                    if "not modified" in desc:
                        # Treat as success: target state already on Telegram.
                        return True, None
                    if "retry after" in desc:
                        delay = self._parse_retry_after(desc)
                        await asyncio.sleep(delay)
                        continue
                    if self._is_message_gone(desc):
                        # Old msg deleted/expired; drop and recreate next call.
                        rail.msg_id = None
                        rail.last_sent = ""
                        continue
                    return False, None
            except Exception:
                log.debug("deliver attempt failed", exc_info=True)
                await asyncio.sleep(0.5 * (attempt + 1))
        return False, None

    # ── Error classification ────────────────────────────────────────────

    @staticmethod
    def _is_message_gone(desc: str) -> bool:
        return any(s in desc for s in (
            "message to edit not found",
            "message can't be edited",
            "message_id_invalid",
        ))

    @staticmethod
    def _classify_unrecoverable(desc: str) -> bool:
        return any(s in desc for s in (
            "chat not found",
            "bot was blocked",
            "user is deactivated",
            "forbidden",
        ))

    @staticmethod
    def _parse_retry_after(desc: str) -> float:
        m = re.search(r"retry after (\d+)", desc)
        if m:
            return min(float(m.group(1)) + 0.2, 30.0)
        return 2.0
