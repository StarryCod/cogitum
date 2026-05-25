"""
cogitum.gateway.telegram
~~~~~~~~~~~~~~~~~~~~~~~~~
Telegram bot gateway — runs as a daemon, connects Cogitum agent to Telegram.

Architecture:
  - Long-polling via httpx (no aiogram dependency)
  - One session per chat (persisted via SessionStore)
  - Full tool support, thinking display, media sending
  - Commands: /new, /resume, /title, /tools, /model, /models, /stop, /help
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import re
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from cogitum.core.agent import (
    Agent,
    AgentApprovalRequest,
    AgentCompacted,
    AgentConfig,
    AgentDone,
    AgentError,
    AgentRetry,
    AgentRetryConfirm,
    AgentText,
    AgentThinking,
    AgentToolCall,
    AgentToolResult,
    AgentTurnPersist,
)
# builtin_tools is imported for the side-effect of registering all
# @tool-decorated functions into REGISTRY at import time. The earlier
# `from ... import *` form leaked the module's private names (including
# `subprocess`, `signal`, helpers prefixed with `_`) into this namespace.
import cogitum.core.builtin_tools  # noqa: F401
from cogitum.core.events import Message
from cogitum.core.llm.loader import load_mesh, load_settings
from cogitum.core.llm.refresh import refresh_all_providers
from cogitum.core.sessions import get_store
from cogitum.core.tools import REGISTRY

from .tg_config import TelegramConfig, load_tg_config
from .tg_formatter import (
    escape_md,
    format_session_divider,
    format_thinking,
    format_tool_call,
    format_tool_result,
    markdown_to_tg,
    split_message,
)
from .tg_stream import TgStream
from .persona_lock import wrap_system_prompt

log = logging.getLogger("cogitum.gateway.telegram")


# ── Telegram API errors ──────────────────────────────────────────────────────


class TelegramAuthError(Exception):
    """Bot token is invalid, revoked, or shaped wrong (401/404).

    Fatal — the gateway must stop polling and surface this to the user.
    """


class TelegramSendError(Exception):
    """Telegram returned ok=false for a send/edit (non-401/404/429).

    Covers formatting errors that survived the parse_mode=None
    fallback, 'message too long', 'chat not found' for a chat the
    bot was kicked from, etc. Callers can catch this to surface a
    user-visible error instead of silently dropping the message.
    """

    def __init__(self, method: str, description: str | None = None) -> None:
        self.method = method
        self.description = description or ""
        super().__init__(f"Telegram {method} failed: {self.description!r}")


class TelegramRateLimitError(Exception):
    """Telegram returned 429 with a retry_after hint (in seconds).

    Constructor coerces and clamps the hint to [1, 300] so a malformed
    null / non-int / runaway value (TG sometimes hints 86400s = 24h)
    can't freeze the gateway.
    """

    _MIN_RETRY = 1
    _MAX_RETRY = 300  # 5 minutes — anything bigger and we still retry.
    _DEFAULT = 5

    def __init__(self, retry_after: Any) -> None:
        try:
            value = int(retry_after) if retry_after not in (None, "") else self._DEFAULT
        except (TypeError, ValueError):
            value = self._DEFAULT
        self.retry_after = max(self._MIN_RETRY, min(value, self._MAX_RETRY))
        super().__init__(f"rate-limited, retry after {self.retry_after}s")


# Module-level constant so tests and call sites share the contract. A
# refactor that drops the gate AND changes the message would still need
# to flip this string.
OPERATOR_ONLY_MSG = (
    "✕ operator-only command. Only the deployment owner "
    "(configured via 'cog tg setup') can use this. "
    "You can use /tools or /help freely."
)


# Regex used by TelegramAPI._scrub_token. Matches '/bot<TOKEN>/' shape
# that httpx exception strings expose when they include a request URL.
_TG_TOKEN_RE = re.compile(r"/bot[A-Za-z0-9:_\-]+/")


# ── Telegram API helpers ─────────────────────────────────────────────────────

class TelegramAPI:
    """Minimal Telegram Bot API client via httpx."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=60.0)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def call(self, method: str, **kwargs) -> dict[str, Any]:
        client = await self._ensure_client()
        # F54+F60 retry: a transient ReadTimeout/ConnectError on the way
        # out (typical VPN drop on cellular tethering) used to take down
        # the entire turn — caller saw "✕ Error: ConnectError" with no
        # recovery. Retry up to 3 attempts with 1/2/4s backoff for the
        # narrow set of httpx network exceptions that are actually
        # transient. Anything else (HTTPStatusError, malformed JSON,
        # PoolTimeout) propagates immediately. We deliberately retry
        # ALL methods including getUpdates: the polling loop already
        # catches TimeoutException so its perceived behaviour doesn't
        # change, but a brief connect blip won't bounce it through the
        # outer backoff path anymore.
        last_exc: Exception | None = None
        attempts = 3
        for attempt in range(attempts):
            try:
                resp = await client.post(f"{self.base}/{method}", json=kwargs)
                break
            except (
                httpx.ReadTimeout,
                httpx.ConnectError,
                httpx.RemoteProtocolError,
            ) as exc:
                last_exc = exc
                if attempt + 1 >= attempts:
                    raise
                # 1s, 2s, 4s — capped before we'd hit the next attempt.
                await asyncio.sleep(2 ** attempt)
        else:  # pragma: no cover — loop always exits via break/raise
            if last_exc is not None:
                raise last_exc
        data = resp.json()
        # Stash the HTTP status under a private metadata key so callers
        # can distinguish 401/404/429 from a generic ok=false. Telegram
        # never uses leading-underscore field names so this won't collide.
        if isinstance(data, dict):
            data["_http_status"] = resp.status_code
        if not data.get("ok"):
            log.warning("TG API error: %s → %s", method, data.get("description"))
        # Promote 401/404 from any call() — not just get_updates — so a
        # token revoke that lands mid-conversation surfaces loudly
        # instead of silently failing send_message / edit_message /
        # answer_callback / send_typing. Caller catches and triggers
        # the same shutdown path as the polling loop.
        if isinstance(data, dict) and data.get("_http_status") in (401, 404):
            raise TelegramAuthError(
                f"Telegram {method} returned {data['_http_status']}: "
                f"{data.get('description')!r}"
            )
        # F54: HTTP 409 on getUpdates means another instance is polling
        # the same bot. Surface a helpful fatal so the user knows to
        # `cog tg stop` first instead of staring at backoff loops.
        if (
            isinstance(data, dict)
            and data.get("_http_status") == 409
            and method == "getUpdates"
        ):
            log.critical(
                "Another bot instance is polling. Stop it with `cog tg stop` "
                "before running in foreground."
            )
            raise TelegramAuthError(
                "Telegram getUpdates returned 409 Conflict — another bot "
                "polling. Stop with `cog tg stop`."
            )
        return data

    @staticmethod
    def _scrub_token(s: str) -> str:
        """Redact '/bot<TOKEN>/' from any string before logging.

        httpx exceptions that include the request URL leak the bot
        token into log output at CRITICAL severity; this strips them
        to a fixed placeholder.
        """
        if not s:
            return s
        return _TG_TOKEN_RE.sub("/bot<REDACTED>/", s)

    async def get_updates(self, offset: int = 0, timeout: int = 30) -> list[dict]:
        try:
            data = await self.call("getUpdates", offset=offset, timeout=timeout)
        except TelegramAuthError:
            # call() raises directly on 401/404; re-raise unchanged so
            # the poll loop's branch fires.
            raise
        status = data.get("_http_status", 200)
        # 429: rate limited. Telegram tells us how long to wait — honour it.
        if status == 429:
            retry_after = (data.get("parameters") or {}).get("retry_after")
            raise TelegramRateLimitError(retry_after)
        # Other 4xx/5xx (transient infra hiccups, malformed request) keep
        # the previous behaviour: warning was already logged in .call(),
        # return an empty result and let the loop continue.
        return data.get("result", [])

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = "MarkdownV2",
        reply_to: int | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        # F32 auto-split: Telegram caps a single sendMessage at 4096
        # chars. /tools with 200+ MCP tools, big tracebacks in compact
        # failures, and any other length-unaware caller used to come
        # back with ok=false 'message is too long' → TelegramSendError
        # → handler crashed. Split locally on a newline-aware boundary
        # (split_message) and chain the sends. The reply_markup goes
        # on the LAST chunk only so buttons attach to the tail (where
        # the user's eye lands), and the return value mirrors the last
        # send so callers that store message_id keep working.
        if len(text) > 4000:
            chunks = split_message(text, max_len=4000)
            last_data: dict = {}
            for idx, chunk in enumerate(chunks):
                is_last = idx == len(chunks) - 1
                last_data = await self._send_message_single(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=parse_mode,
                    reply_to=reply_to if idx == 0 else None,
                    reply_markup=reply_markup if is_last else None,
                )
            return last_data
        return await self._send_message_single(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_to=reply_to,
            reply_markup=reply_markup,
        )

    async def _send_message_single(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = "MarkdownV2",
        reply_to: int | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
        }
        if parse_mode:
            kwargs["parse_mode"] = parse_mode
        if reply_to:
            kwargs["reply_to_message_id"] = reply_to
        if reply_markup:
            kwargs["reply_markup"] = reply_markup
        data = await self.call("sendMessage", **kwargs)
        if not data.get("ok"):
            # Fallback: try without parse_mode (formatting error). The
            # previous version did ``text.replace("\\", "")`` to strip
            # MarkdownV2 escapes — but that also kills genuine
            # backslashes in Windows paths, regex literals, JSON
            # payloads, and any LLM-generated content the operator
            # might be piping into the chat. Resending the SAME text
            # with parse_mode=None tells Telegram to render it as
            # plain text, no escape interpretation, no data loss.
            if parse_mode:
                log.warning(
                    "Markdown send failed, retrying plain: %s",
                    data.get("description"),
                )
                kwargs.pop("parse_mode", None)
                # text is unchanged on purpose — see comment above.
                data = await self.call("sendMessage", **kwargs)
        if not data.get("ok"):
            # Final ok=false (no parse_mode left to strip, or the
            # plain retry also failed). Surface to callers via a
            # typed exception so a 'chat not found' / 'message too
            # long' / 'bot was kicked' doesn't silently disappear
            # at one of the ~30 send_message call sites.
            desc = data.get("description")
            log.warning("sendMessage ok=false (final): %s", desc)
            raise TelegramSendError("sendMessage", desc)
        return data

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: str = "MarkdownV2",
    ) -> dict:
        kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if parse_mode:
            kwargs["parse_mode"] = parse_mode
        data = await self.call("editMessageText", **kwargs)
        if not data.get("ok") and parse_mode:
            # Same fix as send_message: re-send with parse_mode=None
            # rather than stripping backslashes (which destroys real
            # data — Windows paths, regex, JSON).
            kwargs.pop("parse_mode", None)
            data = await self.call("editMessageText", **kwargs)
        return data

    async def send_document(self, chat_id: int, path: str, caption: str = "") -> dict:
        client = await self._ensure_client()
        with open(path, "rb") as f:
            files = {"document": (Path(path).name, f)}
            data_fields: dict[str, Any] = {"chat_id": str(chat_id)}
            if caption:
                data_fields["caption"] = caption
            resp = await client.post(
                f"{self.base}/sendDocument", data=data_fields, files=files
            )
        return resp.json()

    async def send_photo(self, chat_id: int, path: str, caption: str = "") -> dict:
        client = await self._ensure_client()
        with open(path, "rb") as f:
            files = {"photo": (Path(path).name, f)}
            data_fields: dict[str, Any] = {"chat_id": str(chat_id)}
            if caption:
                data_fields["caption"] = caption
            resp = await client.post(
                f"{self.base}/sendPhoto", data=data_fields, files=files
            )
        return resp.json()

    async def get_file(self, file_id: str) -> str | None:
        """Get file path from Telegram, download to /tmp, return local path.

        Uses tempfile.mkstemp (atomic create + unique name + correct
        perms) instead of the deprecated mktemp(). Caller is
        responsible for unlinking when done; the gateway's per-task
        TTL cleanup (see CogitumBot._tg_tempfiles) sweeps these
        after the agent task finishes.
        """
        data = await self.call("getFile", file_id=file_id)
        if not data.get("ok"):
            return None
        file_path = data["result"]["file_path"]
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        client = await self._ensure_client()
        resp = await client.get(url)
        if resp.status_code != 200:
            return None
        ext = Path(file_path).suffix or ".bin"
        # mkstemp returns (fd, name); close fd immediately and write
        # via Path so we don't leak a descriptor.
        fd, local = tempfile.mkstemp(suffix=ext, prefix="cogitum_tg_")
        try:
            os.close(fd)
        except OSError:
            pass
        Path(local).write_bytes(resp.content)
        return local

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    async def send_typing(self, chat_id: int) -> None:
        """Send 'typing...' chat action."""
        await self.call("sendChatAction", chat_id=chat_id, action="typing")

    async def set_my_commands(self, commands: list[dict[str, str]]) -> None:
        """Register bot commands for the menu."""
        await self.call("setMyCommands", commands=commands)

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        """Delete a message."""
        await self.call("deleteMessage", chat_id=chat_id, message_id=message_id)


