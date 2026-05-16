"""
cogitum.widgets.button
~~~~~~~~~~~~~~~~~~~~~~
CogButton — кастомная кнопка на базе Static.
Полный контроль над рендером через Rich, без Textual Button quirks.

Варианты:
  variant="default"  — приглушённый бронза
  variant="primary"  — золото, выделенная
  variant="danger"   — ржавчина, деструктивные действия

Использование:
    yield CogButton("+ Add provider", id="prov-add", variant="primary")

Событие:
    on(CogButton.Pressed, "#prov-add")
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from rich.text import Text
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget


# ── палитра ──────────────────────────────────────────────────────────────────
_STYLES: dict[str, dict[str, str]] = {
    "default": {
        "fg":           "#A8732D",
        "fg_hover":     "#F5C24A",
        "bg":           "#1F1B14",
        "bg_hover":     "#2A2218",
        "border":       "#3D3728",
        "border_hover": "#A8732D",
    },
    "primary": {
        "fg":           "#F5C24A",
        "fg_hover":     "#FFD96A",
        "bg":           "#1F1B14",
        "bg_hover":     "#2A2218",
        "border":       "#A8732D",
        "border_hover": "#F5C24A",
    },
    "danger": {
        "fg":           "#9B3A2A",
        "fg_hover":     "#CF5A3A",
        "bg":           "#1F1B14",
        "bg_hover":     "#251515",
        "border":       "#5A2A1A",
        "border_hover": "#9B3A2A",
    },
}


class CogButton(Widget):
    """Кастомная кнопка Imperial Fists — текст всегда по центру."""

    DEFAULT_CSS = """
    CogButton {
        height: 3;
        width: auto;
        min-width: 14;
        padding: 1 2;
        margin-right: 1;
        background: #1F1B14;
        border: tall #3D3728;
        content-align: center middle;
        color: #A8732D;
    }
    CogButton:hover {
        background: #2A2218;
        border: tall #A8732D;
        color: #F5C24A;
    }
    CogButton:focus {
        border: tall #F5C24A;
    }
    """

    COMPONENT_CLASSES: ClassVar[set[str]] = set()

    # ── сообщение ─────────────────────────────────────────────────────────────

    @dataclass
    class Pressed(Message):
        button: "CogButton"

        @property
        def control(self) -> "CogButton":
            return self.button

    # ── реактивные свойства ───────────────────────────────────────────────────

    hovered: reactive[bool] = reactive(False)

    def __init__(
        self,
        label: str,
        *,
        variant: str = "default",
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(id=id, classes=classes, disabled=disabled)
        self._label = label
        self._variant = variant if variant in _STYLES else "default"

    # ── рендер ────────────────────────────────────────────────────────────────

    def render(self) -> Text:
        style = _STYLES[self._variant]
        fg = style["fg_hover"] if self.hovered else style["fg"]
        t = Text(self._label, style=fg, justify="center", no_wrap=True)
        return t

    # ── hover ─────────────────────────────────────────────────────────────────

    def on_mouse_enter(self) -> None:
        self.hovered = True
        style = _STYLES[self._variant]
        self.styles.background = style["bg_hover"]
        self.styles.border = ("tall", style["border_hover"])
        self.refresh()

    def on_mouse_leave(self) -> None:
        self.hovered = False
        style = _STYLES[self._variant]
        self.styles.background = style["bg"]
        self.styles.border = ("tall", style["border"])
        self.refresh()

    def on_mount(self) -> None:
        style = _STYLES[self._variant]
        self.styles.background = style["bg"]
        self.styles.border = ("tall", style["border"])

    # ── клик / клавиша ────────────────────────────────────────────────────────

    def on_click(self) -> None:
        if not self.disabled:
            self.post_message(self.Pressed(self))

    def on_key(self, event) -> None:
        if event.key in ("enter", "space") and not self.disabled:
            self.post_message(self.Pressed(self))

    # ── label property ────────────────────────────────────────────────────────

    @property
    def label(self) -> str:
        return self._label

    @label.setter
    def label(self, value: str) -> None:
        self._label = value
        self.refresh()
