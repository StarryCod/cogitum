"""
cogitum.widgets.composer
~~~~~~~~~~~~~~~~~~~~~~~~
Composer — поле ввода с выпадающим меню команд.

Enter — отправка. Ctrl+Enter — новая строка.
Up/Down — история предыдущих сообщений (когда поле пустое или на первой строке).
Ctrl+V — вставка. Большой текст (>100 символов) показывается как
[Pasted content N lines] в поле, но отправляется полностью.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from rich.text import Text
from textual import on, events
from textual.app import ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static, TextArea

from ..design import GOLD, GOLD_DIM, GOLD_HI, MUTED, TXT, TXT_DIM, BRONZE


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
    CommandDef("resume", "Resume a past session", aliases=["r"]),
    CommandDef("title", "Set session title: /title <name>", aliases=["t"]),
    CommandDef("tools", "List available tools"),
    CommandDef("godmode", "Jailbreak: on/off/classic/plinian/subtle", aliases=["gm"]),
    CommandDef("clear", "Clear feed display", aliases=["cls", "c"]),
    CommandDef("help", "Show all commands", aliases=["h", "?"]),
    CommandDef("quit", "Exit Cogitum", aliases=["q", "exit"], shortcut="Ctrl+Q"),
]


def _match_command(text: str) -> CommandDef | None:
    name = text.lower().strip()
    for cmd in COMMANDS:
        if cmd.name == name:
            return cmd
        if cmd.aliases and name in cmd.aliases:
            return cmd
    return None


def _filter_commands(query: str) -> list[CommandDef]:
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

            out.append(prefix, style=GOLD_HI if is_sel else MUTED)
            out.append(f"/{cmd.name}", style=name_style)
            out.append(f"  {cmd.description}", style=desc_style)
            if cmd.shortcut:
                out.append(f"  [{cmd.shortcut}]", style=GOLD_DIM)
            if i < len(self._items) - 1:
                out.append("\n")
        self.update(out)


# ── Paste indicator ──────────────────────────────────────────────────────────

_PASTE_THRESHOLD = 100  # chars


# ── Custom TextArea — Enter submits, Ctrl+Enter newline ──────────────────────

class ComposerArea(TextArea):
    """TextArea where Enter submits instead of inserting newline."""

    @dataclass
    class SubmitRequest(Message):
        pass

    @dataclass
    class HistoryRequest(Message):
        direction: int  # -1 = older, +1 = newer

    @dataclass
    class PasteReceived(Message):
        """Bubbles up to Composer with paste text before TextArea processes it."""
        text: str

    def _on_paste(self, event: events.Paste) -> None:
        """Intercept terminal paste event BEFORE TextArea inserts it."""
        text = event.text or ""
        if not text:
            return
        if len(text) > _PASTE_THRESHOLD:
            # Large paste — block TextArea from inserting, let Composer handle
            event.prevent_default()
            event.stop()
            self.post_message(self.PasteReceived(text=text))
        else:
            # Small paste — let TextArea handle normally
            super()._on_paste(event)

    def action_paste(self) -> None:
        """Override Ctrl+V action — intercept large clipboard content."""
        clipboard = self.app.clipboard
        if not clipboard:
            return
        if len(clipboard) > _PASTE_THRESHOLD:
            # Large clipboard — don't insert, send to Composer
            self.post_message(self.PasteReceived(text=clipboard))
        else:
            # Small — normal paste
            super().action_paste()

    async def _on_key(self, event: events.Key) -> None:
        """Intercept Enter BEFORE TextArea processes it."""
        if event.key == "enter":
            # Submit
            event.prevent_default()
            event.stop()
            self.post_message(self.SubmitRequest())
            return
        elif event.key == "ctrl+j" or event.key == "shift+enter":
            # Newline
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        elif event.key in ("backspace", "delete"):
            # If cursor is inside or adjacent to placeholder — delete entire placeholder
            composer = self.parent
            if hasattr(composer, '_pasted_content') and composer._pasted_content:
                placeholder = composer._paste_placeholder()
                text = self.text
                ph_start = text.find(placeholder)
                if ph_start != -1:
                    ph_end = ph_start + len(placeholder)
                    # Get cursor position as offset in text
                    row, col = self.cursor_location
                    lines = text.split("\n")
                    cursor_offset = sum(len(lines[i]) + 1 for i in range(row)) + col
                    # If cursor is inside or at edges of placeholder — delete whole thing
                    if ph_start <= cursor_offset <= ph_end:
                        event.prevent_default()
                        event.stop()
                        new_text = text[:ph_start] + text[ph_end:]
                        self.load_text(new_text)
                        # Position cursor where placeholder was
                        offset = ph_start
                        r, c = 0, 0
                        for i, line in enumerate(new_text.split("\n")):
                            if offset <= len(line):
                                r, c = i, offset
                                break
                            offset -= len(line) + 1
                        self.move_cursor((r, c))
                        composer._pasted_content = None
                        return
        elif event.key == "up":
            # If menu is active — let parent handle navigation
            composer = self.parent
            if hasattr(composer, '_menu_active') and composer._menu_active:
                return
            # If on first line and text is empty or cursor at start → history
            if self.cursor_location[0] == 0:
                event.prevent_default()
                event.stop()
                self.post_message(self.HistoryRequest(direction=-1))
                return
        elif event.key == "down":
            # If menu is active — let parent handle navigation
            composer = self.parent
            if hasattr(composer, '_menu_active') and composer._menu_active:
                return
            # If on last line → history forward
            lines = self.text.split("\n")
            if self.cursor_location[0] >= len(lines) - 1:
                event.prevent_default()
                event.stop()
                self.post_message(self.HistoryRequest(direction=1))
                return
        elif event.key in ("left", "right"):
            # Skip over placeholder as if it's one character
            composer = self.parent
            if hasattr(composer, '_pasted_content') and composer._pasted_content:
                placeholder = composer._paste_placeholder()
                text = self.text
                ph_start = text.find(placeholder)
                if ph_start != -1:
                    ph_end = ph_start + len(placeholder)
                    row, col = self.cursor_location
                    lines = text.split("\n")
                    cursor_offset = sum(len(lines[i]) + 1 for i in range(row)) + col
                    if event.key == "right" and ph_start <= cursor_offset < ph_end:
                        # Jump to end of placeholder
                        event.prevent_default()
                        event.stop()
                        offset = ph_end
                        r, c = 0, 0
                        for i, line in enumerate(lines):
                            if offset <= len(line):
                                r, c = i, offset
                                break
                            offset -= len(line) + 1
                        self.move_cursor((r, c))
                        return
                    elif event.key == "left" and ph_start < cursor_offset <= ph_end:
                        # Jump to start of placeholder
                        event.prevent_default()
                        event.stop()
                        offset = ph_start
                        r, c = 0, 0
                        for i, line in enumerate(lines):
                            if offset <= len(line):
                                r, c = i, offset
                                break
                            offset -= len(line) + 1
                        self.move_cursor((r, c))
                        return
        # Everything else — let TextArea handle normally
        await super()._on_key(event)


# ── Composer widget ──────────────────────────────────────────────────────────

class Composer(Widget):
    """Multi-line input with slash-command autocomplete + history + paste."""

    DEFAULT_CSS = """
    Composer {
        height: auto;
        max-height: 20;
        width: 100%;
        layout: vertical;
    }
    ComposerArea {
        height: auto;
        min-height: 3;
        max-height: 7;
        background: #1A1816;
        color: #E6E1CF;
        border: none;
        border-top: solid #A8732D;
        padding: 0 1;
    }
    ComposerArea:focus {
        background: #1A1816;
        border: none;
        border-top: solid #F5C24A;
    }
    ComposerArea > .text-area--cursor-line {
        background: #1A1816;
    }
    """

    COMPONENT_CLASSES: ClassVar[set[str]] = set()

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self._menu_active = False
        self._history: list[str] = []
        self._history_idx: int = -1  # -1 = not browsing history
        self._draft: str = ""  # saved draft when browsing history
        self._pasted_content: str | None = None  # full pasted text when collapsed
        self._paste_inserting: bool = False  # guard: ignore Changed during insert

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
        yield ComposerArea(id="composer-area", language=None, show_line_numbers=False, theme="css")

    # ── Input handling ────────────────────────────────────────────────────────

    @on(TextArea.Changed, "#composer-area")
    def _on_text_changed(self, event: TextArea.Changed) -> None:
        area = self.query_one("#composer-area", ComposerArea)
        text = area.text
        menu = self.query_one("#cmd-menu", CommandMenu)

        # If paste is active and placeholder was removed from text — cancel paste
        # Skip check during our own insert operation
        if self._pasted_content and not self._paste_inserting:
            placeholder = self._paste_placeholder()
            if placeholder not in text:
                self._pasted_content = None

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

    @on(ComposerArea.SubmitRequest)
    def _on_submit_request(self, event: ComposerArea.SubmitRequest) -> None:
        menu = self.query_one("#cmd-menu", CommandMenu)
        area = self.query_one("#composer-area", ComposerArea)

        if self._menu_active and menu.is_visible:
            cmd = menu.selected_command
            menu.hide()
            self._menu_active = False
            if cmd:
                area.clear()
                self.post_message(self.Submitted(value=f"/{cmd.name}"))
            return

        # Get the actual text to send
        if self._pasted_content:
            # Replace placeholder with real content
            placeholder = self._paste_placeholder()
            raw = area.text
            if placeholder in raw:
                text = raw.replace(placeholder, self._pasted_content, 1).strip()
            else:
                # Placeholder was removed but _pasted_content still set — just use area text
                text = raw.strip()
            self._pasted_content = None
        else:
            text = area.text.strip()

        if not text:
            return

        # Resolve aliases
        if text.startswith("/"):
            cmd_text = text[1:].split()[0]
            args = text[1:][len(cmd_text):].strip()
            matched = _match_command(cmd_text)
            if matched:
                text = f"/{matched.name}" + (f" {args}" if args else "")

        # Add to history
        if text and (not self._history or self._history[-1] != text):
            self._history.append(text)
        self._history_idx = -1
        self._draft = ""

        area.clear()
        menu.hide()
        self._menu_active = False
        self.post_message(self.Submitted(value=text))

    @on(ComposerArea.HistoryRequest)
    def _on_history_request(self, event: ComposerArea.HistoryRequest) -> None:
        # If app has queued messages and agent is running, let app handle this
        # (pop from queue back to composer for editing)
        app = self.app
        if (event.direction == -1
            and hasattr(app, '_pending_messages') and app._pending_messages
            and hasattr(app, '_agent_task') and app._agent_task
            and not app._agent_task.done()):
            # Don't handle — let it bubble to app's _on_history_for_queue
            return

        if not self._history:
            return

        area = self.query_one("#composer-area", ComposerArea)

        if event.direction == -1:  # older
            if self._history_idx == -1:
                # Save current draft
                self._draft = area.text
                self._history_idx = len(self._history) - 1
            elif self._history_idx > 0:
                self._history_idx -= 1
            else:
                return  # at oldest

            area.load_text(self._history[self._history_idx])

        elif event.direction == 1:  # newer
            if self._history_idx == -1:
                return  # not browsing

            if self._history_idx < len(self._history) - 1:
                self._history_idx += 1
                area.load_text(self._history[self._history_idx])
            else:
                # Back to draft
                self._history_idx = -1
                area.load_text(self._draft)

    def on_key(self, event) -> None:
        """Arrow keys for menu navigation."""
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

    @on(ComposerArea.PasteReceived)
    def _on_paste_received(self, event: ComposerArea.PasteReceived) -> None:
        """Handle large paste — store content, show placeholder."""
        self._pasted_content = event.text
        placeholder = self._paste_placeholder()
        area = self.query_one("#composer-area", ComposerArea)
        self._paste_inserting = True
        area.insert(placeholder)
        self._paste_inserting = False

    def _paste_placeholder(self) -> str:
        """Generate placeholder text for pasted content."""
        if not self._pasted_content:
            return ""
        chars = len(self._pasted_content)
        lines = -(-chars // 80)  # ceiling division: visual lines at 80 cols
        word = "line" if lines == 1 else "lines"
        return f"[Pasted content: {lines} {word}]"

    # ── Public API ────────────────────────────────────────────────────────────

    def set_enabled(self, enabled: bool) -> None:
        """Track enabled state but never disable the TextArea itself.
        Users should always be able to type (messages get queued)."""
        # Keep area always enabled — app handles queueing
        pass

    def focus_input(self) -> None:
        self.query_one("#composer-area", ComposerArea).focus()

    def add_to_history(self, text: str) -> None:
        """Add a message to history from outside (e.g. app restoring session)."""
        if text and (not self._history or self._history[-1] != text):
            self._history.append(text)
