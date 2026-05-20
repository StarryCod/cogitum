"""
cogitum.widgets.retry_confirm
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Modal that escalates a stuck retry loop to the user.

Design follows the same pattern as ``ConfirmModal``: ``dismiss(bool)``
on every decision path, caller reads the value via
``push_screen_wait``. No futures, no parallel state — one source of
truth, three decision paths (Continue button, Abort button, auto-tick),
all funnel through ``_finish`` → ``dismiss``.

The "Abort doesn't work" bug came from a previous design that fanned
out: modal set a future, called dismiss, the bridge layer did its own
thing. That had three moving parts and they raced. This file owns
exactly one decision via dismiss() and that's it.
"""
from __future__ import annotations

import logging
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Button, Static

from rich.text import Text

from ..design import (
    GOLD_HI,
    GOLD,
    GOLD_DIM,
    BRONZE,
    COPPER,
    TXT,
    TXT_DIM,
    RUST,
    BG_SOFT,
)


log = logging.getLogger(__name__)


_TITLES: dict[str, str] = {
    "quota": "Quota exceeded",
    "rate_limit": "Rate limited",
    "overloaded": "Provider overloaded",
    "server": "Provider server error",
    "network": "Network error",
    "pool": "All keys cooling down",
    "unknown": "Stream failed",
}


_HINTS: dict[str, str] = {
    "quota": (
        "API account is out of credits or hit a billing limit. Waiting "
        "won't help — top up the account, switch provider, or abort."
    ),
    "rate_limit": (
        "Provider's per-minute or per-hour cap. Usually clears within "
        "seconds; safe to keep waiting."
    ),
    "overloaded": (
        "Provider is under heavy load. Recovers in seconds — retry is "
        "the right call."
    ),
    "server": (
        "Provider returned 5xx. Could be transient — give it a minute."
    ),
    "network": (
        "Connection blip on your side. Retrying usually fixes it."
    ),
    "pool": (
        "Every key in the pool is in cooldown. Waiting for the soonest "
        "to expire is normal."
    ),
    "unknown": (
        "Couldn't classify the error. See message below for details."
    ),
}


