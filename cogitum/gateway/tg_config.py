"""
cogitum.gateway.tg_config
~~~~~~~~~~~~~~~~~~~~~~~~~~
Telegram gateway configuration — load/save telegram.toml.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..core.platform_paths import get_config_dir

log = logging.getLogger(__name__)

_CONFIG_DIR = get_config_dir()

TG_CONFIG_PATH = _CONFIG_DIR / "telegram.toml"


@dataclass
class TelegramConfig:
    bot_token: str = ""
    # Access control. Two layers:
    #   allowed_user_id  — single private-chat operator. 0 means "no
    #                      private-chat lock" (any user who messages the
    #                      bot 1:1 can talk to it).
    #   allowed_chat_ids — group/supergroup ids where the bot acts as a
    #                      moderator/companion. Messages from these chats
    #                      bypass the user-id check entirely.
    # If both are 0/empty the bot is fully open — only do that for local
    # testing or explicitly intended public bots.
    allowed_user_id: int = 0
    allowed_chat_ids: list[int] = field(default_factory=list)
    enabled: bool = False
    # Display
    show_thinking: bool = True
    show_tool_calls: bool = True
    # Model
    default_model: str = ""
    # Default skill loaded for every conversation, before any user input.
    # Used to put the bot in moderator-mode (no tools, conversational
    # only) for group chats. Empty = no default skill.
    default_skill: str = ""

    def is_valid(self) -> bool:
        # Bot is operable as long as it has a token and at least one
        # access path: private user, allowed chat list, or explicit
        # public mode (rare; user has to set enabled + leave both empty).
        return bool(self.bot_token)

    def can_respond(self, *, user_id: int, chat_id: int) -> bool:
        """Decide whether the bot should reply to this message.

        Order:
          1. Allowed chat (group/supergroup) wins — answer for everyone there.
          2. Private operator id matches — answer.
          3. Both gates closed — silent (no leakage that the bot exists).
        """
        if self.allowed_chat_ids and chat_id in self.allowed_chat_ids:
            return True
        if self.allowed_user_id and user_id == self.allowed_user_id:
            return True
        # Fully open mode (no allowlists at all) — only honour it in
        # private chats to avoid the bot being added to a random group
        # and replying to everyone by accident.
        if not self.allowed_user_id and not self.allowed_chat_ids:
            return chat_id == user_id  # private chat: chat_id == user_id on TG
        return False


def _enforce_secure_perms(path: Path) -> None:
    """Ensure telegram.toml is not readable by group or other.

    The token is the bot's only credential — a stray ``chmod 644``
    (e.g. by a backup tool, an editor, or a hand-edit) would expose
    it to every local user. We re-tighten to 0600 on every load and
    log a WARNING describing what we did, so the operator notices
    that something dropped the perms.

    POSIX-only by virtue of stat().st_mode bits; a no-op on Windows
    (chmod is best-effort there too).
    """
    try:
        st = path.stat()
    except OSError:
        return
    if st.st_mode & 0o077:
        log.warning(
            "telegram.toml at %s has overly-permissive mode %#o; "
            "tightening to 0600 (was group/other readable).",
            path, st.st_mode & 0o777,
        )
        try:
            path.chmod(0o600)
        except OSError as e:
            log.warning("Failed to chmod %s to 0600: %s", path, e)


def load_tg_config() -> TelegramConfig:
    """Load telegram.toml, return defaults if missing or unreadable.

    A corrupted config (Ctrl+C mid-save before the atomic-write fix
    that ships in this commit, manual hand-edit gone wrong, partial
    write from a power loss) used to bubble tomllib.TOMLDecodeError
    straight up to the caller — the bot would refuse to start with
    an opaque traceback. We now log a warning and fall back to a
    blank config so ``cog tg setup`` can rewrite a clean file
    instead of demanding the user fix the corruption by hand.
    """
    if not TG_CONFIG_PATH.exists():
        return TelegramConfig()
    _enforce_secure_perms(TG_CONFIG_PATH)
    import sys
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib  # type: ignore[no-redef]
    try:
        with open(TG_CONFIG_PATH, "rb") as f:
            data = f.read()
        raw = tomllib.loads(data.decode())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        log.warning(
            "telegram.toml at %s is unreadable or malformed (%s); "
            "falling back to defaults. Run `cog tg setup` to rewrite.",
            TG_CONFIG_PATH, e,
        )
        return TelegramConfig()
    tg = raw.get("telegram", raw)
    return TelegramConfig(
        bot_token=str(tg.get("bot_token", "")),
        allowed_user_id=int(tg.get("allowed_user_id", 0)),
        allowed_chat_ids=[int(x) for x in tg.get("allowed_chat_ids", [])],
        enabled=bool(tg.get("enabled", False)),
        show_thinking=bool(tg.get("show_thinking", True)),
        show_tool_calls=bool(tg.get("show_tool_calls", True)),
        default_model=str(tg.get("default_model", "")),
        default_skill=str(tg.get("default_skill", "")),
    )


def save_tg_config(cfg: TelegramConfig) -> None:
    """Write telegram.toml atomically.

    Plain ``write_text`` left a window where Ctrl+C between
    file-open-truncate and write-completion would corrupt the TOML
    on disk — next ``load_tg_config`` then bombed with a parser
    error and the bot wouldn't start. ``atomic_write_text`` writes
    to a sibling ``.tmp`` and ``os.replace``s into place, so a
    crash mid-save leaves either the previous content or the new
    one — never a half-written file.
    """
    from ..core.atomic_io import atomic_write_text

    TG_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    chat_ids_repr = "[" + ", ".join(str(x) for x in cfg.allowed_chat_ids) + "]"
    lines = [
        "# Cogitum Telegram Gateway configuration",
        "",
        "[telegram]",
        f'bot_token = "{cfg.bot_token}"',
        f"allowed_user_id = {cfg.allowed_user_id}",
        f"allowed_chat_ids = {chat_ids_repr}",
        f"enabled = {'true' if cfg.enabled else 'false'}",
        f"show_thinking = {'true' if cfg.show_thinking else 'false'}",
        f"show_tool_calls = {'true' if cfg.show_tool_calls else 'false'}",
        f'default_model = "{cfg.default_model}"',
        f'default_skill = "{cfg.default_skill}"',
        "",
    ]
    atomic_write_text(TG_CONFIG_PATH, "\n".join(lines))
    try:
        TG_CONFIG_PATH.chmod(0o600)
    except OSError:
        pass
