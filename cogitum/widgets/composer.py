"""
cogitum.widgets.composer
~~~~~~~~~~~~~~~~~~~~~~~~
Composer — поле ввода с выпадающим меню команд.

Фичи:
  - При вводе / появляется dropdown со списком команд
  - Фильтрация по мере ввода
  - Стрелки вверх/вниз для навигации
  - Enter сразу выполняет команду (не нужен двойной Enter)
  - Escape закрывает меню
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
    aliases: list[str] | None = None
    shortcut: str = ""


COMMANDS: list[CommandDef] = [
    CommandDef("setup", "Provider & auth wizard", aliases=["s"], shortcut="Ctrl+,"),
    CommandDef("models", "Open model picker", aliases=["m"], shortcut="Ctrl+M"),
    CommandDef("model", "Switch model: /model <id>", aliases=["mod"]),
    CommandDef("new", "Clear history, start fresh", aliases=["n", "reset"]),
    CommandDef("tools", "List available tools", aliases=["t"]),
    CommandDef("clear", "Clear feed display", aliases=["cls", "c"]),
    CommandDef("help", "Show all commands", aliases=["h", "?"]),
    CommandDef("quit", "Exit Cogitum", aliases=["q", "exit"], shortcut="Ctrl+Q"),
]


def _match_command(text: str) -> CommandDef | None:
    """Match exact command name or alias."""
    name = text.lower().strip()
    for cmd in COMMANDS:
        if cmd.name == name:
            return cmd
        if cmd.aliases and name in cmd.aliases:
            return cmd
    return None


def _filter_commands(query: str) -> list[CommandDef]:
    """Filter commands by prefix."""
    q = query.lower().strip()
    if not q:
        return list(COMMANDS)
    results = []
    for cmd in COMMANDS:
        if cmd.name.startswith(q):
            results.append(cmd)
        elif cmd.aliases and any(a.startswith(q) for a in cmd.aliases):
            results.append(cmd)
    return results


# ── Dropdown menu ────────────────────────────────────────────────────────────

class CommandMenu(Static):
    """Floating dropdown with filtered command list."""

    DEFAULT_CSS = """
    CommandMenu {
        display: none;
        width: 100%;
        max-height: 12;
        height: auto;
        background: #161618;
        border: round #2A2620;
        padding: 0 1;
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
    #composer-input {
        height: 3;
        background: #14120E;
        color: #E6E1CF;
        border: tall #2A2620;
        padding: 1 2;
    }
    #composer-input:focus {
        border: tall #A8732D;
    }
    """

    COMPONENT_CLASSES: ClassVar[set[str]] = set()

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self._menu_active = False

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
        yield Input(placeholder="message or /command…", id="composer-input")

    # ── Input handling ────────────────────────────────────────────────────────

    @on(Input.Changed, "#composer-input")
    def _on_input_changed(self, event: Input.Changed) -> None:
        text = event.value
        menu = self.query_one("#cmd-menu", CommandMenu)

        if text.startswith("/") and " " not in text:
            query = text[1:]
            filtered = _filter_commands(query)
            if filtered:
                menu.show(filtered)
                self._menu_active = True
            else:
                menu.hide()
                self._menu_active = False
        else:
            menu.hide()
            self._menu_active = False

    @on(Input.Submitted, "#composer-input")
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        event.prevent_default()
        event.stop()

        menu = self.query_one("#cmd-menu", CommandMenu)
        inp = self.query_one("#composer-input", Input)

        if self._menu_active and menu.is_visible:
            # Pick selected command and submit immediately
            cmd = menu.selected_command
            menu.hide()
            self._menu_active = False
            if cmd:
                inp.value = ""
                self.post_message(self.Submitted(value=f"/{cmd.name}"))
            return

        # Normal submit — resolve aliases
        text = inp.value.strip()
        if not text:
            return

        if text.startswith("/"):
            # Check if it's an alias
            cmd_text = text[1:].split()[0]
            args = text[1:][len(cmd_text):].strip()
            matched = _match_command(cmd_text)
            if matched:
                text = f"/{matched.name}" + (f" {args}" if args else "")

        inp.value = ""
        menu.hide()
        self._menu_active = False
        self.post_message(self.Submitted(value=text))

    def on_key(self, event) -> None:
        menu = self.query_one("#cmd-menu", CommandMenu)
        if not self._menu_active or not menu.is_visible:
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
            self._menu_active = False
            event.prevent_default()
            event.stop()

    # ── Public API ────────────────────────────────────────────────────────────

    def set_enabled(self, enabled: bool) -> None:
        inp = self.query_one("#composer-input", Input)
        inp.disabled = not enabled

    def focus_input(self) -> None:
        self.query_one("#composer-input", Input).focus()
