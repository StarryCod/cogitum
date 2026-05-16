"""
cogitum.gateway.tg_config
~~~~~~~~~~~~~~~~~~~~~~~~~~
Telegram gateway configuration — load/save telegram.toml.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_CONFIG_DIR = Path(
    os.environ.get("COGITUM_CONFIG_DIR")
    or os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
) / "cogitum"

TG_CONFIG_PATH = _CONFIG_DIR / "telegram.toml"


@dataclass
class TelegramConfig:
    bot_token: str = ""
    allowed_user_id: int = 0
    enabled: bool = False
    # Display
    show_thinking: bool = True
    show_tool_calls: bool = True
    # Model
    default_model: str = ""

    def is_valid(self) -> bool:
        return bool(self.bot_token) and self.allowed_user_id > 0


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
        enabled=bool(tg.get("enabled", False)),
        show_thinking=bool(tg.get("show_thinking", True)),
        show_tool_calls=bool(tg.get("show_tool_calls", True)),
        default_model=str(tg.get("default_model", "")),
    )


def save_tg_config(cfg: TelegramConfig) -> None:
    """Write telegram.toml."""
    TG_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Cogitum Telegram Gateway configuration",
        "",
        "[telegram]",
        f'bot_token = "{cfg.bot_token}"',
        f"allowed_user_id = {cfg.allowed_user_id}",
        f"enabled = {'true' if cfg.enabled else 'false'}",
        f"show_thinking = {'true' if cfg.show_thinking else 'false'}",
        f"show_tool_calls = {'true' if cfg.show_tool_calls else 'false'}",
        f'default_model = "{cfg.default_model}"',
        "",
    ]
    TG_CONFIG_PATH.write_text("\n".join(lines), encoding="utf-8")
    try:
        TG_CONFIG_PATH.chmod(0o600)
    except OSError:
        pass
