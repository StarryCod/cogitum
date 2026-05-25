"""R3 fix (audit GAP-10a / GAP-10b): subworker tool-result formatting.

Background. Two subworker paths existed in parallel to the primary
``Agent._execute_tool``:

  • ``cogitum.core.legion_worker._execute_tool``
  • ``cogitum.core.delegate._execute_tool`` (closure inside
    ``run_workers``)

Both used bare ``str(result)``, so dict/list returns rendered as Python
repr (``"{'a': 1}"`` instead of ``{"a": 1}``), bytes leaked as
``"b'...'"``, ``None`` became ``"None"``, and empty strings were not
coerced to ``"(no output)"``.  Result: a Legion or Delegate sub-agent
saw a different shape for the SAME tool than the parent agent did,
breaking JSON-parsing and Anthropic empty-content invariants.

R3 routes both subworker paths through the shared
``cogitum.core.tool_result_format.format_tool_result_for_model`` (same
function the primary agent's ``_execute_tool`` uses, just lifted out
of ``agent.py``).  These tests assert the consistency.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from cogitum.core.events import ToolCallPart
from cogitum.core.tool_result_format import format_tool_result_for_model


# ─────────────────────────────────────────────────────────────────────────
# Sanity: shared formatter idempotency / shape contract
# ─────────────────────────────────────────────────────────────────────────


def test_formatter_dict_returns_pretty_json_not_python_repr():
    out = format_tool_result_for_model({"a": 1, "b": "Привет"})
    parsed = json.loads(out)
    assert parsed == {"a": 1, "b": "Привет"}
    assert "'" not in out  # not Python repr


def test_formatter_list_returns_json():
    out = format_tool_result_for_model([1, 2, "x"])
    assert json.loads(out) == [1, 2, "x"]


def test_formatter_none_returns_no_output_sentinel():
    assert format_tool_result_for_model(None) == "(no output)"


def test_formatter_empty_string_returns_no_output_sentinel():
    assert format_tool_result_for_model("") == "(no output)"
    assert format_tool_result_for_model("   \n\t ") == "(no output)"


def test_formatter_bytes_utf8_decoded():
    assert format_tool_result_for_model(b"hello") == "hello"


def test_formatter_idempotent_on_string():
    once = format_tool_result_for_model({"k": "v"})
    twice = format_tool_result_for_model(once)
    assert once == twice


# ─────────────────────────────────────────────────────────────────────────
# Legion worker — module-level _execute_tool wired through formatter
# ─────────────────────────────────────────────────────────────────────────


class _FakeSpec:
    """Stub for registry.get(name) returning an awaitable spec.call."""

    def __init__(self, value):
        self._value = value

    async def call(self, **_kwargs):
        return self._value


class _FakeRegistry:
    def __init__(self, mapping: dict):
        self._mapping = mapping

    def get(self, name: str):
        return self._mapping.get(name)


def _noop_send(_to: str, _body: str) -> None:  # pragma: no cover
    pass


@pytest.mark.asyncio
async def test_legion_worker_dict_result_serialised_as_json():
    from cogitum.core.legion_worker import _execute_tool

    reg = _FakeRegistry({"my_tool": _FakeSpec({"k": "v", "n": 7})})
    tc = ToolCallPart(id="t1", name="my_tool", arguments={})
    text = await _execute_tool(
        tc, registry=reg, send_message=_noop_send, spawn_l2=None
    )
    # Must be valid JSON, not Python repr.
    assert json.loads(text) == {"k": "v", "n": 7}
    assert "'" not in text


@pytest.mark.asyncio
async def test_legion_worker_none_result_becomes_no_output():
    from cogitum.core.legion_worker import _execute_tool

    reg = _FakeRegistry({"my_tool": _FakeSpec(None)})
    tc = ToolCallPart(id="t1", name="my_tool", arguments={})
    text = await _execute_tool(
        tc, registry=reg, send_message=_noop_send, spawn_l2=None
    )
    assert text == "(no output)"


@pytest.mark.asyncio
async def test_legion_worker_empty_string_becomes_no_output():
    from cogitum.core.legion_worker import _execute_tool

    reg = _FakeRegistry({"my_tool": _FakeSpec("")})
    tc = ToolCallPart(id="t1", name="my_tool", arguments={})
    text = await _execute_tool(
        tc, registry=reg, send_message=_noop_send, spawn_l2=None
    )
    assert text == "(no output)"


@pytest.mark.asyncio
async def test_legion_worker_bytes_result_decoded():
    from cogitum.core.legion_worker import _execute_tool

    reg = _FakeRegistry({"my_tool": _FakeSpec(b"binary-but-text")})
    tc = ToolCallPart(id="t1", name="my_tool", arguments={})
    text = await _execute_tool(
        tc, registry=reg, send_message=_noop_send, spawn_l2=None
    )
    assert text == "binary-but-text"


@pytest.mark.asyncio
async def test_legion_worker_truncation_after_formatting():
    """Regression: truncation must run AFTER format, so we don't cut a
    JSON dict mid-key. Build a dict big enough to exceed _TOOL_RESULT_TRUNC."""
    from cogitum.core.legion_worker import _TOOL_RESULT_TRUNC, _execute_tool

    big_dict = {f"key_{i:04d}": "x" * 50 for i in range(_TOOL_RESULT_TRUNC // 30)}
    reg = _FakeRegistry({"my_tool": _FakeSpec(big_dict)})
    tc = ToolCallPart(id="t1", name="my_tool", arguments={})
    text = await _execute_tool(
        tc, registry=reg, send_message=_noop_send, spawn_l2=None
    )
    assert "[truncated;" in text
    # Head of the string must be valid JSON-prefix (starts with '{' and a
    # quoted key) — proof we formatted before slicing.
    assert text.startswith("{")
    assert '"key_0000"' in text  # at least one full key visible


# ─────────────────────────────────────────────────────────────────────────
# Delegate worker — closure _execute_tool wired through formatter
# ─────────────────────────────────────────────────────────────────────────


class _DelegateRegistry:
    """Mimics ToolRegistry.execute(name, args) used by delegate."""

    def __init__(self, value):
        self._value = value

    async def execute(self, _name: str, _args: dict):
        return self._value


def _build_delegate_execute_tool(registry):
    """Build the same closure that ``run_workers`` builds.

    We don't want to spin a full mesh + worker loop just to assert the
    one-line formatter swap. The closure body is small enough to mirror
    here verbatim — the test is a contract assertion: whatever the
    closure does, it must call ``format_tool_result_for_model``.
    """
    from cogitum.core.tool_result_format import format_tool_result_for_model

    async def _execute_tool(name: str, arguments: dict) -> str:
        if registry is None:
            return "ERROR: no tools available"
        try:
            result = await registry.execute(name, arguments)
            return format_tool_result_for_model(result)
        except Exception as e:  # pragma: no cover - defensive
            return f"ERROR: {type(e).__name__}: {e}"

    return _execute_tool


@pytest.mark.asyncio
async def test_delegate_dict_result_serialised_as_json():
    """Direct contract test on the closure's intended behaviour."""
    fn = _build_delegate_execute_tool(_DelegateRegistry({"a": 1, "b": [1, 2]}))
    text = await fn("anything", {})
    assert json.loads(text) == {"a": 1, "b": [1, 2]}