class _TokenScrubFilter(logging.Filter):
    """Logging filter that scrubs '/bot<TOKEN>/' from message + traceback.

    log.exception() formats exc_info into record.exc_text lazily — the
    formatter walks the traceback chain and any httpx error whose str()
    includes the request URL leaks the bot token there. _scrub_token()
    on the message string alone is not enough; the traceback is rendered
    separately. This filter rewrites both surfaces just before emit.

    Installed once at module import on the gateway logger so every
    handler downstream sees the redacted form.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if record.msg and isinstance(record.msg, str):
                record.msg = TelegramAPI._scrub_token(record.msg)
            if record.args and isinstance(record.args, tuple):
                record.args = tuple(
                    TelegramAPI._scrub_token(a) if isinstance(a, str) else a
                    for a in record.args
                )
            # exc_text is None until the first formatter call; force-render
            # so we can scrub it ahead of any handler.
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(
                    record.exc_info
                )
            if record.exc_text:
                record.exc_text = TelegramAPI._scrub_token(record.exc_text)
        except Exception:
            # Never break logging — return True regardless.
            pass
        return True


# Install once on the ROOT logger so the filter applies to every
# child logger (httpx, asyncio, urllib3, anyio, etc.) — third-party
# libs that log the request URL would otherwise bypass scrubbing
# entirely. Re-imports during test reloads guard via attribute probe
# to keep the handler list clean.
def install_token_scrub_filter() -> None:
    """(Re)attach the token-scrub filter to the root logger.

    Idempotent: probe-attribute prevents double-install. Called at
    import time and again from CogitumBot.start() so a downstream
    ``logging.config.dictConfig({...})`` (which wipes filters from
    every existing logger) cannot strip our scrubber.
    """
    rl = logging.getLogger()
    # dictConfig replaces the filter list outright, so the probe alone
    # is not enough — also check that an instance is actually present.
    has_filter = any(isinstance(f, _TokenScrubFilter) for f in rl.filters)
    if not has_filter:
        rl.addFilter(_TokenScrubFilter())
    rl._token_scrub_installed = True  # type: ignore[attr-defined]
    log._token_scrub_installed = True  # type: ignore[attr-defined]


# F43: token-scrub filter installation is performed lazily by
# ``CogitumBot.start()`` and ``run_bot()`` — NOT at module import.
# A module-level call mutated the root logger as an import-time side
# effect, which broke pytest log capture and any logging.dictConfig
# loaded after this import.


# ── Session state ────────────────────────────────────────────────────────────

class ChatSession:
    """Per-chat state."""

    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id
        self.session_id: str | None = None
        self.history: list[Message] = []
        self.agent_task: asyncio.Task | None = None
        self._cancel_flag = False
        self._last_platform: str = "telegram"
        self._approval_queue: asyncio.Queue | None = None

    @property
    def is_busy(self) -> bool:
        return self.agent_task is not None and not self.agent_task.done()

    def cancel(self) -> None:
        self._cancel_flag = True
        if self.agent_task and not self.agent_task.done():
            self.agent_task.cancel()


# ── Main bot ─────────────────────────────────────────────────────────────────

class CogitumBot:
    """Telegram gateway bot."""

    def __init__(self, config: TelegramConfig) -> None:
        self.config = config
        self.api = TelegramAPI(config.bot_token)
        self.sessions: dict[int, ChatSession] = {}
        self.mesh = None
        self.agent: Agent | None = None
        # Telegram caps callback_data at 64 bytes per inline button. The
        # naive "approve:<call_id>" format works for short Anthropic /
        # OpenAI ids (up to ~50 chars including the prefix), but breaks
        # silently with long MCP / composite ids — TG truncates the
        # payload and our callback handler then can't match the pending
        # future, leaving the user with "▲ No pending approval" forever.
        # We map every emitted call_id to a short token (8 hex chars,
        # collision space ~4G — enough for 60s approval window) and
        # round-trip through this lookup. The full id is still the
        # authoritative key in Agent._approval_futures.
        self._approval_token_to_call_id: collections.OrderedDict[str, str] = (
            collections.OrderedDict()
        )
        # Cap on pending approval tokens. Operator can't realistically
        # have more than this many tools awaiting approval at once;
        # without a cap the dict grows unbounded if the operator goes
        # AFK in YOLO=off mode.
        self._approval_token_max = 64
        # F27: also persist the token map to disk on every insert so a
        # crash mid-conversation doesn't leave click-zombie buttons.
        # On startup we restore it (best-effort), and the callback
        # handler edits stale entries to a "[stale — bot restarted,
        # ignore]" message instead of silently no-oping.
        self._approval_persist_path = self._approval_path()
        self._restore_approval_tokens()
        self._running = False
        self._offset = self._load_offset()
        self._mcp_watcher_task: asyncio.Task | None = None
        # Track the polling task so stop() can cancel it directly.
        # Without this, ``self._running = False`` only takes effect AFTER
        # the next ``get_updates(timeout=30)`` returns (≈30s in the worst
        # case), which is why "stop пишет ✓ но бот всё ещё работает".
        self._poll_task: asyncio.Task | None = None
        # Dedup ring for callback_query IDs. Telegram retries unanswered
        # callbacks for ~15s, so a stale handler crash can otherwise cause
        # the same approval click to fire 2-3 times. We remember the last
        # 256 callback IDs we've seen and drop duplicates.
        self._seen_callbacks: collections.OrderedDict[str, float] = collections.OrderedDict()
        self._seen_callbacks_max = 256
        # /godmode persistence — remember the system prompt that was
        # active before the user toggled godmode, so /godmode off can
        # restore it exactly. Single-bot scope is fine: the gateway
        # has one Agent shared across chats. None means godmode is
        # off; non-None means it's on.
        self._pre_godmode_system: str | None = None
        # Bound concurrency on parallel update handlers — without this, a
        # spammer (or our own retry loop) can spawn unbounded tasks.
        # Tier-4 fairness fix: a single chat spamming 8+ messages used
        # to saturate a global Semaphore(8), starving other allowed
        # chats. Now we cap per-chat (default 4) AND globally (32) so
        # one chat can't monopolize the queue.
        self._global_sem = asyncio.Semaphore(32)
        self._chat_sems: dict[int, asyncio.Semaphore] = {}
        self._chat_sem_users: dict[int, int] = {}
        self._per_chat_sem_size = 4
        self._update_tasks: set[asyncio.Task] = set()
        # Backoff state for the poll loop.
        self._poll_backoff = 1.0

    # ── Offset persistence ──────────────────────────────────────────────
    #
    # Without this, restarting the bot resets _offset to 0 and Telegram
    # re-delivers every update from the last 24 hours — which means the
    # user sees duplicate replies after every restart. We persist the
    # offset to disk after each successful batch.

    @staticmethod
    def _offset_path() -> Path:
        from ..core.platform_paths import get_data_dir
        return get_data_dir() / "tg_offset"

    @staticmethod
    def _approval_path() -> Path:
        """F27: location of the persisted approval-token map.

        Stored next to ``tg_offset`` for the same reason — both are
        ephemeral runtime state that lives in the user's data dir,
        never in the repo or in $TMP.
        """
        from ..core.platform_paths import get_data_dir
        return get_data_dir() / "tg_approvals.json"

    def _save_approval_tokens_sync(self) -> None:
        """Atomically rewrite the approval-token map (blocking).

        Best-effort. The persistence is purely an operability nicety
        (restart safety for in-flight approvals); a write failure
        must NEVER bubble up into the chat handler that just inserted
        a token, otherwise a flaky disk would break new approvals.

        File mode is forced to 0o600 after the atomic rename — the
        approval-token map is not a secret per se, but it leaks the
        live tool-call ids of an active agent, which an attacker on
        the same host can replay against the future-table inside the
        approval window.

        This is the blocking implementation; in async contexts call
        ``_save_approval_tokens`` (the awaitable wrapper) so the
        write is offloaded to a thread and we don't stutter the
        event loop on a slow filesystem (network FS, fuse mounts).
        """
        try:
            import json as _json
            import os as _os
            from ..core.atomic_io import atomic_write_text
            payload = _json.dumps(
                dict(self._approval_token_to_call_id),
                ensure_ascii=False,
            )
            atomic_write_text(self._approval_persist_path, payload)
            try:
                _os.chmod(self._approval_persist_path, 0o600)
            except OSError:
                # Non-fatal — best-effort hardening only. On Windows
                # chmod has limited semantics; on a read-only mount the
                # write would have failed earlier anyway.
                log.debug("approval persist chmod failed", exc_info=True)
        except Exception:
            log.debug("approval persist failed", exc_info=True)

    async def _save_approval_tokens(self) -> None:
        """Async wrapper around :meth:`_save_approval_tokens_sync`.

        Offloads the atomic write + chmod to a worker thread via
        ``asyncio.to_thread`` so a slow disk (NFS, fuse, encrypted
        volumes) doesn't block the Telegram event loop while the
        gateway is in the middle of a callback handler.
        """
        await asyncio.to_thread(self._save_approval_tokens_sync)

    # F27: cap on the number of approval tokens we restore from disk.
    # Without a cap, an attacker (or a buggy run) that bloated the
    # JSON file to a few MB would force every restart to load that
    # entire blob into memory and the OrderedDict.
    _APPROVAL_RESTORE_CAP = 1024

    def _restore_approval_tokens(self) -> None:
        """Best-effort restore from disk on startup.

        On a fresh install the file is absent and we silently move on.
        Stale entries (the corresponding agent run is gone) are still
        loaded — the callback handler treats them as stale and edits
        the message to make that clear, which is much better UX than
        a silent click that does nothing.

        If the persisted map exceeds ``_APPROVAL_RESTORE_CAP`` entries,
        only the last N (most recent insertion order) are kept; the
        rest are dropped with a warning. JSON object ordering is
        preserved by ``json.loads`` on Python 3.7+, so "last N" is the
        same insertion order ``_save_approval_tokens`` wrote.
        """
        try:
            import json as _json
            raw = self._approval_persist_path.read_text(encoding="utf-8")
            data = _json.loads(raw)
            if isinstance(data, dict):
                items = [
                    (k, v) for k, v in data.items()
                    if isinstance(k, str) and isinstance(v, str)
                ]
                cap = self._APPROVAL_RESTORE_CAP
                if len(items) > cap:
                    log.warning(
                        "tg_approvals: %d entries on disk exceeds cap %d; "
                        "keeping last %d most recent",
                        len(items), cap, cap,
                    )
                    items = items[-cap:]
                for k, v in items:
                    self._approval_token_to_call_id[k] = v
        except FileNotFoundError:
            return
        except Exception:
            log.debug("approval restore failed", exc_info=True)

    def _load_offset(self) -> int:
        """Read the persisted update offset from disk.

        Returns:
          - The persisted offset on success.
          - 0 if the file simply doesn't exist (fresh install / first run).
          - -1 sentinel if the file exists but is corrupt — treated as
            "skip backlog, jump to latest pending" by the poll loop.
            Without this, a corrupt offset file silently coerced to 0
            and Telegram replayed up to 24h of buffered updates after
            every restart (real sev-2 user complaint).
        """
        path = self._offset_path()
        try:
            return int(path.read_text().strip())
        except FileNotFoundError:
            return 0
        except ValueError as e:
            log.warning(
                "tg_offset corrupt at %s (%s: %s); skipping backlog via offset=-1",
                path, type(e).__name__, e,
            )
            return -1
        except OSError as e:
            log.warning(
                "tg_offset unreadable at %s (%s: %s); skipping backlog via offset=-1",
                path, type(e).__name__, e,
            )
            return -1

    def _save_offset(self) -> None:
        try:
            from ..core.atomic_io import atomic_write_text
            atomic_write_text(self._offset_path(), str(self._offset))
        except Exception:
            # Was OSError-only — but atomic_write_text on Windows can
            # raise PermissionError when OneDrive / antivirus locks the
            # file mid-rename, and cloud sync layers throw their own
            # subclasses (notably nothing inherits from OSError on
            # some shim filesystems). Letting that propagate up to
            # _poll_loop killed the bot. Best-effort persistence is
            # fine: we'll re-try on the next batch and lose at most
            # one update worth of replay.
            log.exception(
                "Failed to persist tg_offset to %s",
                self._offset_path(),
            )

    # ── Operator ACL ────────────────────────────────────────────────────
    #
    # Tier-3 fix: /yolo, /godmode, /model and the inline `model:` callback
    # all mutate the SHARED Agent / AgentConfig instance — so a privileged
    # group member could flip yolo, swap the system prompt, or change the
    # model for everyone, including the operator's private chat. These
    # commands must be locked to the operator (allowed_user_id) only.
    # Read-only listings (/tools, /models, /compact, /reload) stay open
    # because they don't touch shared agent state.

    def _is_operator(self, user_id: int, chat_id: int) -> bool:
        """Return True iff this user is allowed to mutate shared agent state.

        Two modes:
          - Locked deploy (allowed_user_id != 0): only that user id.
          - Fully-open mode (allowed_user_id == 0 AND no allowed_chat_ids):
            mirror can_respond's private-chat semantics — only the 1:1
            user (chat_id == user_id) can mutate. Group context never
            qualifies as operator in any mode.
        """
        if self.config.allowed_user_id != 0:
            return user_id == self.config.allowed_user_id
        # Open mode — only the private 1:1 partner is the operator.
        if not self.config.allowed_chat_ids:
            return chat_id == user_id and user_id != 0
        # Allowlisted chats but no operator id — refuse all mutations.
        return False

    async def _deny_operator_only(
        self, *, chat_id: int | None = None, callback_id: str | None = None,
    ) -> None:
        """Send the unified operator-only rejection.

        Use the same OPERATOR_ONLY_MSG constant for command sends and
        callback toasts so log-grep / UI / tests share one contract.
        """
        if callback_id is not None:
            await self.api.answer_callback(callback_id, OPERATOR_ONLY_MSG)
        if chat_id is not None:
            await self.api.send_message(chat_id, escape_md(OPERATOR_ONLY_MSG))

    # ── Callback dedup ──────────────────────────────────────────────────

    def _is_duplicate_callback(self, cb_id: str) -> bool:
        """True if we've already seen this callback_query.id recently."""
        now = time.monotonic()
        # Drop entries older than 60s (Telegram retries window).
        for old_id, ts in list(self._seen_callbacks.items()):
            if now - ts > 60:
                self._seen_callbacks.pop(old_id, None)
            else:
                break
        if cb_id in self._seen_callbacks:
            return True
        self._seen_callbacks[cb_id] = now
        # Bounded ring.
        while len(self._seen_callbacks) > self._seen_callbacks_max:
            self._seen_callbacks.popitem(last=False)
        return False

    async def start(self) -> None:
        """Initialize mesh and start polling.

        Pre-step: a single getMe round-trip catches an
        invalid/revoked/expired token (or any other ok=false response)
        before we waste seconds refreshing every provider. After the
        token check we register operator-only commands in the bot menu.
        """
        # Re-attach the token-scrub filter in case downstream code (or
        # the host application) called ``logging.config.dictConfig``
        # after our import-time install — dictConfig wipes the root
        # logger's filter list, so without this the filter would be
        # silently gone before the first poll. install_token_scrub_filter
        # is idempotent.
        install_token_scrub_filter()
        log.info("Starting Cogitum Telegram gateway...")

        # Token sanity check first — a single getMe round-trip catches
        # an invalid/revoked token in <1s, before we waste several seconds
        # refreshing every provider and loading the mesh. Without this,
        # operators saw the bot 'start' (with all the model-refresh logs)
        # and then sit in a 30/min 401 busy-loop.
        try:
            health = await self.api.call("getMe")
        except TelegramAuthError as e:
            # call() raises this on 401/404 — token is dead.
            log.critical(
                "Telegram bot token is invalid or revoked (%s). "
                "Run `cog setup` again. Not starting gateway.",
                self.api._scrub_token(str(e)),
            )
            return
        except Exception as e:
            # Other failures (DNS, proxy, ...) — never log e raw because
            # httpx exceptions can include the bot URL with the token.
            log.critical(
                "Telegram getMe health check failed: %s. Cannot start gateway.",
                self.api._scrub_token(str(e)),
            )
            return
        status = health.get("_http_status", 200)
        # Bail on ANY ok=false from getMe — TG returns ok=false+200 for
        # 'Token expired', 'TOKEN_INVALID', 'Forbidden' etc., none of
        # which contain the literal word 'unauthorized'.
        if status in (401, 404) or not health.get("ok"):
            log.critical(
                "Telegram bot token is invalid or revoked "
                "(getMe status=%s, desc=%r). "
                "Run `cog setup` again. Not starting gateway.",
                status, health.get("description"),
            )
            return

        # Auto-discover models for every configured provider before
        # building the mesh, so /models picker has fresh data.
        log.info("Refreshing models from all providers...")
        try:
            refresh = await refresh_all_providers(timeout=6.0, only_empty=False)
            for pid, r in refresh.items():
                log.info("  %-20s %s — %s", pid, r["status"], r["message"])
        except Exception as e:
            log.warning("model refresh failed (non-fatal): %s", e)

        # Load mesh
        self.mesh = load_mesh()
        if not self.mesh.providers:
            log.error("No providers configured. Run `cog setup` first.")
            return

        settings = load_settings()
        model = self.config.default_model or settings.get("default_model", "")

        # Resolve model
        if model and self.mesh.resolve(model):
            current_model = model
        else:
            pairs = self.mesh.list_resolved()
            current_model = pairs[0].qualified_id if pairs else None

        if not current_model:
            log.error("No models available.")
            return

        # Build the agent's system prompt:
        #   1. Start with AgentConfig's default Cogitum persona.
        #   2. If telegram.toml sets default_skill, load that skill's
        #      content and append — this is how operators put the bot
        #      into "moderator mode" (skill name 'tg-moderator') for
        #      group chats. Skill content overrides default behaviour
        #      because it comes later in the prompt.
        #   3. Wrap with persona_lock — anti-injection guard against
        #      "ignore previous instructions" and forged <system>
        #      tags coming through Telegram messages.
        # Tool access: skill 'tg-moderator' implies tools_enabled=False
        # (chat-only). Other skills leave tools as-is.
        from ..core.skills import read_skill

        agent_cfg = AgentConfig(model=current_model, platform="telegram")

        skill_name = (self.config.default_skill or "").strip()
        tools_enabled = True
        if skill_name:
            skill_body = read_skill(skill_name)
            if skill_body:
                agent_cfg.system = (
                    agent_cfg.system.rstrip()
                    + "\n\n═══ ACTIVE SKILL: " + skill_name + " ═══\n"
                    + skill_body.strip()
                )
                # Skills whose name starts with 'tg-moderator' are
                # explicitly conversational — disable the tool layer
                # so the agent can't accidentally exfiltrate group
                # data via web_search etc.
                if skill_name.startswith("tg-moderator"):
                    tools_enabled = False
            else:
                log.warning(
                    "telegram.default_skill=%r not found; falling back to default persona",
                    skill_name,
                )

        agent_cfg.system = wrap_system_prompt(agent_cfg.system)
        agent_cfg.tools_enabled = tools_enabled

        self.agent = Agent(
            mesh=self.mesh,
            registry=REGISTRY,
            config=agent_cfg,
        )

        # MCP: connect configured servers and register their tools
        try:
            from cogitum.core.mcp import (
                discover_mcp_tools,
                load_config,
                start_watcher,
            )
            from cogitum.core.mcp.sampling import build_sampling_callback
            mcp_cfg = load_config()
            cb = build_sampling_callback(self.mesh, current_model)
            result = discover_mcp_tools(REGISTRY, mcp_cfg, sampling_callback=cb)
            connected = sum(
                1 for s in result.get("servers", []) if s.get("state") == "connected"
            )
            log.info(
                "MCP: %d servers connected, %d tools registered",
                connected, len(result.get("registered", [])),
            )
            for s in result.get("servers", []):
                if s.get("state") != "connected":
                    log.warning(
                        "MCP server %r %s: %s",
                        s.get("name"), s.get("state"), s.get("last_error"),
                    )

            # Start the mcp.toml file watcher so external edits
            # (cog mcp add/remove/risk, hand edits, TUI Setup) are picked up
            # without restarting the daemon.
            async def _mcp_rebuild() -> None:
                fresh_cfg = load_config()
                fresh_cb = build_sampling_callback(
                    self.mesh, self.agent.cfg.model if self.agent else current_model
                )
                rs = discover_mcp_tools(REGISTRY, fresh_cfg, sampling_callback=fresh_cb)
                added = len(rs.get("registered", []))
                removed = len(rs.get("unregistered", []))
                connected = sum(
                    1 for s in rs.get("servers", []) if s.get("state") == "connected"
                )
                log.info(
                    "MCP watcher reconcile: %d connected, +%d tools, -%d tools",
                    connected, added, removed,
                )

            self._mcp_watcher_task = start_watcher(_mcp_rebuild)
        except Exception as e:
            log.warning("MCP discovery failed (non-fatal): %s", e)
            self._mcp_watcher_task = None

        self._running = True
        log.info("Gateway ready. Model: %s. Polling...", current_model)

        # Register bot commands menu. Operator-only commands carry an
        # explicit '(operator)' marker in the description so a group
        # member browsing the menu sees up-front which actions will
        # bounce back ✕ operator-only.
        await self.api.set_my_commands([
            {"command": "new", "description": "✦ New session (operator)"},
            {"command": "resume", "description": "◆ Resume session (operator)"},
            {"command": "title", "description": "✏️ Rename current session (operator)"},
            {"command": "tools", "description": "⚙ List available tools"},
            {"command": "models", "description": "◇ Pick model (operator)"},
            {"command": "model", "description": "⟳ Switch model directly (operator)"},
            {"command": "reload", "description": "♻️ Reload providers/models (operator)"},
            {"command": "godmode", "description": "▲ Jailbreak prompt (operator)"},
            {"command": "yolo", "description": "◈ Auto-approve all tools (operator)"},
            {"command": "compact", "description": "⟳ Compact context now (operator)"},
            {"command": "stop", "description": "⏹ Cancel generation (operator)"},
            {"command": "help", "description": "❓ All commands"},
        ])

        # Notify operator (or each allowed group) that gateway restarted.
        # Sending to chat_id=0 in chat-only deployments would just
        # produce a TG 'Bad Request: chat not found' warning — skip it
        # and target the actual recipients instead.
        welcome_text = (
            "⟳ *Cogitum Gateway restarted*\n\n"
            f"Model: `{escape_md(current_model)}`\n"
            f"▲ {escape_md('Previous session was reset. Use /resume to continue a saved session.')}\n\n"
            f"{escape_md('Tools:')} `{escape_md(str(len(REGISTRY.names())))}`"
        )
        if self.config.allowed_user_id:
            await self.api.send_message(self.config.allowed_user_id, welcome_text)
        elif self.config.allowed_chat_ids:
            for cid in self.config.allowed_chat_ids:
                try:
                    await self.api.send_message(cid, welcome_text)
                except TelegramAuthError:
                    raise
                except Exception:
                    log.debug("welcome to chat %s failed", cid, exc_info=True)

        try:
            self._poll_task = asyncio.create_task(self._poll_loop())
            await self._poll_task
        except asyncio.CancelledError:
            # stop() cancelled the poll loop — clean shutdown, not an error.
            pass
        finally:
            await self.api.close()
            if self.mesh:
                await self.mesh.aclose()
            if self._mcp_watcher_task is not None:
                self._mcp_watcher_task.cancel()
                try:
                    await self._mcp_watcher_task
                except (asyncio.CancelledError, Exception):
                    pass
            try:
                from cogitum.core.mcp import shutdown_mcp
                shutdown_mcp()
            except Exception:
                log.debug("mcp shutdown failed", exc_info=True)

    async def stop(self) -> None:
        """Stop the gateway promptly.

        Strategy:
          1. Flag ``_running = False`` so the poll loop won't restart.
          2. Cancel the poll task directly — this aborts the in-flight
             ``get_updates(timeout=30)`` instantly instead of waiting up
             to 30s for the long-poll to return naturally. Without this,
             the daemon ``stop`` looked instantaneous in the TUI but the
             actual process kept running until Telegram replied.
          3. Cancel any in-flight session handlers so users don't see
             half-sent responses after stop.
          4. Close the httpx client as a belt-and-suspenders fallback —
             if get_updates somehow survived cancellation, this kills
             the underlying socket.
        """
        self._running = False
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
        for session in self.sessions.values():
            session.cancel()
        # F4: cancel any pending approval futures so an in-flight
        # AgentApprovalRequest the operator never answered doesn't
        # dangle as an orphan task after the bot exits.
        agent = getattr(self, "agent", None)
        if agent is not None:
            try:
                await agent.aclose()
            except Exception:
                log.debug("agent aclose during bot.stop", exc_info=True)
        # Closing the API client breaks any HTTP request that was still
        # mid-flight when cancellation arrived.
        try:
            await self.api.close()
        except Exception:
            log.debug("api close during stop", exc_info=True)

    # ── Mesh reload ──────────────────────────────────────────────────────────

    async def _reload_mesh(self, *, silent: bool = False, chat_id: int | None = None) -> None:
        """Re-read providers.toml and rebuild the mesh in place.

        Also re-runs auto-discovery for all providers so newly-added
        models appear without restart.

        Preserves the current model if still available; falls back to first
        resolved model otherwise. Updates self.agent.mesh and cfg.model so the
        next request uses the fresh mesh.
        """
        old_model = self.agent.cfg.model if self.agent else None
        old_mesh = self.mesh

        # Re-read secrets.env so freshly-saved keys (via TUI wizard or
        # `cog secret set`) are picked up without a daemon restart.
        try:
            from ..core.llm.secrets_env import load_secrets_into_environ
            load_secrets_into_environ(override=False)
        except Exception:
            log.debug("swallowed exception", exc_info=True)

        # Auto-discover models for every provider first
        try:
            refresh = await refresh_all_providers(timeout=6.0, only_empty=False)
            log.info("reload refresh: %s", {k: v["message"] for k, v in refresh.items()})
        except Exception as e:
            log.warning("reload refresh failed: %s", e)

        try:
            new_mesh = load_mesh()
        except Exception as e:
            log.exception("mesh reload failed")
            if not silent and chat_id is not None:
                await self.api.send_message(
                    chat_id, escape_md(f"✕ reload failed: {e}")
                )
            return

        if not new_mesh.providers:
            if not silent and chat_id is not None:
                await self.api.send_message(
                    chat_id, escape_md("No providers in providers.toml. Run `cog setup`.")
                )
            return

        # Pick a model: keep current if still available, else first resolved
        if old_model and new_mesh.resolve(old_model):
            current_model = old_model
        else:
            pairs = new_mesh.list_resolved()
            current_model = pairs[0].qualified_id if pairs else None

        if not current_model:
            if not silent and chat_id is not None:
                await self.api.send_message(
                    chat_id, escape_md("No models available after reload.")
                )
            return

        # Swap mesh on agent + close old one
        self.mesh = new_mesh
        if self.agent is not None:
            self.agent.mesh = new_mesh
            self.agent.cfg.model = current_model
        if old_mesh is not None and old_mesh is not new_mesh:
            try:
                await old_mesh.aclose()
            except Exception:
                log.debug("swallowed exception", exc_info=True)

        # Reconcile MCP: re-read mcp.toml and add/remove/reconnect servers.
        # `discover_mcp_tools` is fully idempotent — same call as startup.
        mcp_summary = ""
        try:
            from cogitum.core.mcp import discover_mcp_tools, load_config
            from cogitum.core.mcp.sampling import build_sampling_callback
            mcp_cfg = load_config()
            cb = build_sampling_callback(new_mesh, current_model)
            result = discover_mcp_tools(REGISTRY, mcp_cfg, sampling_callback=cb)
            connected = sum(
                1 for s in result.get("servers", []) if s.get("state") == "connected"
            )
            added = len(result.get("registered", []))
            removed = len(result.get("unregistered", []))
            mcp_summary = (
                f"\nMCP: `{connected}` servers"
                + (f" · `+{added}`" if added else "")
                + (f" · `-{removed}`" if removed else "")
            )
            log.info(
                "MCP reconcile: %d servers connected, +%d tools, -%d tools",
                connected, added, removed,
            )
        except Exception as e:
            log.warning("MCP reconcile failed: %s", e)

        if not silent and chat_id is not None:
            n_models = len(new_mesh.list_resolved())
            n_providers = len(new_mesh.providers)
            await self.api.send_message(
                chat_id,
                f"⟳ *Reloaded*\n"
                f"Providers: `{n_providers}`\n"
                f"Models: `{n_models}`\n"
                f"Current: `{escape_md(current_model)}`"
                f"{mcp_summary}",
            )

    # ── Polling ──────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                updates = await self.api.get_updates(
                    offset=self._offset, timeout=30
                )
                # Reset backoff after any successful round-trip.
                self._poll_backoff = 1.0
                for update in updates:
                    self._offset = update["update_id"] + 1
                    # Bounded fan-out: spawn handler tasks but cap parallelism
                    # via a semaphore. Keep hard refs to prevent GC of the
                    # task before it completes (RUF006 fix).
                    task = asyncio.create_task(self._spawn_handler(update))
                    self._update_tasks.add(task)
                    task.add_done_callback(self._update_tasks.discard)
                # Persist offset after each batch so a restart won't replay.
                if updates:
                    self._save_offset()
            except httpx.TimeoutException:
                continue
            except asyncio.CancelledError:
                break
            except TelegramAuthError as e:
                # Token is revoked/wrong/shape-broken. No amount of
                # retrying will fix this — surface loudly and stop the
                # gateway so the operator notices instead of seeing the
                # bot 'run' silently.
                log.critical(
                    "Telegram bot token is invalid or revoked. "
                    "Run `cog setup` again. Stopping gateway. (%s)",
                    self.api._scrub_token(str(e)),
                )
                self._running = False
                break
            except TelegramRateLimitError as e:
                # TG already told us the wait time — honour it instead
                # of layering exponential backoff on top.
                log.warning(
                    "Telegram rate limit hit; sleeping %ds before retry",
                    e.retry_after,
                )
                await asyncio.sleep(e.retry_after)
                continue
            except Exception as e:
                # Scrub the bot URL/token from any httpx-style exception
                # message before logging. log.exception's exc_info path
                # is fine because the formatter renders frames, not the
                # request URL string.
                log.exception("Poll error: %s", self.api._scrub_token(str(e)))
                # Exponential backoff capped at 30s. Resets on next success.
                await asyncio.sleep(self._poll_backoff)
                self._poll_backoff = min(self._poll_backoff * 2, 30.0)

    def _chat_id_of(self, update: dict) -> int:
        """Best-effort chat id extraction for fairness gating.

        Falls back to 0 when neither a message nor a callback_query is
        present so the global cap still applies.
        """
        msg = update.get("message") or update.get("edited_message")
        if msg:
            return int(msg.get("chat", {}).get("id", 0) or 0)
        cb = update.get("callback_query")
        if cb:
            return int(cb.get("message", {}).get("chat", {}).get("id", 0) or 0)
        return 0

    async def _spawn_handler(self, update: dict) -> None:
        """Run _handle_update under per-chat + global concurrency caps.

        Acquire global first (≤32 in flight bot-wide), then per-chat
        (≤4 per chat). Per-chat semaphores are lazily created and
        reaped when no waiters remain so a million-chat deploy doesn't
        leak Semaphore objects.

        TelegramAuthError raised by call() (e.g. send_message after a
        mid-conversation token revoke) escalates to the same shutdown
        path as the polling loop: log critical once, drop _running,
        cancel the poll task. Without this the bot silently no-ops on
        every API call after a revoke.
        """
        chat_id = self._chat_id_of(update)
        sem = self._chat_sems.get(chat_id)
        if sem is None:
            sem = asyncio.Semaphore(self._per_chat_sem_size)
            self._chat_sems[chat_id] = sem
        self._chat_sem_users[chat_id] = self._chat_sem_users.get(chat_id, 0) + 1
        try:
            async with self._global_sem:
                async with sem:
                    try:
                        await self._handle_update(update)
                    except TelegramAuthError as e:
                        log.critical(
                            "Telegram bot token is invalid or revoked "
                            "(mid-conversation). Stopping gateway. (%s)",
                            self.api._scrub_token(str(e)),
                        )
                        self._running = False
                        if self._poll_task is not None and not self._poll_task.done():
                            self._poll_task.cancel()
                    except Exception:
                        log.exception("Update handler crashed")
                        # F76: best-effort reply so the user knows
                        # something blew up instead of staring at a
                        # silent bot that ate their command. Mute any
                        # send_message exception (e.g. chat-not-found
                        # if they kicked us mid-handler) — the log
                        # already has the original traceback.
                        try:
                            if chat_id:
                                await self.api.send_message(
                                    chat_id,
                                    escape_md(
                                        "✕ Internal error — check logs."
                                    ),
                                )
                        except Exception:
                            log.debug(
                                "F76 reply failed", exc_info=True
                            )
        finally:
            n = self._chat_sem_users.get(chat_id, 0) - 1
            if n <= 0:
                self._chat_sem_users.pop(chat_id, None)
                # Counter at zero means no current holders AND no
                # pending waiters: every waiter increments the counter
                # in the acquire path above before awaiting the
                # semaphore, so n == 0 strictly implies an empty
                # semaphore — no need to peek at the private
                # ``sem._waiters`` deque (Tier-4 hardening).
                self._chat_sems.pop(chat_id, None)
            else:
                self._chat_sem_users[chat_id] = n

    # ── Update dispatch ──────────────────────────────────────────────────────

    async def _handle_update(self, update: dict) -> None:
        # Handle callback queries (inline keyboard)
        if "callback_query" in update:
            await self._handle_callback(update["callback_query"])
            return

        msg = update.get("message")
        if not msg:
            return

        chat_id = msg["chat"]["id"]
        user_id = msg.get("from", {}).get("id", 0)

        # Auth check (private user OR allowed group chat).
        if not self.config.can_respond(user_id=user_id, chat_id=chat_id):
            # Stay silent in groups we're not allowed in — don't leak
            # bot existence. In private 1:1 with a non-allowed user we
            # also stay silent (TG already shows them the bot link, no
            # need to hand them an "access denied" hint).
            return

        # Get or create session
        session = self.sessions.setdefault(chat_id, ChatSession(chat_id))

        # Handle text
        text = msg.get("text", "").strip()
        if text.startswith("/"):
            await self._handle_command(text, session, msg)
            return

        # Handle media (photo/document)
        context_extra = ""
        if "photo" in msg:
            # Get highest resolution photo
            photo = msg["photo"][-1]
            local_path = await self.api.get_file(photo["file_id"])
            if local_path:
                context_extra = f"\n[User sent image: {local_path}]"
        elif "document" in msg:
            doc = msg["document"]
            local_path = await self.api.get_file(doc["file_id"])
            if local_path:
                context_extra = f"\n[User sent file: {local_path} ({doc.get('file_name', 'unknown')})]"

        if not text and not context_extra:
            return

        # Handle reply context
        reply_context = ""
        if "reply_to_message" in msg:
            reply_msg = msg["reply_to_message"]
            reply_text = reply_msg.get("text", "")[:200]
            if reply_text:
                reply_context = f"\n[Replying to: {reply_text}]"

        full_message = text + context_extra + reply_context

        # Inject platform context if this is the first message or platform changed
        if not session.history:
            full_message = f"[User is writing from Telegram]\n{full_message}"
        elif session._last_platform != "telegram":
            full_message = f"[User switched to Telegram]\n{full_message}"
        session._last_platform = "telegram"

        # Run agent
        await self._run_agent(session, full_message, msg.get("message_id"))

    # ── Commands ─────────────────────────────────────────────────────────────

    async def _handle_command(self, text: str, session: ChatSession, msg: dict) -> None:
        """Dispatch /command messages.

        Open: /tools /help.
        Operator-only (mutates shared Agent / store / mesh state):
          /new /title /stop /resume /yolo /godmode /model /models /reload /compact.
        Operator gate is _is_operator(from_user_id, chat_id).
        """
        chat_id = session.chat_id
        # Tier-3 ACL: capture acting user id once. Used by the operator
        # guard on every state-mutating command.
        from_user_id = msg.get("from", {}).get("id", 0)
        parts = text[1:].split(maxsplit=1)
        cmd = (parts[0] if parts else "").lower()
        rest = parts[1] if len(parts) > 1 else ""

        if cmd == "start":
            # Mark mutating commands as (operator) so a curious group
            # member sees up-front which entries will bounce — keep
            # this list synced with /help body and setMyCommands above.
            welcome = (
                "✦ *COGITUM* — sovereign agentic CLI\n\n"
                "Send me a message and I'll use my tools to help\\.\n\n"
                "Commands:\n"
                "/new — new session \\(operator\\)\n"
                "/resume — resume past session \\(operator\\)\n"
                "/title — rename session \\(operator\\)\n"
                "/tools — list tools\n"
                "/models — pick model \\(operator\\)\n"
                "/reload — reload providers/models \\(operator\\)\n"
                "/stop — cancel generation \\(operator\\)\n"
                "/help — all commands"
            )
            await self.api.send_message(chat_id, welcome)

        elif cmd == "new":
            # Tier-4 ACL: /new wipes session.history + session.session_id,
            # which a non-operator group member could use to nuke the
            # operator's running session-store-backed conversation.
            if not self._is_operator(from_user_id, chat_id):
                await self._deny_operator_only(chat_id=chat_id)
                return
            # P1 race fix: if the agent is mid-run, /new used to clear
            # session.history in place — but the running ``_run_agent``
            # task's ``finally`` block writes ``task.result()`` back to
            # ``session.history`` and to the session store, restoring
            # the just-cleared turn (and the just-cleared session_id
            # silently rebinds to the new agent's surviving id). End
            # result: /new looked successful, then the next message
            # brought the old conversation back from the dead. Refuse
            # politely instead — operator has /stop for force-cancel.
            if session.is_busy:
                await self.api.send_message(
                    chat_id,
                    escape_md("⏳ Wait, agent running. Use /stop first."),
                )
                return
            session.history = []
            session.session_id = None
            divider = format_session_divider("NEW SESSION")
            await self.api.send_message(chat_id, divider)

        elif cmd == "resume":
            # Tier-3 ACL: /resume reads/writes the global session store
            # — non-operator click would expose the operator's saved
            # private histories and let a group member overwrite them.
            if not self._is_operator(from_user_id, chat_id):
                await self._deny_operator_only(chat_id=chat_id)
                return
            # P1 race fix: same as /new — if the agent is mid-run, the
            # background task's writeback in ``_run_agent.finally`` will
            # overwrite the just-loaded session.history with the live
            # turn's messages, leaving the bot in a hybrid state. Refuse.
            if session.is_busy:
                await self.api.send_message(
                    chat_id,
                    escape_md("⏳ Wait, agent running. Use /stop first."),
                )
                return
            store = get_store()
            sessions = store.list_sessions(limit=20)
            if not sessions:
                await self.api.send_message(
                    chat_id, escape_md("No saved sessions."), parse_mode="MarkdownV2"
                )
                return

            # If user provided a name/pattern, filter by it
            if rest:
                import fnmatch
                pattern = rest.strip()
                # Support glob patterns (e.g. "Привет*", "*test*")
                if "*" in pattern or "?" in pattern:
                    matched = [s for s in sessions if fnmatch.fnmatch(
                        (s.title or "").lower(), pattern.lower()
                    )]
                else:
                    # Substring match
                    matched = [s for s in sessions if pattern.lower() in (s.title or "").lower()]

                if not matched:
                    await self.api.send_message(
                        chat_id, escape_md(f"No sessions matching: {pattern}"),
                    )
                    return
                elif len(matched) == 1:
                    # Direct resume
                    s = matched[0]
                    messages = store.load_session(s.id)
                    session.history = messages
                    session.session_id = s.id
                    title = s.title or s.id[:12]
                    divider = format_session_divider(f"RESUMED: {title}")
                    await self.api.send_message(chat_id, divider)
                    msg_count = len(messages)
                    await self.api.send_message(
                        chat_id,
                        escape_md(f"◆ Loaded {msg_count} messages. Continue the conversation."),
                    )
                    return
                else:
                    sessions = matched[:8]

            # Build inline keyboard
            buttons = []
            for s in sessions[:8]:
                title = s.title or s.id[:12]
                buttons.append([{
                    "text": f"◆ {title}",
                    "callback_data": f"resume:{s.id}",
                }])
            markup = {"inline_keyboard": buttons}
            await self.api.send_message(
                chat_id,
                escape_md("Pick a session to resume:"),
                reply_markup=markup,
            )

        elif cmd == "title":
            # Tier-4 ACL: /title writes get_store().set_title(session_id,...)
            # — that's a write into the SHARED sqlite session store. A
            # group member could rename the operator's saved private
            # session via the same chat's session_id (or any session_id
            # the bot exposes via the bot-level state). Block.
            if not self._is_operator(from_user_id, chat_id):
                await self._deny_operator_only(chat_id=chat_id)
                return
            if not rest:
                await self.api.send_message(
                    chat_id, escape_md("Usage: /title <name>"), parse_mode="MarkdownV2"
                )
                return
            if session.session_id:
                get_store().set_title(session.session_id, rest)
                await self.api.send_message(
                    chat_id, f"◈ Session title: *{escape_md(rest)}*"
                )
            else:
                await self.api.send_message(
                    chat_id, escape_md("No active session — send a message first.")
                )

        elif cmd == "tools":
            names = REGISTRY.names()
            tool_list = "\n".join(f"• `{n}`" for n in names)
            await self.api.send_message(
                chat_id,
                f"⚙ *{len(names)} tools:*\n{tool_list}",
            )

        elif cmd == "models":
            # Tier-3 ACL: /models triggers _reload_mesh which mutates
            # the shared mesh and may also rotate Agent.cfg.model. The
            # picker UI itself is harmless but the side-effect isn't.
            if not self._is_operator(from_user_id, chat_id):
                await self._deny_operator_only(chat_id=chat_id)
                return
            # Reload mesh from disk so newly-added providers/models show up
            await self._reload_mesh(silent=True)
            if not self.mesh:
                await self.api.send_message(chat_id, escape_md("Mesh not loaded."))
                return
            pairs = self.mesh.list_resolved()
            if not pairs:
                await self.api.send_message(
                    chat_id, escape_md("No models available. Run `cog setup` to add providers.")
                )
                return
            buttons = []
            for r in pairs[:12]:
                display = r.model.display or r.model.id
                buttons.append([{
                    "text": f"◇ {display}",
                    "callback_data": f"model:{r.qualified_id}",
                }])
            markup = {"inline_keyboard": buttons}
            current = self.agent.cfg.model if self.agent else "—"
            total = len(pairs)
            shown = min(12, total)
            header = f"Current: `{escape_md(current or '—')}`"
            if total > shown:
                header += f"\n_{escape_md(f'showing {shown} of {total}')}_"
            await self.api.send_message(
                chat_id,
                f"{header}\nPick a model:",
                reply_markup=markup,
            )

        elif cmd == "reload":
            # Tier-3 ACL: /reload mutates self.mesh, self.agent.mesh,
            # and self.agent.cfg.model on top of running provider
            # round-trips on the operator's dime. Lock to operator.
            if not self._is_operator(from_user_id, chat_id):
                await self._deny_operator_only(chat_id=chat_id)
                return
            # Manual mesh reload after editing providers.toml
            await self._reload_mesh(silent=False, chat_id=chat_id)

        elif cmd == "model":
            # Tier-3 ACL: switching model mutates shared Agent state.
            if not self._is_operator(from_user_id, chat_id):
                await self._deny_operator_only(chat_id=chat_id)
                return
            # F3 None-agent guard: on a fresh install with no providers,
            # self.agent is None — touching .cfg.model would raise
            # AttributeError. Mirror /compact's pattern.
            if self.agent is None or self.agent.cfg is None:
                await self.api.send_message(
                    chat_id, escape_md("No active model. Run /setup first.")
                )
                return
            if not rest:
                current = self.agent.cfg.model if self.agent else "—"
                await self.api.send_message(
                    chat_id, f"Current model: `{escape_md(current or '—')}`"
                )
                return
            if self.mesh:
                candidates = self.mesh.resolve(rest)
                if candidates:
                    self.agent.cfg.model = candidates[0].qualified_id
                    await self.api.send_message(
                        chat_id,
                        f"◈ Model: `{escape_md(candidates[0].qualified_id)}`",
                    )
                else:
                    await self.api.send_message(
                        chat_id, escape_md(f"✕ No model matches: {rest}")
                    )

        elif cmd in ("godmode", "gm"):
            await self._cmd_godmode(rest, chat_id, from_user_id)

        elif cmd == "yolo":
            await self._cmd_yolo(rest, chat_id, from_user_id)

        elif cmd == "compact":
            await self._cmd_compact(session, chat_id, from_user_id)

        elif cmd == "stop":
            # Tier-4 ACL: /stop calls session.cancel() which kills the
            # operator's running agent turn. From a group chat that's
            # an escalation surface (DoS / lost work). Operator only.
            if not self._is_operator(from_user_id, chat_id):
                await self._deny_operator_only(chat_id=chat_id)
                return
            if session.is_busy:
                session.cancel()
                await self.api.send_message(chat_id, escape_md("⏹ Stopped."))
            else:
                await self.api.send_message(chat_id, escape_md("Nothing running."))

        elif cmd in ("help", "h"):
            help_text = (
                "✦ *Commands:*\n\n"
                "/new — start fresh session _\\(operator\\)_\n"
                "/resume — resume past session _\\(operator\\)_\n"
                "/title `<name>` — rename session _\\(operator\\)_\n"
                "/tools — list available tools\n"
                "/models — pick model \\(keyboard\\) _\\(operator\\)_\n"
                "/model `<id>` — switch model directly _\\(operator\\)_\n"
                "/reload — reload providers/models _\\(operator\\)_\n"
                "/godmode `[on|off|list|status|<preset>]` — jailbreak prompt _\\(operator\\)_\n"
                "/yolo `[on|off|status]` — auto\\-approve all tools \\(autonomous\\) _\\(operator\\)_\n"
                "/compact — compact context now _\\(operator\\)_\n"
                "/stop — cancel current generation _\\(operator\\)_\n"
                "/help — this message"
            )
            await self.api.send_message(chat_id, help_text)

        else:
            await self.api.send_message(
                chat_id, escape_md(f"Unknown command: /{cmd}. Try /help")
            )

    # ── Per-command handlers (extracted from _handle_command) ────────────────

    async def _cmd_godmode(
        self, rest: str, chat_id: int, from_user_id: int,
    ) -> None:
        """Handle ``/godmode [on|off|list|status|<preset>]`` (TG).

        Extracted from ``_handle_command``. Tier-3 ACL — swaps
        ``Agent.cfg.system`` globally, so non-operators are bounced
        with the standard operator-only deny message. F1 None-agent
        guard mirrors the inline block.
        """
        # Tier-3 ACL: /godmode swaps Agent.cfg.system globally.
        if not self._is_operator(from_user_id, chat_id):
            await self._deny_operator_only(chat_id=chat_id)
            return
        # F1 None-agent guard.
        if self.agent is None or self.agent.cfg is None:
            await self.api.send_message(
                chat_id, escape_md("No active model. Run /setup first.")
            )
            return
        from cogitum.core.godmode import (
            get_preset, list_presets, auto_pick_preset,
        )
        current_model = (self.agent.cfg.model if self.agent else "") or ""
        sub = rest.strip().lower()

        if not sub or sub in ("on", "auto"):
            preset_name = auto_pick_preset(current_model)
            preset = get_preset(preset_name)
            if self._pre_godmode_system is None:
                self._pre_godmode_system = self.agent.cfg.system
            self.agent.cfg.system = wrap_system_prompt(preset)
            await self.api.send_message(
                chat_id,
                f"◈ *godmode:* `{escape_md(preset_name)}` "
                + escape_md(f"— enabled (auto-picked for {current_model or 'unknown model'})"),
            )

        elif sub == "off":
            if self._pre_godmode_system is not None:
                self.agent.cfg.system = self._pre_godmode_system
                self._pre_godmode_system = None
                await self.api.send_message(
                    chat_id, escape_md("godmode: disabled — normal mode restored")
                )
            else:
                await self.api.send_message(chat_id, escape_md("godmode: already off"))

        elif sub == "list":
            names = ", ".join(list_presets())
            auto_name = auto_pick_preset(current_model)
            await self.api.send_message(
                chat_id,
                f"*godmode presets:* {escape_md(names)}\n"
                f"_auto for_ `{escape_md(current_model or '(no model)')}`: `{escape_md(auto_name)}`",
            )

        elif sub == "status":
            state = "ON" if self._pre_godmode_system is not None else "OFF"
            await self.api.send_message(chat_id, escape_md(f"godmode: {state}"))

        else:
            preset = get_preset(rest.strip())
            if preset:
                if self._pre_godmode_system is None:
                    self._pre_godmode_system = self.agent.cfg.system
                self.agent.cfg.system = wrap_system_prompt(preset)
                await self.api.send_message(
                    chat_id, f"◈ *godmode:* `{escape_md(rest.strip())}` " + escape_md("— enabled")
                )
            else:
                await self.api.send_message(
                    chat_id,
                    escape_md(f"unknown preset: {rest} (try /godmode list)"),
                )

    async def _cmd_yolo(
        self, rest: str, chat_id: int, from_user_id: int,
    ) -> None:
        """Handle ``/yolo [on|off|toggle|status] [<ttl_minutes>]`` (TG).

        Extracted from ``_handle_command``. Tier-3 ACL — flips
        ``Agent.cfg.yolo_mode`` globally, so non-operators are
        bounced. F38 TTL parsing accepts ``/yolo on <minutes>``.
        Uses ``time.monotonic`` so an NTP step-back / DST edit
        cannot extend the privileged window.
        """
        # Tier-3 ACL: /yolo flips Agent.cfg.yolo_mode globally,
        # disarming approval prompts in every chat.
        if not self._is_operator(from_user_id, chat_id):
            await self._deny_operator_only(chat_id=chat_id)
            return
        # F2 None-agent guard.
        if self.agent is None or self.agent.cfg is None:
            await self.api.send_message(
                chat_id, escape_md("No active model. Run /setup first.")
            )
            return
        # F38 TTL: optional second arg `/yolo on <ttl_minutes>`.
        # Parse rest as either "[on|off|toggle|status]" or
        # "on <minutes>" so the user can opt into a time-boxed yolo.
        raw = (rest or "").strip()
        tokens = raw.split()
        sub = tokens[0].lower() if tokens else ""
        ttl_minutes: float | None = None
        if sub == "on" and len(tokens) > 1:
            try:
                ttl_minutes = float(tokens[1])
                if ttl_minutes <= 0:
                    ttl_minutes = None
            except ValueError:
                await self.api.send_message(
                    chat_id,
                    escape_md(
                        f"usage: /yolo on <minutes>  (got '{tokens[1]}')"
                    ),
                )
                return
        if sub in ("", "on", "toggle"):
            if sub == "on":
                self.agent.cfg.yolo_mode = True
            else:
                self.agent.cfg.yolo_mode = not self.agent.cfg.yolo_mode
            if self.agent.cfg.yolo_mode:
                if ttl_minutes is not None:
                    # F38: monotonic clock — wall-clock step-back
                    # (NTP, DST, manual edit) cannot extend a
                    # privileged window. yolo_until is in-memory
                    # only; restart resets it.
                    self.agent.cfg.yolo_until = (
                        time.monotonic() + ttl_minutes * 60.0
                    )
                    await self.api.send_message(
                        chat_id,
                        escape_md(
                            f"◈ yolo: ENABLED for {ttl_minutes:g} min "
                            "— agent runs autonomously. "
                            "No approval prompts. Use /stop to abort."
                        ),
                    )
                else:
                    # Toggle/on with no TTL clears any prior expiry.
                    self.agent.cfg.yolo_until = None
                    await self.api.send_message(
                        chat_id,
                        escape_md(
                            "◈ yolo: ENABLED — agent runs autonomously. "
                            "No approval prompts. Use /stop to abort."
                        ),
                    )
            else:
                self.agent.cfg.yolo_until = None
                await self.api.send_message(
                    chat_id,
                    escape_md("yolo: disabled — approval prompts restored"),
                )
        elif sub == "off":
            self.agent.cfg.yolo_mode = False
            self.agent.cfg.yolo_until = None
            await self.api.send_message(
                chat_id,
                escape_md("yolo: disabled — approval prompts restored"),
            )
        elif sub == "status":
            state = "ON" if self.agent.cfg.yolo_mode else "OFF"
            until = getattr(self.agent.cfg, "yolo_until", None)
            if state == "ON" and until:
                remain = max(0.0, until - time.monotonic())
                await self.api.send_message(
                    chat_id,
                    escape_md(
                        f"yolo: {state} (auto-off in {remain/60:.1f} min)"
                    ),
                )
            else:
                await self.api.send_message(
                    chat_id, escape_md(f"yolo: {state}")
                )
        else:
            await self.api.send_message(
                chat_id,
                escape_md(
                    "usage: /yolo [on|off|toggle|status] [<ttl_minutes>]"
                ),
            )

    async def _cmd_compact(
        self, session: ChatSession, chat_id: int, from_user_id: int,
    ) -> None:
        """Handle ``/compact`` — manual context compaction (TG).

        Extracted from ``_handle_command``. Tier-3 ACL — runs an
        LLM turn on the shared Agent (paid by operator's keys) and
        rewrites session-store messages, so non-operators are
        bounced. Reads ``AgentCompacted.status`` from the queue so
        the user-facing message reflects the actual outcome
        (``ok`` / ``not_needed`` / ``no_change``).
        """
        # Tier-3 ACL: /compact runs an LLM turn on the shared
        # Agent (paid by operator's keys) and rewrites session
        # store messages. Group spam = cost-amplified DoS.
        if not self._is_operator(from_user_id, chat_id):
            await self._deny_operator_only(chat_id=chat_id)
            return
        # Manual context compaction.
        if not session.history:
            await self.api.send_message(
                chat_id, escape_md("nothing to compact — history is empty")
            )
            return
        if session.is_busy:
            await self.api.send_message(
                chat_id,
                escape_md("agent is busy — wait for the current turn to finish"),
            )
            return
        if self.agent is None:
            await self.api.send_message(chat_id, escape_md("no agent — /models first"))
            return

        try:
            # Use a queue so we can read back AgentCompacted.status
            # and pick the right user-facing message instead of
            # always saying "compacted X → Y" when the buffer
            # didn't actually change.
            event_q: asyncio.Queue = asyncio.Queue()
            new_msgs, before, after = await self.agent.compact_now(
                session.history, queue=event_q,
            )
            session.history = new_msgs
            # Persist the compacted history so /resume picks up the
            # smaller form rather than the bloated original.
            if session.session_id:
                # ``get_store`` is already module-level imported.
                # An earlier inner re-import made Python flag every
                # reference inside _handle_command as local, raising
                # UnboundLocalError in /title's branch which happens
                # to run before the import line was reached.
                store = get_store()
                # Wipe and rewrite — append-only would leave the
                # old log in place. The session file is the live
                # state, not an audit trail.
                store.replace_messages(session.session_id, new_msgs)
            msgs_delta = len(new_msgs)
            # Pull the AgentCompacted event for the status. There
            # should be exactly one in the queue.
            status = "ok"
            try:
                ev = event_q.get_nowait()
                status = getattr(ev, "status", "ok")
            except asyncio.QueueEmpty:
                pass

            if status == "not_needed":
                msg_text = (
                    f"⟳ history is small — nothing to compact "
                    f"({msgs_delta} messages, ~{before} tokens)"
                )
            elif status == "no_change":
                msg_text = (
                    f"⟳ compaction ran but didn't reduce size "
                    f"(~{before} tokens unchanged); the summarizer "
                    f"may have errored — try /compact again or check "
                    f"gateway.log"
                )
            else:
                msg_text = (
                    f"⟳ context compacted: ~{before} → ~{after} tokens, "
                    f"{msgs_delta} messages now"
                )
            await self.api.send_message(chat_id, escape_md(msg_text))
        except Exception as exc:
            log.warning("Manual compact failed", exc_info=True)
            await self.api.send_message(
                chat_id, escape_md(f"compact failed: {exc}")
            )

    # ── Callback queries (inline keyboards) ──────────────────────────────────

    async def _handle_callback(self, callback: dict) -> None:
        """Dispatch callback_query payloads (inline keyboard clicks).

        Flow: dedup → can_respond auth → ACL split:
          - resume:<id>  operator-only (mutates global session store).
          - model:<id>   operator-only (mutates Agent.cfg.model).
          - approve:/reject:<token>  operator-only (gates tool exec).
        Reject path uses OPERATOR_ONLY_MSG via _deny_operator_only.
        """
        cb_id = callback["id"]
        # Dedup: Telegram retries unanswered callbacks for ~15s. Without this
        # check, a slow handler (or one that crashes mid-flight) causes the
        # same approval click to be processed multiple times — a real
        # rate-limit / replay-attack vector.
        if self._is_duplicate_callback(cb_id):
            log.debug("Dropping duplicate callback_query %s", cb_id)
            try:
                await self.api.answer_callback(cb_id)
            except TelegramAuthError:
                # Auth errors must propagate to _spawn_handler →
                # gateway shutdown. Do NOT swallow.
                raise
            except Exception:
                log.debug("swallowed exception", exc_info=True)
            return
        data = callback.get("data", "")
        chat_id = callback["message"]["chat"]["id"]
        user_id = callback.get("from", {}).get("id", 0)

        if not self.config.can_respond(user_id=user_id, chat_id=chat_id):
            await self.api.answer_callback(cb_id, "✕ Access denied")
            return

        session = self.sessions.setdefault(chat_id, ChatSession(chat_id))

        if data.startswith("resume:"):
            # Tier-3 ACL: a non-operator click would let a group member
            # load the operator's saved private session into the group
            # chat, then mutate it via /run.
            if not self._is_operator(user_id, chat_id):
                await self._deny_operator_only(callback_id=cb_id)
                return
            # P1 race fix (mirror of /new and /resume command guards):
            # clicking a resume button while the agent is mid-run lets
            # the running task's finally-block overwrite the freshly
            # loaded history. Refuse with a callback toast.
            if session.is_busy:
                await self.api.answer_callback(
                    cb_id, "⏳ Wait, agent running. /stop first."
                )
                return
            session_id = data[7:]
            store = get_store()
            messages = store.load_session(session_id)
            meta = store.get_meta(session_id)
            session.history = messages
            session.session_id = session_id
            title = meta.title if meta else session_id[:12]
            divider = format_session_divider(f"RESUMED: {title}")
            await self.api.send_message(chat_id, divider)
            # Show brief summary
            msg_count = len(messages)
            await self.api.send_message(
                chat_id,
                escape_md(f"◆ Loaded {msg_count} messages. Continue the conversation."),
            )
            await self.api.answer_callback(cb_id, f"Resumed: {title}")

        elif data.startswith("model:"):
            # Tier-3 ACL: model swap mutates shared Agent state — block
            # non-operators from clicking a model button delivered to
            # an allowed group chat.
            if not self._is_operator(user_id, chat_id):
                await self._deny_operator_only(callback_id=cb_id)
                return
            model_id = data[6:]
            if self.agent:
                self.agent.cfg.model = model_id
            await self.api.answer_callback(cb_id, f"Model: {model_id}")
            await self.api.send_message(
                chat_id, f"◈ Model: `{escape_md(model_id)}`"
            )

        elif data.startswith("approve:") or data.startswith("reject:"):
            # Tool approval response.
            #
            # callback_data carries a SHORT TOKEN (8 hex chars), not the
            # raw call_id, so we stay under Telegram's 64-byte cap even
            # for long MCP / composite ids. Resolve the token via the
            # bot-level lookup map populated when the buttons were sent.
            # Decisions are paired with the right pending tool call by
            # call_id via Agent.submit_approval — order in the UI no
            # longer matters.
            #
            # Tier-3 ACL: even though tokens are scoped per pending call,
            # a non-operator group member shouldn't be able to approve
            # (or reject) a tool the operator's private agent is running.
            # The Agent is shared across chats — so a click in a group
            # would resolve a private-chat call_id.
            if not self._is_operator(user_id, chat_id):
                await self._deny_operator_only(callback_id=cb_id)
                return
            _, _, token = data.partition(":")
            call_id = self._approval_token_to_call_id.pop(token, None)
            # F27: token map mutated — persist immediately so a crash
            # right after the click doesn't leave the disk copy
            # claiming this approval is still pending.
            if call_id is not None:
                await self._save_approval_tokens()
            action = "approve" if data.startswith("approve:") else "reject"
            # The agent lives on the bot, not on per-chat sessions —
            # session.agent does not exist (ChatSession only carries
            # chat-local state). Route via self.agent.
            agent = self.agent
            routed = False
            if call_id is not None and agent is not None:
                routed = agent.submit_approval(call_id, action)
            if routed:
                glyph = "◈" if action == "approve" else "✕"
                await self.api.answer_callback(cb_id, f"{glyph} {'Sanctioned' if action == 'approve' else 'Forbidden'}")
                # Edit the approval message to show decision
                msg_id = callback["message"]["message_id"]
                decision_text = f"{'◈ Sanctioned' if action == 'approve' else '✕ Forbidden'}"
                await self.api.edit_message(chat_id, msg_id, escape_md(decision_text))
            else:
                # F27: tell apart two stale-button cases.
                #   (a) call_id WAS in the persisted map (we just popped
                #       it) but the agent doesn't have a live future —
                #       this is the "bot restarted, run is gone" path.
                #       Edit the message so the user knows clicking
                #       again won't help.
                #   (b) call_id was already missing — pre-existing
                #       "▲ No pending approval" toast (run ended,
                #       /yolo auto-approved, etc.).
                if call_id is not None:
                    msg_id = callback["message"]["message_id"]
                    try:
                        await self.api.edit_message(
                            chat_id,
                            msg_id,
                            escape_md("[stale — bot restarted, ignore]"),
                        )
                    except Exception:
                        log.debug(
                            "F27 stale edit failed", exc_info=True
                        )
                    await self.api.answer_callback(
                        cb_id, "▲ Stale — bot restarted"
                    )
                else:
                    await self.api.answer_callback(
                        cb_id, "▲ No pending approval"
                    )

        else:
            await self.api.answer_callback(cb_id)

    # ── Agent execution ──────────────────────────────────────────────────────

    async def _run_agent(
        self, session: ChatSession, user_message: str, reply_to: int | None = None
    ) -> None:
        if not self.agent:
            await self.api.send_message(
                session.chat_id, escape_md("✕ Agent not initialized.")
            )
            return

        if session.is_busy:
            await self.api.send_message(
                session.chat_id, escape_md("… Still working... /stop to cancel.")
            )
            return

        chat_id = session.chat_id
        queue: asyncio.Queue = asyncio.Queue()
        approval_q: asyncio.Queue = asyncio.Queue()
        session._approval_queue = approval_q

        # Send typing indicator immediately
        await self.api.send_typing(chat_id)

        # Ensure session exists in store
        if not session.session_id:
            from cogitum.core.events import _id
            store = get_store()
            meta = store.create_session(
                session_id=_id(), model=self.agent.cfg.model or ""
            )
            session.session_id = meta.id

        # P1 race guard: snapshot the session id we're running for at
        # the moment the run starts. If the operator manages to change
        # it mid-flight (despite the /new and /resume busy-checks —
        # e.g. via a future codepath, a manual store mutation, or a
        # crash that bypasses the lock), the post-run writeback below
        # will detect the mismatch and skip persisting, so the new
        # session's data isn't trampled by this run's result.
        session_id_at_start = session.session_id

        _last_typing = time.time()

        async def keep_typing() -> None:
            """Background task to keep 'typing...' indicator alive."""
            nonlocal _last_typing
            while True:
                await asyncio.sleep(4)
                now = time.time()
                if now - _last_typing > 4:
                    await self.api.send_typing(chat_id)
                    _last_typing = now

        # Run agent
        session._cancel_flag = False

        # Inject TG context for send_media tool. Capture the tokens
        # returned by ``_set_tg_context`` so the matching ``finally``
        # below can ``var.reset(token)`` them — preserving any outer
        # binding instead of leaking ``None`` between concurrent agent
        # runs in the same Context. See builtin_tools._set_tg_context.
        from cogitum.core.builtin_tools import _set_tg_context, _clear_tg_context
        _tg_ctx_tokens = _set_tg_context(self.api, chat_id)

        async def agent_task():
            return await self.agent.run(
                user_message=user_message,
                history=session.history,
                queue=queue,
                approval_queue=approval_q,
            )

        task = asyncio.create_task(agent_task())
        typing_task = asyncio.create_task(keep_typing())
        session.agent_task = task

        # Collect results
        # Streaming surface: handles debounce, dedup, edit retries, splits.
        stream = TgStream(self.api, chat_id)

        try:
            await self._drain_event_queue(
                queue=queue,
                task=task,
                stream=stream,
                session=session,
                chat_id=chat_id,
            )

            # Commit any debounced edits still in flight before we move on.
            await stream.flush()

            # Update history
            if task.done() and not task.cancelled() and not task.exception():
                session.history = task.result()
                # Persist to disk. Append-only used to mean: when the
                # agent's auto-compaction shrinks ``session.history``
                # in-place, we would still APPEND the (shorter) new
                # history on top of the old long form already on disk.
                # ``/resume`` then loaded a duplicated, contradictory
                # log. Switching to atomic replace_messages keeps the
                # session file as the live state of the conversation,
                # which is what every read path already assumes.
                store = get_store()
                store.replace_messages(session.session_id, session.history)

        except asyncio.CancelledError:
            await self.api.send_message(chat_id, escape_md("⏹ Cancelled."))
        except TelegramAuthError:
            # Token revoke mid-conversation — escalate to the same
            # shutdown path as the polling loop. Re-raise so the
            # _spawn_handler wrapper logs CRITICAL once and stops the
            # bot instead of silently no-oping every send_message.
            raise
        except Exception as e:
            log.exception("Agent run error")
            await self.api.send_message(
                chat_id, f"✕ {escape_md(str(e))}"
            )
        finally:
            typing_task.cancel()
            _clear_tg_context(_tg_ctx_tokens)
            session.agent_task = None
            session._approval_queue = None

    async def _drain_event_queue(
        self,
        *,
        queue: "asyncio.Queue",
        task: "asyncio.Task",
        stream: "TgStream",
        session: ChatSession,
        chat_id: int,
    ) -> None:
        """Drain agent events into Telegram message rails.

        Lifted from the inline drain inside ``_run_agent``. Returns
        when ``AgentDone`` / ``AgentError`` arrives, or when the
        idle-watchdog window elapses with the agent task already
        finished (the queue drained out of order or the agent
        crashed silently).

        Branches mirror the original block one-for-one:

          * ``AgentThinking`` / ``AgentText`` stream into the
            spoiler / response rails.
          * ``AgentToolCall`` (non-preliminary) and
            ``AgentToolResult`` push status-rail lines and
            auto-attach screenshot output when the tool announces
            it.
          * ``AgentApprovalRequest`` is auto-approved when YOLO is
            on; otherwise it injects an approval prompt with
            short-token ``callback_data`` (so the 64-byte cap
            holds even for long MCP call_ids) and persists the
            token map immediately.
          * ``AgentTurnPersist`` atomic-rewrites the session file.
          * ``AgentRetry`` is silent. ``AgentRetryConfirm`` posts
            a permanent / retryable error message — there's no
            modal UI in TG.
          * ``AgentCompacted`` posts a status-specific notice so
            the user sees auto-compaction rather than a silent
            "forgot earlier turns".
          * ``AgentDone`` / ``AgentError`` exit the drain.

        Why the idle watchdog instead of a fixed 180s timeout: the
        old ``wait_for(queue.get(), timeout=180)`` would break out
        of the drain the moment the model sat thinking for >3 min
        (long reasoning, slow tool, big terminal output) and the
        agent ``task`` was still running — its ``task.done()`` was
        False, so the success branch in the caller didn't fire and
        the conversation history was silently discarded for that
        turn. Now we only consider the run truly stalled when (a)
        the queue has been quiet for the idle window AND (b) the
        underlying agent task itself is done.
        """
        # Local state for the drain loop only — caller doesn't need
        # them after the drain returns.
        thinking_buf = ""
        text_buf = ""
        tool_results_shown = 0
        # Drain events until done.
        #   thinking → spoiler rail (live edits)
        #   tool calls → status rail (rolling tail, live edits)
        #   text → response rail (live stream, auto-split at 3800 chars)
        _IDLE_WINDOW_S = 300
        while True:
            try:
                event = await asyncio.wait_for(
                    queue.get(), timeout=_IDLE_WINDOW_S
                )
            except asyncio.TimeoutError:
                if task.done():
                    # Agent finished but we never saw AgentDone /
                    # AgentError on the queue (drained out of
                    # order, or crashed silently). Exit the loop
                    # so the persistence branch below can run.
                    break
                # Agent is still working. Send a typing pulse so
                # the chat doesn't look frozen and keep waiting.
                try:
                    await self.api.send_typing(chat_id)
                except Exception:
                    log.debug("typing pulse failed", exc_info=True)
                continue

            if isinstance(event, AgentThinking):
                thinking_buf += event.delta
                if self.config.show_thinking:
                    await stream.update_thinking(
                        thinking_buf, formatter=format_thinking
                    )

            elif isinstance(event, AgentText):
                text_buf += event.delta
                # Live-stream the response body so users see typing.
                await stream.update_response(
                    text_buf, formatter=markdown_to_tg
                )

            elif isinstance(event, AgentToolCall):
                if not event.preliminary and self.config.show_tool_calls:
                    line = format_tool_call(event.tool_name, event.arguments)
                    await stream.push_status(line)

            elif isinstance(event, AgentToolResult):
                if self.config.show_tool_calls:
                    line = format_tool_result(
                        event.tool_name, event.result, event.error
                    )
                    await stream.push_status(line)
                tool_results_shown += 1
                # Auto-attach screenshot output.
                if "screenshot saved to" in event.result.lower():
                    import re as _re
                    path_match = _re.search(r"(/\S+\.png)", event.result)
                    if path_match and Path(path_match.group(1)).exists():
                        await stream.attach_photo(path_match.group(1))

            elif isinstance(event, AgentApprovalRequest):
                # Defense-in-depth: short-circuit if yolo turned
                # on between the agent emitting this request and
                # us draining it. The agent's _execute_tool gate
                # already covers the steady state; this protects
                # against the race where /yolo on lands during
                # a turn that already queued an approval. Route
                # via submit_approval (per-call_id future) — NOT
                # session._approval_queue, which the agent no
                # longer reads from after the FIFO→futures fix.
                if (
                    self.agent
                    and self.agent.cfg.yolo_mode
                ):
                    self.agent.submit_approval(event.call_id, "approve")
                    continue
                # Force pending edits before injecting an approval prompt
                # so the buttons land below the latest status, not above.
                await stream.flush()
                danger_rune = "▲" if event.danger_level == "danger" else "◈"
                from cogitum.gateway.tg_formatter import escape_md
                from cogitum.core.builtin_tools import _tool_subtitle_for_approval

                # Strip Cf-category Unicode (ZWSP, ZWJ, RTL/LRM marks,
                # BOM) from the rendered description so the operator
                # can't be tricked by an invisible right-to-left
                # override that visually swaps "rm -rf /" → "/ fr- mr".
                # The bytes the agent will *actually* execute are
                # already screened upstream; this just keeps the UI
                # honest for the human reviewing the approval.
                import unicodedata as _ud
                desc_raw = _tool_subtitle_for_approval(event.tool_name, event.arguments)
                desc = "".join(
                    ch for ch in desc_raw
                    if _ud.category(ch) != "Cf"
                )
                approval_text = (
                    f"{danger_rune} *Sanction required* \\({escape_md(event.danger_level)}\\)\n\n"
                    f"`{escape_md(event.tool_name)}`\n"
                    f"{escape_md(desc)}"
                )
                # Round-trip through a short token so callback_data
                # stays within Telegram's 64-byte cap regardless of
                # how long the underlying call_id is. 8 hex chars =
                # 4G collision space, more than enough for a single
                # approval window (one user, one pending call).
                import secrets as _secrets
                token = _secrets.token_hex(4)
                # If by some miracle we collide, regenerate. Bounded
                # loop so we never infinite-spin.
                for _ in range(8):
                    if token not in self._approval_token_to_call_id:
                        break
                    token = _secrets.token_hex(4)
                self._approval_token_to_call_id[token] = event.call_id
                # Bound the map: operator can't have hundreds of
                # pending tools at once. Evict oldest entries when
                # we exceed the cap.
                while (
                    len(self._approval_token_to_call_id)
                    > self._approval_token_max
                ):
                    self._approval_token_to_call_id.popitem(last=False)
                # F27: persist after each insert so a crash before
                # the operator clicks doesn't make the buttons
                # zombie. Save is best-effort (atomic + swallow).
                await self._save_approval_tokens()
                markup = {"inline_keyboard": [[
                    {"text": "◈ Sanction", "callback_data": f"approve:{token}"},
                    {"text": "✕ Forbid", "callback_data": f"reject:{token}"},
                ]]}
                await self.api.send_message(chat_id, approval_text, reply_markup=markup)

            elif isinstance(event, AgentTurnPersist):
                # Mid-run persistence checkpoint. Agent finished
                # an atomic state change (assistant commit, tool
                # results landed, fallback summary). Snapshot
                # the buffer and atomic-rewrite the session file
                # so a crash mid-loop only loses the in-flight
                # turn, never accumulated history.
                try:
                    session.history = list(event.messages)
                    if session.session_id:
                        from ..core.sessions import get_store
                        get_store().replace_messages(
                            session.session_id, session.history,
                        )
                except Exception:
                    log.debug(
                        "TG mid-run persist failed (will retry on AgentDone)",
                        exc_info=True,
                    )

            elif isinstance(event, AgentRetry):
                pass  # silent retry

            elif isinstance(event, AgentCompacted):
                # Surface auto-compaction visibly — without this,
                # the user sees the chat appear to "forget" the
                # earlier turns silently. Manual /compact has its
                # own status path; this branch fires for any
                # AgentCompacted event coming through the agent
                # loop (auto = threshold trigger; manual would
                # only land here in pathological flows).
                label = "manual" if event.manual else "auto"
                status = getattr(event, "status", "ok")
                if status == "not_needed":
                    msg_text = (
                        f"⟳ history is small — nothing to compact "
                        f"({event.messages_before} messages, "
                        f"~{event.before_tokens} tokens) ({label})"
                    )
                elif status == "no_change":
                    msg_text = (
                        f"⟳ compaction ran but didn't reduce size "
                        f"(~{event.before_tokens} tokens unchanged) "
                        f"({label})"
                    )
                else:
                    msg_text = (
                        f"⟳ context compacted ({label}): "
                        f"~{event.before_tokens} → ~{event.after_tokens} tokens, "
                        f"{event.messages_before} → {event.messages_after} messages"
                    )
                await self.api.send_message(chat_id, escape_md(msg_text))

            elif isinstance(event, AgentRetryConfirm):
                # No modal in Telegram — just send a status
                # message so the user knows we're stuck. Agent
                # keeps retrying on its own; user can /stop to
                # abort.
                permanent = event.error_class == "quota"
                if permanent:
                    await self.api.send_message(
                        chat_id,
                        "✕ *Quota exceeded*\n\n"
                        + escape_md(event.error_message)
                        + "\n\n"
                        + escape_md(
                            "Top up the API account or switch provider, "
                            "then try again. Send /stop to abort the "
                            "current request."
                        ),
                    )
                else:
                    await self.api.send_message(
                        chat_id,
                        f"▲ *Retry {event.attempt}/{event.max_attempts}* "
                        + escape_md(f"({event.error_class})")
                        + "\n\n"
                        + escape_md(event.error_message[:200])
                        + "\n\n"
                        + escape_md("Send /stop to abort."),
                    )

            elif isinstance(event, AgentDone):
                break

            elif isinstance(event, AgentError):
                await stream.flush()
                await self.api.send_message(
                    chat_id, f"✕ *Error:* {escape_md(event.message)}"
                )
                break


