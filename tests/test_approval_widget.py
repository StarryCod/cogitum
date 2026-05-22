"""Tests for ApprovalWidget — focus, key bindings, decision dispatch."""
from __future__ import annotations

import pytest

from textual.app import App
from textual.widgets import Static

from cogitum.widgets.approval import ApprovalWidget


class _HostApp(App):
    """Minimal host that mounts an ApprovalWidget on first compose."""

    def __init__(self, tool_name: str, args: dict, danger: str) -> None:
        super().__init__()
        self._tool = tool_name
        self._args = args
        self._danger = danger
        self.last_decision: str | None = None
        self.last_call_id: str | None = None

    def compose(self):
        yield Static("feed", id="feed")

    async def on_mount(self) -> None:
        widget = ApprovalWidget(
            tool_name=self._tool,
            arguments=self._args,
            call_id="call-123",
            danger_level=self._danger,
        )
        await self.mount(widget)

    def on_approval_widget_decided(self, msg: ApprovalWidget.Decided) -> None:
        self.last_decision = msg.decision
        self.last_call_id = msg.call_id


@pytest.mark.asyncio
async def test_widget_takes_focus_on_mount():
    app = _HostApp("terminal", {"command": "echo hi"}, "medium")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        widget = app.query(ApprovalWidget).first()
        assert widget.has_focus, "approval widget must auto-focus so kbd works"


@pytest.mark.asyncio
async def test_arrow_down_then_enter_rejects():
    app = _HostApp("terminal", {"command": "rm -rf /"}, "danger")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert app.last_decision == "reject"
    assert app.last_call_id == "call-123"


@pytest.mark.asyncio
async def test_enter_on_default_approves():
    app = _HostApp("write_file", {"path": "/tmp/x", "content": "hi"}, "low")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert app.last_decision == "approve"


@pytest.mark.asyncio
async def test_quick_y_approves():
    app = _HostApp("terminal", {"command": "ls"}, "medium")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
    assert app.last_decision == "approve"


@pytest.mark.asyncio
async def test_quick_n_rejects():
    app = _HostApp("terminal", {"command": "ls"}, "medium")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
    assert app.last_decision == "reject"


@pytest.mark.asyncio
async def test_escape_rejects():
    app = _HostApp("terminal", {"command": "ls"}, "medium")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.last_decision == "reject"


@pytest.mark.asyncio
async def test_no_emoji_in_widget_render():
    """40K aesthetic — only geometric runes, no consumer emoji."""
    from cogitum.widgets import approval as _approval

    # Smoke-test the rendered strings the widget will emit by inspecting
    # the source of the module — ensures the legacy emoji are gone for good.
    src = open(_approval.__file__).read()
    for banned in ("✅", "❌", "🔴", "🟡", "✏️"):
        assert banned not in src, \
            f"banned glyph {banned!r} still in approval.py"

    # Live render check: instantiate a widget and verify its describe output
    # contains only ASCII + 40K runes.
    app = _HostApp("terminal", {"command": "ls"}, "medium")
    async with app.run_test():
        widget = app.query(ApprovalWidget).first()
        assert widget is not None
        # Header static text accessible via .render()
        for static in widget.query(Static):
            text = str(static.render())
            for banned in ("✅", "❌", "🔴", "🟡", "✏️"):
                assert banned not in text, \
                    f"banned {banned!r} in static render: {text!r}"


@pytest.mark.asyncio
async def test_widget_survives_markup_in_tool_args():
    """Tool args routinely contain ``[/]``, ``[bg]``, ``[a-z]`` etc.
    The header used to interpolate that text into a Textual markup
    string, which crashed the whole TUI with ``MarkupError: Expected
    markup value (found '/]')`` the moment such an approval popped up.

    This test runs the widget against three known-bad payloads — a
    closing-tag literal, a Windows path with brackets, and a regex
    with character classes — and asserts the widget mounts cleanly.
    No more crash on user-controlled tool-arg shapes."""
    bad_payloads = [
        # Closing-tag literal (the original crash report from the user).
        ("terminal", {"command": "echo [/]"}),
        # Windows path with bracketed segment — common when users
        # paste downloads / project folders (the field that
        # surfaced the bug).
        ("write_file", {"path": r"C:\Users\u\Project [Lermess]\out.py", "content": "x"}),
        # Python regex with character class — would parse as a tag.
        ("edit_file", {"path": r"src/util.py", "old": r"re.compile(r'\[/\]')"}),
        # Background-mode tag inside a description (we previously
        # emitted ``[bg] {cmd}`` raw into markup).
        ("terminal", {"command": "long-build", "mode": "background"}),
    ]
    for tool_name, args in bad_payloads:
        app = _HostApp(tool_name, args, "medium")
        async with app.run_test() as pilot:
            await pilot.pause()
            # If the widget tried to parse markup, run_test would
            # have raised MarkupError before we get here. Querying
            # the rendered text just confirms the widget is alive.
            widget = app.query(ApprovalWidget).first()
            assert widget is not None, f"widget missing for {tool_name} {args!r}"