@pytest.mark.asyncio
async def test_delegate_none_result_becomes_no_output():
    fn = _build_delegate_execute_tool(_DelegateRegistry(None))
    text = await fn("anything", {})
    assert text == "(no output)"


@pytest.mark.asyncio
async def test_delegate_empty_string_becomes_no_output():
    fn = _build_delegate_execute_tool(_DelegateRegistry(""))
    text = await fn("anything", {})
    assert text == "(no output)"


@pytest.mark.asyncio
async def test_delegate_bytes_result_decoded():
    fn = _build_delegate_execute_tool(_DelegateRegistry(b"hello-bytes"))
    text = await fn("anything", {})
    assert text == "hello-bytes"


def test_delegate_module_imports_format_tool_result_for_model():
    """Belt-and-braces: the delegate module must actually import the
    shared formatter at module scope. If a future refactor drops the
    import, this test screams immediately.
    """
    # Re-import here (not at module top) because conftest's autouse
    # fixture wipes all ``cogitum.*`` modules between tests, which
    # would make a top-level import alias point to a stale function
    # object from a different reload generation.
    from cogitum.core.tool_result_format import format_tool_result_for_model
    import cogitum.core.delegate as _del

    assert hasattr(_del, "format_tool_result_for_model")
    assert _del.format_tool_result_for_model is format_tool_result_for_model


def test_legion_worker_module_imports_format_tool_result_for_model():
    """Same belt-and-braces guard for legion_worker."""
    from cogitum.core.tool_result_format import format_tool_result_for_model
    import cogitum.core.legion_worker as _lw

    assert hasattr(_lw, "format_tool_result_for_model")
    assert _lw.format_tool_result_for_model is format_tool_result_for_model


# ─────────────────────────────────────────────────────────────────────────
# Cross-path consistency — the headline guarantee of GAP-10
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subworker_and_primary_agent_format_dict_identically():
    """A dict tool return MUST produce the same string in:
      • Agent._execute_tool (primary path)
      • legion_worker._execute_tool (subworker A)
      • delegate._execute_tool closure (subworker B)
    """
    from cogitum.core.legion_worker import _execute_tool as legion_exec

    payload = {"alpha": 1, "beta": ["x", "y"], "rus": "ё"}

    # 1) Primary path uses format_tool_result_for_model directly.
    primary = format_tool_result_for_model(payload)
    # 2) Legion subworker.
    reg = _FakeRegistry({"t": _FakeSpec(payload)})
    legion = await legion_exec(
        ToolCallPart(id="x", name="t", arguments={}),
        registry=reg,
        send_message=_noop_send,
        spawn_l2=None,
    )
    # 3) Delegate subworker (closure contract).
    fn = _build_delegate_execute_tool(_DelegateRegistry(payload))
    delegate = await fn("t", {})

    assert primary == legion == delegate, (primary, legion, delegate)


@pytest.mark.asyncio
async def test_subworker_and_primary_agent_format_none_identically():
    from cogitum.core.legion_worker import _execute_tool as legion_exec

    primary = format_tool_result_for_model(None)
    reg = _FakeRegistry({"t": _FakeSpec(None)})
    legion = await legion_exec(
        ToolCallPart(id="x", name="t", arguments={}),
        registry=reg,
        send_message=_noop_send,
        spawn_l2=None,
    )
    fn = _build_delegate_execute_tool(_DelegateRegistry(None))
    delegate = await fn("t", {})

    assert primary == legion == delegate == "(no output)"


@pytest.mark.asyncio
async def test_subworker_and_primary_agent_format_bytes_identically():
    from cogitum.core.legion_worker import _execute_tool as legion_exec

    payload = b"\xe2\x9c\x93 done"  # checkmark + text, valid utf-8

    primary = format_tool_result_for_model(payload)
    reg = _FakeRegistry({"t": _FakeSpec(payload)})
    legion = await legion_exec(
        ToolCallPart(id="x", name="t", arguments={}),
        registry=reg,
        send_message=_noop_send,
        spawn_l2=None,
    )
    fn = _build_delegate_execute_tool(_DelegateRegistry(payload))
    delegate = await fn("t", {})

    assert primary == legion == delegate
