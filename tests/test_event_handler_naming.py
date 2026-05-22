"""Detect Textual ``_on_<event>`` handler name collisions.

Textual's MessagePump auto-binds methods named ``_on_<event_name>``
(snake_case) to ``events.<EventName>`` regardless of any ``@on(...)``
decorator on the same method. If a developer reuses such a name for
a custom-typed handler (e.g. ``_on_paste`` taking ``Input.Submitted``),
the *real* paste event from the OS still routes to it and the body
crashes the entire app on first paste/drag-drop.

This test scans the in-tree widget/screen modules for ``def _on_<x>``
methods, derives the event class Textual would dispatch, and asserts
the annotated argument type matches. A mismatch is the bug.

Add new exemptions to ``_KNOWN_EVENTS`` only if you are sure the
collision is intentional.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent / "cogitum"

# event_name → expected handler argument type (suffix-based match)
# Pulled from textual.events; only the ones we actually risk colliding
# with are listed. Adding more is cheap; missing one is a silent bug.
_KNOWN_EVENTS: dict[str, tuple[str, ...]] = {
    "paste": ("Paste",),
    "key": ("Key",),
    "click": ("Click",),
    "mount": ("Mount",),
    "unmount": ("Unmount",),
    "focus": ("Focus",),
    "blur": ("Blur",),
    "resize": ("Resize",),
    "show": ("Show",),
    "hide": ("Hide",),
    "load": ("Load",),
    "ready": ("Ready",),
    "idle": ("Idle",),
    "screen_resume": ("ScreenResume",),
    "screen_suspend": ("ScreenSuspend",),
    "enter": ("Enter",),
    "leave": ("Leave",),
}


def _annot_text(annot: ast.expr | None) -> str:
    if annot is None:
        return ""
    return ast.unparse(annot)


def test_no_textual_event_name_collisions() -> None:
    failures: list[str] = []
    for path in ROOT.rglob("*.py"):
        if "/data/" in str(path):
            continue  # skill scripts run in their own contexts
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = node.name
            if not name.startswith("_on_"):
                continue
            event_name = name[len("_on_"):]
            expected = _KNOWN_EVENTS.get(event_name)
            if not expected:
                continue
            args = node.args.args
            # skip ``self``
            param = args[1] if len(args) > 1 else None
            if param is None:
                continue
            annot = _annot_text(param.annotation)
            if not any(annot.endswith(suffix) for suffix in expected):
                rel = path.relative_to(ROOT.parent)
                failures.append(
                    f"{rel}:{node.lineno} def {name}({param.arg}: {annot or '<no annotation>'}) "
                    f"— Textual auto-binds this name to events.{expected[0]}; "
                    f"rename the method or change the type"
                )
    assert not failures, "\n".join(["Event handler naming collisions:", *failures])