# ── Entry point ──────────────────────────────────────────────────────────────

async def run_bot(config: TelegramConfig | None = None) -> None:
    """Main entry point for the Telegram gateway."""
    cfg = config or load_tg_config()
    if not cfg.is_valid():
        log.error(
            "Telegram gateway not configured. Run `cog tg setup` to set bot token and user ID."
        )
        return

    bot = CogitumBot(cfg)

    # Handle signals for graceful shutdown. We hold the stop tasks in a
    # set so the asyncio event loop doesn't GC them before they finish
    # (RUF006). The set is local to run() and dies with the loop.
    shutdown_tasks: set[asyncio.Task] = set()

    def _request_stop() -> None:
        t = asyncio.create_task(bot.stop())
        shutdown_tasks.add(t)
        t.add_done_callback(shutdown_tasks.discard)

    loop = asyncio.get_running_loop()
    # Signal handling differs across platforms:
    #   POSIX: SIGINT + SIGTERM → loop.add_signal_handler works.
    #   Windows: loop.add_signal_handler raises NotImplementedError.
    #            asyncio.ProactorEventLoop on Windows handles Ctrl+C
    #            naturally as KeyboardInterrupt; SIGTERM does not exist
    #            on Windows. We register what we can and skip the rest.
    if sys.platform == "win32":
        # On Windows we rely on KeyboardInterrupt propagation. The
        # service stop in `cogitum-tg.service` is Linux-only anyway;
        # Windows users who want a daemon path use NSSM / Task
        # Scheduler which sends terminate signals out of band.
        # F11: signal.signal handlers fire on the main thread but
        # OUTSIDE the asyncio loop's invariants. Calling
        # asyncio.create_task directly from there can raise
        # ``RuntimeError: no running event loop`` (or schedule the
        # task on the wrong loop) and leave bot.stop() never running.
        # Use loop.call_soon_threadsafe so the actual create_task
        # happens on the loop thread.
        try:
            import signal as _sig

            def _win_sigint(_s, _f) -> None:
                try:
                    loop.call_soon_threadsafe(
                        lambda: shutdown_tasks.add(
                            asyncio.create_task(bot.stop())
                        )
                    )
                except RuntimeError:
                    # Loop already gone — nothing to do, KeyboardInterrupt
                    # propagation will handle the rest.
                    pass

            _sig.signal(_sig.SIGINT, _win_sigint)
        except (ValueError, OSError):
            # Falls through silently — KeyboardInterrupt still works.
            pass
    else:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _request_stop)

    await bot.start()


def main() -> None:
    """CLI entry point."""
    # F7: when the Windows daemon path (cog tg start) launches us as
    # `python -m cogitum.gateway.telegram` directly, cli.main()'s
    # _windows_init() never runs, so the console codepage stays at 866
    # /1251 and any emoji/box-drawing char in a log line trips
    # UnicodeEncodeError. Re-run the same init here so direct module
    # invocation gets identical UTF-8 behaviour.
    try:
        from cogitum.cli import _windows_init
        _windows_init()
    except Exception:
        log.debug("swallowed exception", exc_info=True)

    # Load persisted secrets so providers can resolve env: refs
    try:
        from cogitum.core.llm.secrets_env import load_secrets_into_environ
        load_secrets_into_environ(override=False)
    except Exception:
        log.debug("swallowed exception", exc_info=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
