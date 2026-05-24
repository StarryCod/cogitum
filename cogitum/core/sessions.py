"""
cogitum.core.sessions
~~~~~~~~~~~~~~~~~~~~~
Session persistence — JSONL append-only storage with index.

Each session = one conversation. Stored as:
  ~/.config/cogitum/sessions/{id}.jsonl   — one JSON line per message
  ~/.config/cogitum/sessions/index.json   — [{id, title, created_at, updated_at, model, count}]
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .events import (
    Message, ContentPart, TextPart, ThinkingPart, ImagePart,
    ToolCallPart, ToolResultPart,
)

from .platform_paths import get_data_dir

_SESSIONS_DIR = get_data_dir() / "sessions"


@dataclass
class SessionMeta:
    id: str
    title: str
    created_at: float
    updated_at: float
    model: str = ""
    count: int = 0


# ---------------------------------------------------------------------------
# Serialization: Message <-> JSON
# ---------------------------------------------------------------------------

def _part_to_dict(p: ContentPart) -> dict[str, Any]:
    if isinstance(p, TextPart):
        return {"kind": "text", "text": p.text}
    elif isinstance(p, ThinkingPart):
        d: dict[str, Any] = {"kind": "thinking", "text": p.text}
        if p.signature:
            d["signature"] = p.signature
        return d
    elif isinstance(p, ImagePart):
        d = {"kind": "image", "mime": p.mime}
        if p.url:
            d["url"] = p.url
        if p.data:
            d["data"] = p.data
        return d
    elif isinstance(p, ToolCallPart):
        return {"kind": "tool_call", "id": p.id, "name": p.name, "arguments": p.arguments}
    elif isinstance(p, ToolResultPart):
        return {"kind": "tool_result", "tool_call_id": p.tool_call_id,
                "content": p.content, "is_error": p.is_error}
    return {"kind": "unknown"}


def _dict_to_part(d: dict[str, Any]) -> ContentPart:
    kind = d.get("kind", "text")
    if kind == "text":
        return TextPart(text=d["text"])
    elif kind == "thinking":
        return ThinkingPart(text=d["text"], signature=d.get("signature"))
    elif kind == "image":
        return ImagePart(url=d.get("url"), data=d.get("data"), mime=d.get("mime", "image/png"))
    elif kind == "tool_call":
        return ToolCallPart(id=d["id"], name=d["name"], arguments=d.get("arguments", {}))
    elif kind == "tool_result":
        return ToolResultPart(
            tool_call_id=d["tool_call_id"],
            content=d.get("content", ""),
            is_error=d.get("is_error", False),
        )
    return TextPart(text=str(d))


def message_to_json(msg: Message) -> str:
    """Serialize a Message to a single JSON line."""
    obj = {
        "id": msg.id,
        "role": msg.role,
        "timestamp": msg.timestamp,
        "parts": [_part_to_dict(p) for p in msg.parts],
    }
    if msg.provider:
        obj["provider"] = msg.provider
    if msg.model:
        obj["model"] = msg.model
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def json_to_message(line: str) -> Message:
    """Deserialize a JSON line to a Message."""
    obj = json.loads(line)
    return Message(
        role=obj["role"],
        parts=[_dict_to_part(p) for p in obj.get("parts", [])],
        id=obj.get("id", ""),
        timestamp=obj.get("timestamp", 0.0),
        provider=obj.get("provider"),
        model=obj.get("model"),
    )


# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------

class SessionStore:
    """Manages session files and index."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or _SESSIONS_DIR
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self.base_dir / "index.json"
        self._index: list[SessionMeta] = self._load_index()
        # Sweep stale .jsonl.tmp files left over from a process kill
        # mid replace_messages. Any such temp file is by definition
        # incomplete (the rename never happened) so it's safe to
        # unlink. Without this, sessions that were never re-opened
        # leak temp garbage forever.
        try:
            for tmp in self.base_dir.glob("*.jsonl.tmp"):
                try:
                    tmp.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def _load_index(self) -> list[SessionMeta]:
        if not self._index_path.exists():
            return []
        try:
            data = json.loads(self._index_path.read_text())
            return [SessionMeta(**item) for item in data]
        except (json.JSONDecodeError, TypeError, KeyError):
            return []

    def _save_index(self) -> None:
        data = [
            {"id": s.id, "title": s.title, "created_at": s.created_at,
             "updated_at": s.updated_at, "model": s.model, "count": s.count}
            for s in self._index
        ]
        self._index_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def list_sessions(self, limit: int = 50) -> list[SessionMeta]:
        """Return sessions sorted by updated_at (newest first)."""
        return sorted(self._index, key=lambda s: s.updated_at, reverse=True)[:limit]

    def search(self, query: str, limit: int = 20) -> list[SessionMeta]:
        """Fuzzy search sessions by title."""
        q = query.lower()
        results = [s for s in self._index if q in s.title.lower()]
        return sorted(results, key=lambda s: s.updated_at, reverse=True)[:limit]

    def create_session(self, session_id: str, title: str = "New session", model: str = "") -> SessionMeta:
        """Create a new session."""
        now = time.time()
        meta = SessionMeta(
            id=session_id, title=title,
            created_at=now, updated_at=now,
            model=model, count=0,
        )
        self._index.append(meta)
        self._save_index()
        # Create empty JSONL file
        (self.base_dir / f"{session_id}.jsonl").touch()
        return meta

    def append_message(self, session_id: str, msg: Message) -> None:
        """Append a message to session file and update index."""
        path = self.base_dir / f"{session_id}.jsonl"
        line = message_to_json(msg) + "\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(line)

        # Update index
        for meta in self._index:
            if meta.id == session_id:
                meta.updated_at = time.time()
                meta.count += 1
                if msg.model and not meta.model:
                    meta.model = msg.model
                break
        self._save_index()

    def append_messages(self, session_id: str, messages: list[Message]) -> None:
        """Append multiple messages at once (batch)."""
        if not messages:
            return
        path = self.base_dir / f"{session_id}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            for msg in messages:
                f.write(message_to_json(msg) + "\n")

        for meta in self._index:
            if meta.id == session_id:
                meta.updated_at = time.time()
                meta.count += len(messages)
                last_model = next((m.model for m in reversed(messages) if m.model), None)
                if last_model and not meta.model:
                    meta.model = last_model
                break
        self._save_index()

    def replace_messages(self, session_id: str, messages: list[Message]) -> None:
        """Atomically rewrite the session file with the given messages.

        Used by manual compaction (``/compact``) — append-only would
        leave the bloated original on disk and the next ``/resume``
        would still load all of it. Atomic temp-rename keeps the
        operation crash-safe on POSIX (the rename either happens or
        doesn't; no half-written file).

        Refuses to overwrite a non-empty session file with an empty
        message list — that would silently destroy the user's
        history. A bug upstream that produces an empty buffer (or a
        race where a snapshot beats the user-message append) must
        not be able to zero out persisted state.
        """
        path = self.base_dir / f"{session_id}.jsonl"
        if not messages:
            existing_count = 0
            for meta in self._index:
                if meta.id == session_id:
                    existing_count = meta.count
                    break
            if existing_count > 0:
                # Loud log so a regression here doesn't pass silently.
                import logging
                logging.getLogger(__name__).warning(
                    "replace_messages refused to zero out session %r "
                    "(would destroy %d existing message(s))",
                    session_id, existing_count,
                )
                return
        tmp = path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for msg in messages:
                f.write(message_to_json(msg) + "\n")
        tmp.replace(path)

        for meta in self._index:
            if meta.id == session_id:
                meta.updated_at = time.time()
                meta.count = len(messages)
                last_model = next(
                    (m.model for m in reversed(messages) if m.model), None
                )
                if last_model:
                    meta.model = last_model
                break
        self._save_index()

    def load_session(self, session_id: str) -> list[Message]:
        """Load all messages from a session."""
        path = self.base_dir / f"{session_id}.jsonl"
        if not path.exists():
            return []
        messages = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    messages.append(json_to_message(line))
                except (json.JSONDecodeError, KeyError):
                    continue
        return messages

    def set_title(self, session_id: str, title: str) -> None:
        """Update session title."""
        for meta in self._index:
            if meta.id == session_id:
                meta.title = title
                self._save_index()
                return

    def set_model(self, session_id: str, model: str) -> None:
        """Update session model."""
        for meta in self._index:
            if meta.id == session_id:
                meta.model = model
                self._save_index()
                return

    def get_meta(self, session_id: str) -> SessionMeta | None:
        """Get session metadata."""
        for meta in self._index:
            if meta.id == session_id:
                return meta
        return None

    def delete_session(self, session_id: str) -> None:
        """Delete a session and its file."""
        path = self.base_dir / f"{session_id}.jsonl"
        if path.exists():
            path.unlink()
        self._index = [s for s in self._index if s.id != session_id]
        self._save_index()


# Singleton
_store: SessionStore | None = None


def get_store() -> SessionStore:
    global _store
    if _store is None:
        _store = SessionStore()
    return _store
