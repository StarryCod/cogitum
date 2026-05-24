"""Regression tests for the Ctrl+V paste-duplication bug.

Symptom: pasting text into the composer (TUI) inserted it twice. The
event flow was:

  1. User presses Ctrl+V → terminal emits a `events.Paste`.
  2. ComposerArea (subclass of Textual's TextArea) sees it. The
     base TextArea's default handler INSERTS the text into the
     buffer.
  3. The event then bubbles up to Composer.on_paste, which ALSO
     inserts (or collapses long pastes into [Pasted N lines]).
  4. User sees the same text twice.

`event.stop()` on the parent doesn't help — by the time the parent
sees the event, the TextArea has already mutated its buffer.

Fix: ComposerArea defines its own `_on_paste(event)` that calls
`event.prevent_default()` so the default insert is suppressed, but
DOES NOT call `event.stop()` so the event still bubbles to
Composer's handler which performs the real insert.
"""

from __future__ import annotations

import inspect

import pytest

from cogitum.widgets.composer import Composer, ComposerArea


def test_composer_area_has_on_paste_hook() -> None:
    """ComposerArea must define its own paste hook. If this method
    disappears (someone refactors it away), the duplicate-paste bug
    silently returns."""
    assert hasattr(ComposerArea, "_on_paste"), (
        "ComposerArea._on_paste hook missing — paste duplication "
        "regression risk"
    )
    # It must be a coroutine (async def) — the parent class expects
    # the async signature.
    assert inspect.iscoroutinefunction(ComposerArea._on_paste)


def test_composer_area_on_paste_calls_prevent_default() -> None:
    """The hook's body must call event.prevent_default() — that's
    THE thing that stops the double insert. We assert on the source
    string because there's no clean way to inspect the body of an
    async coroutine without running it, and a behavioural test that
    drives a real Textual paste event would need a full app harness.

    The assertion is loose-but-meaningful: any future implementation
    of the same fix has to call prevent_default somewhere. If the
    method exists but doesn't call it, the bug is back."""
    src = inspect.getsource(ComposerArea._on_paste)
    assert "prevent_default" in src, (
        "_on_paste must call event.prevent_default() to stop "
        "TextArea's default insert; otherwise paste duplicates"
    )


def test_composer_area_on_paste_does_not_stop_propagation() -> None:
    """If we ALSO called event.stop(), the event wouldn't bubble to
    Composer.on_paste — the composer wouldn't see the paste at all,
    and short pastes would just disappear. Verify the hook does NOT
    stop the event.

    Walks the function body's AST so we don't get false positives
    from `event.stop()` mentioned in the docstring.
    """
    import ast
    src = inspect.getsource(ComposerArea._on_paste)
    # Dedent so ast.parse handles class-level methods.
    import textwrap
    tree = ast.parse(textwrap.dedent(src))
    func = tree.body[0]
    assert isinstance(func, ast.AsyncFunctionDef)

    has_stop_call = False
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        # Looking for `event.stop()` — Attribute(value=Name('event'), attr='stop')
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "stop"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "event"
        ):
            has_stop_call = True
            break

    assert not has_stop_call, (
        "ComposerArea._on_paste must NOT call event.stop() — the "
        "Composer parent needs to receive the bubbled event so it "
        "can perform the actual insert (collapse-long / insert-short)"
    )


def test_composer_on_paste_handler_still_present() -> None:
    """Composer's own on_paste handler is the one that actually
    decides what to do with the pasted text (collapse vs inline).
    If THAT disappears, paste does nothing at all because the
    ComposerArea hook silenced the default."""
    assert hasattr(Composer, "on_paste"), (
        "Composer.on_paste handler missing — paste would be "
        "fully silenced (TextArea default suppressed AND no "
        "Composer-level handler to take over)"
    )