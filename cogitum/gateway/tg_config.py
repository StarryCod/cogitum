"""
cogitum.gateway.tg_config
~~~~~~~~~~~~~~~~~~~~~~~~~~
Telegram gateway configuration — load/save telegram.toml.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..core.platform_paths import get_config_dir

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


def load_tg_config() -> TelegramConfig:
    """Load telegram.toml, return defaults if missing."""
    if not TG_CONFIG_PATH.exists():
        return TelegramConfig()
    import sys
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib  # type: ignore[no-redef]
    with open(TG_CONFIG_PATH, "rb") as f:
        data = f.read()
    raw = tomllib.loads(data.decode())
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
    """Write telegram.toml."""
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
    TG_CONFIG_PATH.write_text("\n".join(lines), encoding="utf-8")
    try:
        TG_CONFIG_PATH.chmod(0o600)
    except OSError:
        pass
