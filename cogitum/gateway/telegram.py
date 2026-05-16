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
import json
import logging
import os
import signal
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from cogitum.core.agent import (
    Agent,
    AgentApprovalRequest,
    AgentConfig,
    AgentDone,
    AgentError,
    AgentRetry,
    AgentText,
    AgentThinking,
    AgentToolCall,
    AgentToolResult,
)
from cogitum.core.builtin_tools import *  # noqa: F401,F403 — registers tools
from cogitum.core.events import Message, TextPart
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

log = logging.getLogger("cogitum.gateway.telegram")

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
        resp = await client.post(f"{self.base}/{method}", json=kwargs)
        data = resp.json()
        if not data.get("ok"):
            log.warning("TG API error: %s → %s", method, data.get("description"))
        return data

    async def get_updates(self, offset: int = 0, timeout: int = 30) -> list[dict]:
        data = await self.call("getUpdates", offset=offset, timeout=timeout)
        return data.get("result", [])

    async def send_message(
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
            # Fallback: try without parse_mode (formatting error)
            if parse_mode:
                log.warning("Markdown send failed, retrying plain: %s", data.get("description"))
                kwargs.pop("parse_mode", None)
                kwargs["text"] = text.replace("\\", "")  # strip escapes
                data = await self.call("sendMessage", **kwargs)
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
            kwargs.pop("parse_mode", None)
            kwargs["text"] = text.replace("\\", "")
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
        """Get file path from Telegram, download to /tmp, return local path."""
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
        local = tempfile.mktemp(suffix=ext, prefix="cogitum_tg_")
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
        self._running = False
        self._offset = 0

    async def start(self) -> None:
        """Initialize mesh and start polling."""
        log.info("Starting Cogitum Telegram gateway...")

        # Auto-discover models for every configured provider before
        # building the mesh, so /models picker has fresh data.
        log.info("Refreshing models from all providers...")
        try:
            refresh = await refresh_all_providers(timeout=6.0, only_empty=False)
            for pid, r in refresh.items():
                log.info("  %-20s %s — %s", pid, r["status"], r["message"])
        except Exception as e:  # noqa: BLE001
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

        self.agent = Agent(
            mesh=self.mesh,
            registry=REGISTRY,
            config=AgentConfig(model=current_model, platform="telegram"),
        )

        self._running = True
        log.info("Gateway ready. Model: %s. Polling...", current_model)

        # Register bot commands menu
        await self.api.set_my_commands([
            {"command": "new", "description": "✦ New session"},
            {"command": "resume", "description": "📋 Resume session (or /resume <name>)"},
            {"command": "title", "description": "✏️ Rename current session"},
            {"command": "tools", "description": "🔧 List available tools"},
            {"command": "models", "description": "🤖 Pick model"},
            {"command": "model", "description": "🔄 Switch model directly"},
            {"command": "reload", "description": "♻️ Reload providers/models from disk"},
            {"command": "stop", "description": "⏹ Cancel generation"},
            {"command": "help", "description": "❓ All commands"},
        ])

        # Notify user that gateway restarted (session reset)
        await self.api.send_message(
            self.config.allowed_user_id,
            "🔄 *Cogitum Gateway restarted*\n\n"
            f"Model: `{escape_md(current_model)}`\n"
            f"⚠️ {escape_md('Previous session was reset. Use /resume to continue a saved session.')}\n\n"
            f"{escape_md('Tools:')} `{escape_md(str(len(REGISTRY.names())))}`"
        )

        try:
            await self._poll_loop()
        finally:
            await self.api.close()
            if self.mesh:
                await self.mesh.aclose()

    async def stop(self) -> None:
        self._running = False
        for session in self.sessions.values():
            session.cancel()

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
        except Exception:  # noqa: BLE001
            pass

        # Auto-discover models for every provider first
        try:
            refresh = await refresh_all_providers(timeout=6.0, only_empty=False)
            log.info("reload refresh: %s", {k: v["message"] for k, v in refresh.items()})
        except Exception as e:  # noqa: BLE001
            log.warning("reload refresh failed: %s", e)

        try:
            new_mesh = load_mesh()
        except Exception as e:  # noqa: BLE001
            log.exception("mesh reload failed")
            if not silent and chat_id is not None:
                await self.api.send_message(
                    chat_id, escape_md(f"❌ reload failed: {e}")
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
            except Exception:  # noqa: BLE001
                pass

        if not silent and chat_id is not None:
            n_models = len(new_mesh.list_resolved())
            n_providers = len(new_mesh.providers)
            await self.api.send_message(
                chat_id,
                f"🔄 *Reloaded*\n"
                f"Providers: `{n_providers}`\n"
                f"Models: `{n_models}`\n"
                f"Current: `{escape_md(current_model)}`",
            )

    # ── Polling ──────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                updates = await self.api.get_updates(
                    offset=self._offset, timeout=30
                )
                for update in updates:
                    self._offset = update["update_id"] + 1
                    asyncio.create_task(self._handle_update(update))
            except httpx.TimeoutException:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("Poll error: %s", e)
                await asyncio.sleep(5)

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

        # Auth check
        if user_id != self.config.allowed_user_id:
            await self.api.send_message(
                chat_id, escape_md("⛔ Access denied."), parse_mode="MarkdownV2"
            )
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
        chat_id = session.chat_id
        parts = text[1:].split(maxsplit=1)
        cmd = (parts[0] if parts else "").lower()
        rest = parts[1] if len(parts) > 1 else ""

        if cmd == "start":
            welcome = (
                "✦ *COGITUM* — sovereign agentic CLI\n\n"
                "Send me a message and I'll use my tools to help\\.\n\n"
                "Commands:\n"
                "/new — new session\n"
                "/resume — resume past session\n"
                "/title — rename session\n"
                "/tools — list tools\n"
                "/models — pick model\n"
                "/reload — reload providers/models\n"
                "/stop — cancel generation\n"
                "/help — all commands"
            )
            await self.api.send_message(chat_id, welcome)

        elif cmd == "new":
            session.history = []
            session.session_id = None
            divider = format_session_divider("NEW SESSION")
            await self.api.send_message(chat_id, divider)

        elif cmd == "resume":
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
                        escape_md(f"📋 Loaded {msg_count} messages. Continue the conversation."),
                    )
                    return
                else:
                    sessions = matched[:8]

            # Build inline keyboard
            buttons = []
            for s in sessions[:8]:
                title = s.title or s.id[:12]
                buttons.append([{
                    "text": f"📋 {title}",
                    "callback_data": f"resume:{s.id}",
                }])
            markup = {"inline_keyboard": buttons}
            await self.api.send_message(
                chat_id,
                escape_md("Pick a session to resume:"),
                reply_markup=markup,
            )

        elif cmd == "title":
            if not rest:
                await self.api.send_message(
                    chat_id, escape_md("Usage: /title <name>"), parse_mode="MarkdownV2"
                )
                return
            if session.session_id:
                get_store().set_title(session.session_id, rest)
                await self.api.send_message(
                    chat_id, f"✅ Session title: *{escape_md(rest)}*"
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
                f"🔧 *{len(names)} tools:*\n{tool_list}",
            )

        elif cmd == "models":
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
                    "text": f"🤖 {display}",
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
            # Manual mesh reload after editing providers.toml
            await self._reload_mesh(silent=False, chat_id=chat_id)

        elif cmd == "model":
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
                        f"✅ Model: `{escape_md(candidates[0].qualified_id)}`",
                    )
                else:
                    await self.api.send_message(
                        chat_id, escape_md(f"❌ No model matches: {rest}")
                    )

        elif cmd == "stop":
            if session.is_busy:
                session.cancel()
                await self.api.send_message(chat_id, escape_md("⏹ Stopped."))
            else:
                await self.api.send_message(chat_id, escape_md("Nothing running."))

        elif cmd in ("help", "h"):
            help_text = (
                "✦ *Commands:*\n\n"
                "/new — start fresh session\n"
                "/resume — resume past session\n"
                "/title `<name>` — rename session\n"
                "/tools — list available tools\n"
                "/models — pick model \\(keyboard\\)\n"
                "/model `<id>` — switch model directly\n"
                "/reload — reload providers/models from disk\n"
                "/stop — cancel current generation\n"
                "/help — this message"
            )
            await self.api.send_message(chat_id, help_text)

        else:
            await self.api.send_message(
                chat_id, escape_md(f"Unknown command: /{cmd}. Try /help")
            )

    # ── Callback queries (inline keyboards) ──────────────────────────────────

    async def _handle_callback(self, callback: dict) -> None:
        cb_id = callback["id"]
        data = callback.get("data", "")
        chat_id = callback["message"]["chat"]["id"]
        user_id = callback.get("from", {}).get("id", 0)

        if user_id != self.config.allowed_user_id:
            await self.api.answer_callback(cb_id, "⛔ Access denied")
            return

        session = self.sessions.setdefault(chat_id, ChatSession(chat_id))

        if data.startswith("resume:"):
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
                escape_md(f"📋 Loaded {msg_count} messages. Continue the conversation."),
            )
            await self.api.answer_callback(cb_id, f"Resumed: {title}")

        elif data.startswith("model:"):
            model_id = data[6:]
            if self.agent:
                self.agent.cfg.model = model_id
            await self.api.answer_callback(cb_id, f"Model: {model_id}")
            await self.api.send_message(
                chat_id, f"✅ Model: `{escape_md(model_id)}`"
            )

        elif data.startswith("approve:") or data.startswith("reject:"):
            # Tool approval response
            action = "approve" if data.startswith("approve:") else "reject"
            call_id = data.split(":", 1)[1]
            session = self.sessions.get(chat_id)
            if session and hasattr(session, '_approval_queue') and session._approval_queue:
                await session._approval_queue.put((call_id, action))
                glyph = "◈" if action == "approve" else "✕"
                await self.api.answer_callback(cb_id, f"{glyph} {'Sanctioned' if action == 'approve' else 'Forbidden'}")
                # Edit the approval message to show decision
                msg_id = callback["message"]["message_id"]
                decision_text = f"{'◈ Sanctioned' if action == 'approve' else '✕ Forbidden'}"
                await self.api.edit_message(chat_id, msg_id, escape_md(decision_text))
            else:
                await self.api.answer_callback(cb_id, "⚠️ No pending approval")

        else:
            await self.api.answer_callback(cb_id)

    # ── Agent execution ──────────────────────────────────────────────────────

    async def _run_agent(
        self, session: ChatSession, user_message: str, reply_to: int | None = None
    ) -> None:
        if not self.agent:
            await self.api.send_message(
                session.chat_id, escape_md("❌ Agent not initialized.")
            )
            return

        if session.is_busy:
            await self.api.send_message(
                session.chat_id, escape_md("⏳ Still working... /stop to cancel.")
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

        # Inject TG context for send_media tool
        from cogitum.core.builtin_tools import _set_tg_context, _clear_tg_context
        _set_tg_context(self.api, chat_id)

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
        thinking_buf = ""
        text_buf = ""
        tool_results_shown = 0

        # Message IDs for editing
        thinking_msg_id: int | None = None
        status_msg_id: int | None = None
        status_lines: list[str] = []

        async def _send_or_edit_thinking(buf: str) -> None:
            """Send or update thinking spoiler message."""
            nonlocal thinking_msg_id
            if not buf.strip() or not self.config.show_thinking:
                return
            formatted = format_thinking(buf)
            if thinking_msg_id is None:
                resp = await self.api.send_message(chat_id, formatted)
                if resp.get("ok"):
                    thinking_msg_id = resp["result"]["message_id"]
            else:
                await self.api.edit_message(chat_id, thinking_msg_id, formatted)

        async def _send_or_edit_status(line: str) -> None:
            """Send or update tool status message."""
            nonlocal status_msg_id, status_lines, _last_typing
            status_lines.append(line)
            text = "\n".join(status_lines[-10:])
            if status_msg_id is None:
                resp = await self.api.send_message(chat_id, text, parse_mode="MarkdownV2")
                if resp.get("ok"):
                    status_msg_id = resp["result"]["message_id"]
            else:
                await self.api.edit_message(chat_id, status_msg_id, text, parse_mode="MarkdownV2")
            # Refresh typing
            now = time.time()
            if now - _last_typing > 4:
                await self.api.send_typing(chat_id)
                _last_typing = now

        try:
            # Drain events until done — mirror TUI order:
            # 1. Thinking (spoiler, updated live)
            # 2. Tool calls (status message, edited)
            # 3. Final response (separate message)
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=180)
                except asyncio.TimeoutError:
                    break

                if isinstance(event, AgentThinking):
                    thinking_buf += event.delta

                elif isinstance(event, AgentText):
                    text_buf += event.delta

                elif isinstance(event, AgentToolCall):
                    # Flush thinking before first tool call
                    if thinking_buf.strip() and thinking_msg_id is None:
                        await _send_or_edit_thinking(thinking_buf)
                    if not event.preliminary and self.config.show_tool_calls:
                        line = format_tool_call(event.tool_name, event.arguments)
                        await _send_or_edit_status(line)

                elif isinstance(event, AgentToolResult):
                    if self.config.show_tool_calls:
                        line = format_tool_result(event.tool_name, event.result, event.error)
                        await _send_or_edit_status(line)
                    tool_results_shown += 1
                    # Check if result contains a file path (screenshot, etc.)
                    if "screenshot saved to" in event.result.lower():
                        import re
                        path_match = re.search(r"(/\S+\.png)", event.result)
                        if path_match and Path(path_match.group(1)).exists():
                            await self.api.send_photo(chat_id, path_match.group(1))

                elif isinstance(event, AgentApprovalRequest):
                    # Show approval buttons to user (40K-styled glyphs).
                    danger_rune = "▲" if event.danger_level == "danger" else "◈"
                    from cogitum.gateway.tg_formatter import escape_md
                    from cogitum.core.builtin_tools import _tool_subtitle_for_approval

                    desc = _tool_subtitle_for_approval(event.tool_name, event.arguments)
                    approval_text = (
                        f"{danger_rune} *Sanction required* \\({escape_md(event.danger_level)}\\)\n\n"
                        f"`{escape_md(event.tool_name)}`\n"
                        f"{escape_md(desc)}"
                    )
                    markup = {"inline_keyboard": [[
                        {"text": "◈ Sanction", "callback_data": f"approve:{event.call_id}"},
                        {"text": "✕ Forbid", "callback_data": f"reject:{event.call_id}"},
                    ]]}
                    await self.api.send_message(chat_id, approval_text, reply_markup=markup)

                elif isinstance(event, AgentRetry):
                    pass  # silent retry

                elif isinstance(event, AgentDone):
                    break

                elif isinstance(event, AgentError):
                    await self.api.send_message(
                        chat_id, f"❌ *Error:* {escape_md(event.message)}"
                    )
                    break

            # If thinking was never sent (no tool calls happened), send it now
            if thinking_buf.strip() and thinking_msg_id is None and self.config.show_thinking:
                await _send_or_edit_thinking(thinking_buf)

            # Send main response (always last, like in TUI)
            if text_buf.strip():
                formatted = markdown_to_tg(text_buf.strip())
                chunks = split_message(formatted)
                for chunk in chunks:
                    await self.api.send_message(chat_id, chunk)

            # Update history
            if task.done() and not task.cancelled() and not task.exception():
                session.history = task.result()
                # Persist to disk
                store = get_store()
                store.append_messages(session.session_id, session.history)

        except asyncio.CancelledError:
            await self.api.send_message(chat_id, escape_md("⏹ Cancelled."))
        except Exception as e:
            log.exception("Agent run error")
            await self.api.send_message(
                chat_id, f"❌ {escape_md(str(e))}"
            )
        finally:
            typing_task.cancel()
            _clear_tg_context()
            session.agent_task = None
            session._approval_queue = None


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

    # Handle signals for graceful shutdown
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.stop()))

    await bot.start()


def main() -> None:
    """CLI entry point."""
    # Load persisted secrets so providers can resolve env: refs
    try:
        from cogitum.core.llm.secrets_env import load_secrets_into_environ
        load_secrets_into_environ(override=False)
    except Exception:
        pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
