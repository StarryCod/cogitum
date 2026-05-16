"""Regression tests for the eternal-loading tool_card bug.

The class of bugs covered here:
- Critical [C1]: drain finishes (or times out) with cards still in "running"
  state — they used to stay frozen on the spinner forever. mark_interrupted
  is now called from every drain terminal path so a pending card always
  ends up with a final state.
- Critical [C7]: a preliminary card (created when stream emits
  preliminary=True) loses its full event because the stream errored before
  the full tool_call arrived. Same fix path: any pending card at AgentError
  is force-finalized.

Two layers of coverage:
1. Public API behaviour (state-only, no Textual mounting). We monkeypatch
   ToolCallCard.update so we don't need an active App for these tests.
2. Drain call-site verification (parses cogitum/app.py and asserts the
   sweep is wired into every drain terminal path).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cogitum.widgets.feed import ToolCallCard


@pytest.fixture
def stub_render(monkeypatch):
    """ToolCallCard.update() needs an active Textual App; we don't have one
    in unit tests. The state we care about (_result, _error, _preparing) is
    set BEFORE update() is called, so stubbing update is safe."""
    monkeypatch.setattr(ToolCallCard, "update", lambda self, *a, **k: None)


# ── ToolCallCard public API ────────────────────────────────────────────────


def test_new_card_is_pending(stub_render):
    card = ToolCallCard("terminal", {"command": "ls"}, call_id="abc")
    assert card.is_pending()


def test_set_result_clears_pending(stub_render):
    card = ToolCallCard("terminal", {"command": "ls"}, call_id="abc")
    card.set_result("ok", error=False)
    assert not card.is_pending()


def test_mark_interrupted_finalizes_pending_card(stub_render):
    card = ToolCallCard("terminal", {"command": "ls"}, call_id="abc")
    assert card.is_pending()
    card.mark_interrupted()
    assert not card.is_pending()
    # And it's marked as an error, so the user sees it didn't succeed.
    assert card._error is True


def test_mark_interrupted_idempotent_on_completed_card(stub_render):
    """If a real result arrived just before the sweep, don't overwrite it."""
    card = ToolCallCard("terminal", {"command": "ls"}, call_id="abc")
    card.set_result("real output here", error=False)
    card.mark_interrupted("should not appear")
    # Original result intact.
    assert card._result == "real output here"
    assert card._error is False


def test_mark_interrupted_uses_custom_reason(stub_render):
    card = ToolCallCard("browser", {"action": "open"}, call_id="x")
    card.mark_interrupted("(interrupted: connection lost)")
    assert "(interrupted: connection lost)" in card._result


def test_preliminary_card_can_be_force_finalized(stub_render):
    """C7: preliminary card created without args, then stream dies.
    mark_interrupted must be able to finalize it cleanly."""
    card = ToolCallCard("delegate_task", {}, call_id="prelim", preparing=True)
    assert card.is_pending()
    card.mark_interrupted("(stream error before full tool_call)")
    assert not card.is_pending()


# ── Drain-handler call-site verification ───────────────────────────────────
#
# These read the source of cogitum/app.py and assert that mark_interrupted
# is called from each drain terminal path. They guard against a future
# refactor silently dropping the sweep — which would re-introduce C1/C7.


_APP_PY = Path(__file__).parent.parent / "cogitum" / "app.py"


def _section_text(start_marker: str, end_marker: str | None = None) -> str:
    src = _APP_PY.read_text(encoding="utf-8")
    start = src.index(start_marker)
    if end_marker:
        end = src.index(end_marker, start)
    else:
        end = start + 4000
    return src[start:end]


def test_agent_done_handler_sweeps_pending_cards():
    block = _section_text("isinstance(event, AgentDone)", "isinstance(event, AgentError)")
    assert "mark_interrupted" in block, (
        "AgentDone handler must sweep pending tool cards (regression: C1)"
    )


def test_agent_error_handler_sweeps_pending_cards():
    block = _section_text("isinstance(event, AgentError)", "# Run agent")
    assert "mark_interrupted" in block, (
        "AgentError handler must sweep pending tool cards (regression: C1, C7)"
    )


def test_drain_timeout_fallback_sweeps_pending_cards():
    block = _section_text("asyncio.TimeoutError", "Update history")
    assert "mark_interrupted" in block, (
        "drain timeout fallback must sweep pending tool cards (regression)"
    )
    # And it must NOT touch the private attribute any more (C2).
    assert "card._result" not in block, (
        "drain timeout fallback should use the public is_pending/mark_interrupted "
        "API, not poke at card._result directly (regression: C2)"
    )
