"""
cogitum.widgets.composer
~~~~~~~~~~~~~~~~~~~~~~~~
Composer — поле ввода с выпадающим меню команд.

Фичи:
  - При вводе / появляется dropdown со списком команд
  - Фильтрация по мере ввода
  - Стрелки вверх/вниз для навигации
  - Enter вставляет команду но не отправляет
  - Escape закрывает меню
  - Enter без меню — отправка сообщения
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Input, Static

from ..design import GOLD, GOLD_DIM, GOLD_HI, MUTED, TXT, TXT_DIM


# ── Command definitions ──────────────────────────────────────────────────────

@dataclass
class CommandDef:
    name: str
    description: str
    shortcut: str = ""


COMMANDS: list[CommandDef] = [
    CommandDef("setup", "Provider & auth wizard", "Ctrl+,"),
    CommandDef("models", "Open model picker", "Ctrl+M"),
    CommandDef("model", "Switch model: /model <id>"),
    CommandDef("new", "Clear history, start fresh"),
    CommandDef("tools", "List available tools"),
    CommandDef("clear", "Clear feed display"),
    CommandDef("help", "Show all commands"),
    CommandDef("quit", "Exit Cogitum", "Ctrl+Q"),
]


# ── Dropdown menu ────────────────────────────────────────────────────────────

class CommandMenu(Static):
    """Floating dropdown with filtered command list."""

    DEFAULT_CSS = """
    CommandMenu {
        display: none;
        layer: overlay;
        dock: bottom;
        width: 100%;
        max-height: 12;
        height: auto;
        background: #161618;
        border: round #2A2620;
        padding: 0 1;
        margin-bottom: 3;
    }
    CommandMenu.visible {
        display: block;
    }
    """

    selected_index: reactive[int] = reactive(0)

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self._items: list[CommandDef] = []

    def show(self, items: list[CommandDef]) -> None:
        self._items = items
        self.selected_index = 0
        self._render_items()
        self.add_class("visible")

    def hide(self) -> None:
        self.remove_class("visible")
        self._items = []

    @property
    def is_visible(self) -> bool:
        return self.has_class("visible")

    @property
    def selected_command(self) -> CommandDef | None:
        if self._items and 0 <= self.selected_index < len(self._items):
            return self._items[self.selected_index]
        return None

    def move_up(self) -> None:
        if self._items:
            self.selected_index = (self.selected_index - 1) % len(self._items)
            self._render_items()

    def move_down(self) -> None:
        if self._items:
            self.selected_index = (self.selected_index + 1) % len(self._items)
            self._render_items()

    def _render_items(self) -> None:
        out = Text()
        for i, cmd in enumerate(self._items):
            is_sel = i == self.selected_index
            prefix = "▸ " if is_sel else "  "
            name_style = f"bold {GOLD_HI}" if is_sel else GOLD
            desc_style = TXT if is_sel else TXT_DIM
            shortcut_style = GOLD_DIM

            out.append(prefix, style=GOLD_HI if is_sel else MUTED)
            out.append(f"/{cmd.name}", style=name_style)
            out.append(f"  {cmd.description}", style=desc_style)
            if cmd.shortcut:
                out.append(f"  [{cmd.shortcut}]", style=shortcut_style)
            if i < len(self._items) - 1:
                out.append("\n")
        self.update(out)


# ── Composer widget ──────────────────────────────────────────────────────────

class Composer(Widget):
    """Input field with slash-command autocomplete dropdown."""

    DEFAULT_CSS = """
    Composer {
        height: auto;
        max-height: 16;
        width: 100%;
        layout: vertical;
    }
    #composer-bar {
        height: 3;
        width: 100%;
        layout: horizontal;
        background: #14120E;
    }
    #composer-prefix {
        width: 4;
        height: 3;
        color: #F5C24A;
        background: #14120E;
        content-align: center middle;
    }
    #composer-input {
        height: 3;
        background: #14120E;
        color: #E6E1CF;
        border: none;
        padding: 1 1;
    }
    #composer-input:focus {
        border: none;
    }
    """

    COMPONENT_CLASSES: ClassVar[set[str]] = set()

    # ── Messages ──────────────────────────────────────────────────────────────

    @dataclass
    class Submitted(Message):
        value: str

        @property
        def control(self) -> "Composer":
            return self._sender  # type: ignore[return-value]

    # ── Compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield CommandMenu(id="cmd-menu")
        with Widget(id="composer-bar"):
            yield Static("▶", id="composer-prefix")
            yield Input(placeholder="type your task or /command…", id="composer-input")

    # ── Input handling ────────────────────────────────────────────────────────

    @on(Input.Changed, "#composer-input")
    def _on_input_changed(self, event: Input.Changed) -> None:
        text = event.value
        menu = self.query_one("#cmd-menu", CommandMenu)

        if text.startswith("/"):
            query = text[1:].lower().strip()
            filtered = [
                cmd for cmd in COMMANDS
                if query == "" or cmd.name.startswith(query)
            ]
            if filtered:
                menu.show(filtered)
            else:
                menu.hide()
        else:
            menu.hide()

    @on(Input.Submitted, "#composer-input")
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        event.prevent_default()
        event.stop()

        menu = self.query_one("#cmd-menu", CommandMenu)
        inp = self.query_one("#composer-input", Input)

        if menu.is_visible:
            # Insert selected command, don't submit
            cmd = menu.selected_command
            if cmd:
                inp.value = f"/{cmd.name} "
                inp.cursor_position = len(inp.value)
            menu.hide()
            return

        # Normal submit
        text = inp.value.strip()
        if text:
            inp.value = ""
            self.post_message(self.Submitted(value=text))

    def on_key(self, event) -> None:
        menu = self.query_one("#cmd-menu", CommandMenu)
        if not menu.is_visible:
            return

        if event.key == "up":
            menu.move_up()
            event.prevent_default()
            event.stop()
        elif event.key == "down":
            menu.move_down()
            event.prevent_default()
            event.stop()
        elif event.key == "escape":
            menu.hide()
            event.prevent_default()
            event.stop()

    # ── Public API ────────────────────────────────────────────────────────────

    def set_enabled(self, enabled: bool) -> None:
        inp = self.query_one("#composer-input", Input)
        inp.disabled = not enabled
        prefix = self.query_one("#composer-prefix", Static)
        prefix.update("▶" if enabled else "⏳")

    def focus_input(self) -> None:
        self.query_one("#composer-input", Input).focus()