class RetryConfirmModal(ModalScreen[bool]):
    """Yes/No prompt with auto-continue countdown.

    Returns ``True`` to keep retrying, ``False`` to abort. Caller waits
    via ``push_screen_wait``; never trust the return value alone if
    other code paths might dismiss the screen — but in this design they
    can't, only ``_finish`` ever dismisses.
    """

    DEFAULT_CSS = f"""
    RetryConfirmModal {{
        align: center middle;
        background: rgba(0,0,0,0.55);
    }}
    #rc-shell {{
        width: 76;
        max-width: 90%;
        padding: 1 2;
        background: {BG_SOFT};
        border: round {BRONZE};
        height: auto;
    }}
    #rc-title  {{ color: {GOLD_HI}; text-style: bold; padding-bottom: 1; }}
    #rc-hint   {{ color: {TXT}; padding-bottom: 1; }}
    #rc-msg    {{ color: {BRONZE}; padding-bottom: 1; max-height: 8; }}
    #rc-timer  {{ color: {TXT_DIM}; padding-bottom: 1; }}
    #rc-foot   {{ height: 3; align: right middle; }}
    #rc-foot Button {{ margin-left: 1; min-width: 20; }}
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "abort", "Abort", priority=True),
        Binding("enter", "kontinue", "Continue", priority=True),
    ]

    def __init__(
        self,
        *,
        attempt: int,
        max_attempts: int,
        error_class: str,
        error_message: str,
        auto_continue_in: float,
    ) -> None:
        super().__init__()
        self._attempt = attempt
        self._max_attempts = max_attempts
        self._error_class = error_class
        self._error_message = error_message
        self._remaining = float(auto_continue_in)
        self._tick_timer: Timer | None = None
        # Race guard for the four entrypoints into _finish (timer tick,
        # Continue button, Abort button, key bindings). First one wins;
        # subsequent calls are no-ops.
        self._answered = False

    def compose(self) -> ComposeResult:
        title = Text()
        glyph = "✕ " if self._error_class == "quota" else "▲ "
        title_style = RUST if self._error_class == "quota" else COPPER
        title.append(glyph, style=title_style)
        title.append(_TITLES.get(self._error_class, "Stream failed"),
                     style=f"bold {GOLD_HI}")
        title.append(f"   attempt {self._attempt}/{self._max_attempts}",
                     style=TXT_DIM)

        hint = Text(_HINTS.get(self._error_class, _HINTS["unknown"]), style=TXT)

        body = Text()
        body.append("Provider response:\n", style=GOLD_DIM)
        clean = "".join(
            c if c.isprintable() or c in "\n\t " else "·"
            for c in self._error_message
        )
        body.append(clean, style=BRONZE)

        with Vertical(id="rc-shell"):
            yield Static(title, id="rc-title")
            yield Static(hint, id="rc-hint")
            yield Static(body, id="rc-msg")
            yield Static(self._timer_text(), id="rc-timer")
            with Horizontal(id="rc-foot"):
                yield Button("Abort (Esc)", id="rc-abort", variant="error")
                yield Button("Continue (Enter)", id="rc-continue",
                             variant="primary")

    def on_mount(self) -> None:
        self._tick_timer = self.set_interval(0.25, self._tick)
        self._last_shown = int(self._remaining + 0.999)
        # Default focus on the safer choice. For permanent errors
        # (quota) that's Abort — waiting won't fix billing. For
        # everything else default to Continue so a stray Enter
        # ack's the retry.
        target_id = "rc-abort" if self._error_class == "quota" else "rc-continue"
        try:
            self.query_one(f"#{target_id}", Button).focus()
        except Exception:
            log.debug("retry-confirm: focus failed", exc_info=True)

    def _timer_text(self) -> Text:
        out = Text()
        if self._error_class == "quota":
            out.append("Waiting won't fix this. ", style=COPPER)
        out.append("Auto-continue in ", style=TXT_DIM)
        out.append(f"{int(self._remaining + 0.999)}s", style=GOLD)
        out.append("  ·  Esc/A aborts, Enter/C continues.", style=TXT_DIM)
        return out

    def _tick(self) -> None:
        if self._answered:
            return
        self._remaining -= 0.25
        if self._remaining <= 0:
            self._finish(True, source="auto")
            return
        cur = int(self._remaining + 0.999)
        if cur != self._last_shown:
            self._last_shown = cur
            try:
                self.query_one("#rc-timer", Static).update(self._timer_text())
            except Exception:
                pass

    # ---- decision paths --------------------------------------------------

    def action_kontinue(self) -> None:
        # Method name 'kontinue' avoids accidental clash with any Textual
        # builtin action_continue. The action is bound to Enter.
        self._finish(True, source="enter")

    def action_abort(self) -> None:
        self._finish(False, source="esc")

    @on(Button.Pressed, "#rc-continue")
    def _on_continue(self, event: Button.Pressed) -> None:
        event.stop()
        self._finish(True, source="continue-button")

    @on(Button.Pressed, "#rc-abort")
    def _on_abort(self, event: Button.Pressed) -> None:
        event.stop()
        self._finish(False, source="abort-button")

    def _finish(self, decision: bool, *, source: str) -> None:
        if self._answered:
            return
        self._answered = True
        log.info(
            "retry-confirm: decision=%s source=%s class=%s attempt=%d",
            decision, source, self._error_class, self._attempt,
        )
        if self._tick_timer is not None:
            try:
                self._tick_timer.stop()
            except Exception:
                pass
            self._tick_timer = None
        if not decision:
            # Abort = exact same path as Esc on the main screen.
            # Dismiss first so the modal disappears, then call the
            # app's cancel action which cancels ``_agent_task``,
            # stops spinners, prints "⏹ stopped by user". One line,
            # no signaling to the agent — cancellation propagates
            # through asyncio's normal channels.
            self.dismiss(False)
            try:
                self.app.action_cancel_agent()
            except Exception:
                log.debug("retry-confirm: action_cancel_agent failed",
                          exc_info=True)
        else:
            # Continue: just close. Agent's backoff sleep is already
            # ticking in the background; it'll resume on its own.
            self.dismiss(True)


__all__ = ["RetryConfirmModal"]
