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
