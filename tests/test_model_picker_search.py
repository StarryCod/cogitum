"""Tests for ModelPicker search/filter logic."""
from __future__ import annotations

from dataclasses import dataclass
import pytest


def _make_entry(qualified_id: str, model_id: str, display: str = "",
                aliases: tuple = (), caps: tuple = ("text", "tools"),
                ctx: int = 128_000):
    """Build a fake _Entry without spinning up a full mesh."""
    from cogitum.widgets.model_picker import _Entry
    from cogitum.core.llm.capabilities import Capability

    @dataclass
    class _Model:
        id: str
        display: str
        aliases: tuple
        capabilities: object
        context_window: int = 128_000
        max_output_tokens: int = 16_000
        cost_input: float = 0.0
        cost_output: float = 0.0

    @dataclass
    class _Provider:
        id: str

    @dataclass
    class _Resolved:
        qualified_id: str
        provider: object
        model: object

    # Build a Capability flag set
    cap_flag = Capability(0)
    for c in caps:
        member = Capability.__members__.get(c.upper())
        if member is not None:
            cap_flag |= member
    # The picker code calls .to_strings() and `Capability.X in m.capabilities`
    # We need an object that supports both. Use Capability flag directly.
    pid, _, mid = qualified_id.partition("/")
    # Add a to_strings method via attaching to a wrapper
    class _CapWrap:
        def __init__(self, flag):
            self._flag = flag
        def __contains__(self, item):
            return bool(self._flag & item)
        def to_strings(self):
            return [m.name.lower() for m in Capability if m & self._flag]
    return _Entry(_Resolved(
        qualified_id=qualified_id,
        provider=_Provider(id=pid),
        model=_Model(
            id=model_id, display=display, aliases=aliases,
            capabilities=_CapWrap(cap_flag), context_window=ctx,
        ),
    ))


def test_search_haystack_includes_aliases_and_caps():
    e = _make_entry(
        "or/qwen-3-235b", "qwen-3-235b",
        display="Qwen 3 235B",
        aliases=("qwen3-235", "q3"),
        caps=("text", "tools"),
    )
    h = e.search_haystack
    assert "qwen-3-235b" in h
    assert "qwen3-235" in h
    assert "qwen 3 235b" in h
    assert "tools" in h


def test_substring_match_finds_short_query():
    """Search 'qwen' should always find qwen models, even with short query."""
    haystack = _make_entry(
        "or/qwen-3-235b", "qwen-3-235b", "Qwen 3 235B"
    ).search_haystack
    assert "qwen" in haystack


def test_humanize_kept_as_helper_in_setup_flow():
    """Ensure the humanize helper still works for ManageModelsModal."""
    from cogitum.setup_flow import _humanize, _infer_caps
    assert _humanize("llama-3.1-8b") == "Llama 3.1 8B"
    assert _humanize("kr/claude-sonnet-4.5") == "Claude Sonnet 4.5"
    caps = _infer_caps("qwen-3-vl-30b")
    assert "vision" in caps
    caps = _infer_caps("deepseek-r1-distill")
    assert "reasoning" in caps
    caps = _infer_caps("plain-text-model")
    assert caps == ["text", "tools"]
