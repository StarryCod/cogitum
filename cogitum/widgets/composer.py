"""
cogitum.widgets.composer
~~~~~~~~~~~~~~~~~~~~~~~~
Composer — многострочное поле ввода с выпадающим меню команд.

Фичи:
  - TextArea: растёт до 5 строк, потом скроллится
  - При вводе / появляется dropdown со списком команд
  - Фильтрация по мере ввода
  - Стрелки вверх/вниз для навигации по меню
  - Enter сразу выполняет выбранную команду
  - Shift+Enter — новая строка
  - Enter без меню — отправка сообщения
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static, TextArea

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
    """Dropdown with filtered command list."""

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
    """Multi-line input with slash-command autocomplete."""

    DEFAULT_CSS = """
    Composer {
        height: auto;
        max-height: 20;
        width: 100%;
        layout: vertical;
    }
    #composer-area {
        height: auto;
        min-height: 3;
        max-height: 7;
        background: #14120E;
        color: #E6E1CF;
        border: tall #2A2620;
    }
    #composer-area:focus {
        border: tall #A8732D;
    }
    #composer-area .text-area--cursor {
        color: #F5C24A;
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
        yield TextArea(id="composer-area", language=None, show_line_numbers=False)

    def on_mount(self) -> None:
        area = self.query_one("#composer-area", TextArea)
        area.theme = "css"  # Use CSS styling, no syntax theme

    # ── Input handling ────────────────────────────────────────────────────────

    @on(TextArea.Changed, "#composer-area")
    def _on_text_changed(self, event: TextArea.Changed) -> None:
        area = self.query_one("#composer-area", TextArea)
        text = area.text
        menu = self.query_one("#cmd-menu", CommandMenu)

        # Only show menu if first line starts with / and no space yet
        first_line = text.split("\n")[0] if text else ""
        if first_line.startswith("/") and " " not in first_line and "\n" not in text:
            query = first_line[1:]
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

    def on_key(self, event) -> None:
        menu = self.query_one("#cmd-menu", CommandMenu)

        # Menu navigation
        if self._menu_active and menu.is_visible:
            if event.key == "up":
                menu.move_up()
                event.prevent_default()
                event.stop()
                return
            elif event.key == "down":
                menu.move_down()
                event.prevent_default()
                event.stop()
                return
            elif event.key == "escape":
                menu.hide()
                self._menu_active = False
                event.prevent_default()
                event.stop()
                return
            elif event.key == "enter":
                # Pick command and submit
                cmd = menu.selected_command
                menu.hide()
                self._menu_active = False
                if cmd:
                    area = self.query_one("#composer-area", TextArea)
                    area.clear()
                    self.post_message(self.Submitted(value=f"/{cmd.name}"))
                event.prevent_default()
                event.stop()
                return

        # Normal Enter (no menu) — submit; Shift+Enter — newline
        if event.key == "enter":
            area = self.query_one("#composer-area", TextArea)
            text = area.text.strip()
            if not text:
                event.prevent_default()
                event.stop()
                return

            # Resolve aliases
            if text.startswith("/"):
                cmd_text = text[1:].split()[0]
                args = text[1:][len(cmd_text):].strip()
                matched = _match_command(cmd_text)
                if matched:
                    text = f"/{matched.name}" + (f" {args}" if args else "")

            area.clear()
            menu.hide()
            self._menu_active = False
            self.post_message(self.Submitted(value=text))
            event.prevent_default()
            event.stop()

    # ── Public API ────────────────────────────────────────────────────────────

    def set_enabled(self, enabled: bool) -> None:
        area = self.query_one("#composer-area", TextArea)
        area.disabled = not enabled

    def focus_input(self) -> None:
        self.query_one("#composer-area", TextArea).focus()
