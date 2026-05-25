"""Audit F3: невалидный JSON в аргументах tool_call → ERROR result,
без выполнения тулы.

До фикса:
  * ``openai_compat`` инжектил ``args = {"_raw": "<сырая строка>"}``.
    Тула либо падала с TypeError на неожиданный kwarg, либо (хуже)
    с ``**kwargs`` принимала странный аргумент и выполнялась с
    мусорным состоянием.
  * ``anthropic_native`` делал то же самое.
  * Flush «висячих» pending tool_calls в ``agent.run()`` подставлял
    ``args = {}`` и тула выполнялась с дефолтами (``git status`` без
    аргументов и т.п.).

Теперь поведение:
  * Провайдеры на JSONDecodeError шлют ``tool_call_args=None`` плюс
    человекочитаемое описание в ``tool_call_args_delta``.
  * Агент в ``TOOL_CALL_DONE`` handler пишет id в
    ``self._malformed_tool_call_ids``, а ``args`` оставляет ``{}``
    для wire-shape.
  * ``_execute_tool`` short-circuit'ит на этом id ДО любых approval
    или ``registry.execute``: возвращает ту самую ERROR-строку.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator
from unittest.mock import MagicMock

import pytest

from cogitum.core.agent import Agent, AgentConfig
from cogitum.core.events import (
    ChunkKind,
    StreamChunk,
    ToolCallPart,
)
from cogitum.core.llm.providers.openai_compat import OpenAICompatProvider
from cogitum.core.llm.providers.anthropic_native import AnthropicProvider


# ---------------------------------------------------------------------------
# Provider-level: JSONDecodeError ⇒ tool_call_args=None + delta-error
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self) -> AsyncIterator[str]:
        for ln in self._lines:
            yield ln


def _fake_lease():
    lease = MagicMock()
    lease.tokens_used = 0
    lease.record = MagicMock()
    return lease


@pytest.mark.asyncio
async def test_openai_compat_emits_none_args_and_error_delta_on_invalid_json():
    """Главное условие: на JSONDecodeError ``tool_call_args`` должен
    быть None (а НЕ ``{"_raw": ...}``), а описание ошибки — в
    ``tool_call_args_delta``."""
    import json as _json
    inst = OpenAICompatProvider.__new__(OpenAICompatProvider)
    inst._client = None
    inst.config = MagicMock()

    bad_json = '{"path": "/", "missing_close":'
    lines = [
        "data: " + _json.dumps({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_x",
                        "function": {"name": "ls", "arguments": bad_json},
                    }]
                },
                "finish_reason": None,
            }]
        }),
        "data: " + _json.dumps({
            "choices": [{"delta": {}, "finish_reason": "tool_calls"}]
        }),
        "data: [DONE]",
    ]

    resp = _FakeResp(lines)
    chunks = []
    async for c in inst._parse_sse(resp, _fake_lease()):
        chunks.append(c)

    dones = [c for c in chunks if c.kind == ChunkKind.TOOL_CALL_DONE]
    assert len(dones) == 1
    done = dones[0]
    # F3 главное: нет инжекции _raw.
    assert done.tool_call_args is None
    assert done.tool_call_args_delta is not None
    assert done.tool_call_args_delta.startswith("ERROR: invalid JSON")
    # И preview оригинальной строки доступен для модели.
    assert "missing_close" in done.tool_call_args_delta


@pytest.mark.asyncio
async def test_anthropic_native_emits_none_args_and_error_delta_on_invalid_json():
    import json as _json
    inst = AnthropicProvider.__new__(AnthropicProvider)
    inst._client = None
    inst.config = MagicMock()

    bad_args = '{"q": "hello'  # truncated mid-string
    lines = [
        "data: " + _json.dumps({
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "search"},
        }),
        "data: " + _json.dumps({
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": bad_args},
        }),
        "data: " + _json.dumps({"type": "content_block_stop", "index": 0}),
        "data: " + _json.dumps({"type": "message_stop"}),
    ]
    resp = _FakeResp(lines)
    chunks = []
    async for c in inst._parse_sse(resp, _fake_lease()):
        chunks.append(c)

    dones = [c for c in chunks if c.kind == ChunkKind.TOOL_CALL_DONE]
    assert len(dones) == 1
    done = dones[0]
    assert done.tool_call_args is None
    assert (done.tool_call_args_delta or "").startswith("ERROR: invalid JSON")


# ---------------------------------------------------------------------------
# Agent-level: short-circuit в _execute_tool, не зовём registry
# ---------------------------------------------------------------------------


class _Mesh:
    providers: dict = {}

    def resolve(self, ref):
        return []

    def list_resolved(self):
        return []

    async def stream(self, req):
        if False:
            yield None

    async def aclose(self):
        return None


class _RecordingRegistry:
    """Записывает все вызовы execute, чтобы тест мог утверждать
    ``call_count == 0`` для malformed-call_id."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._tools = {"ls": True}

    def to_openai(self, tags=None):
        return []

    def names(self):
        return list(self._tools)

    async def execute(self, name, args):
        self.calls.append((name, args))
        return "ok"


def _make_agent(registry):
    cfg = AgentConfig(model="x", tools_enabled=True)
    a = Agent.__new__(Agent)
    a.cfg = cfg
    a.registry = registry
    a._approval_queue = None
    a._approval_futures = {}
    a._malformed_tool_call_ids = {}
    a._active_tool_tasks = []
    return a


@pytest.mark.asyncio
async def test_execute_tool_short_circuits_on_malformed_args():
    """``_execute_tool`` должен вернуть ERROR-строку, не вызывая
    registry, если id числится в ``_malformed_tool_call_ids``."""
    registry = _RecordingRegistry()
    agent = _make_agent(registry)

    err_msg = "ERROR: invalid JSON in tool arguments: bad token | preview: {q:"
    agent._malformed_tool_call_ids["call_bad"] = err_msg

    tc = ToolCallPart(id="call_bad", name="ls", arguments={})
    result = await agent._execute_tool(tc, turn=1)

    assert result == err_msg
    assert registry.calls == [], "registry.execute не должен вызываться для malformed args"
    # И map очистился — повторный legitimate call_id с тем же значением
    # должен дойти до registry.
    assert "call_bad" not in agent._malformed_tool_call_ids


@pytest.mark.asyncio
async def test_execute_tool_runs_normally_when_args_clean():
    """Sanity: тот же call_id, но без записи в malformed-map, должен
    дойти до registry.execute."""
    registry = _RecordingRegistry()
    agent = _make_agent(registry)

    tc = ToolCallPart(id="call_ok", name="ls", arguments={"path": "/"})
    result = await agent._execute_tool(tc, turn=1)

    assert result == "ok"
    assert registry.calls == [("ls", {"path": "/"})]


@pytest.mark.asyncio
async def test_malformed_id_consumed_only_once():
    """После short-circuit запись должна быть удалена, чтобы повторный
    легитимный вызов с тем же id (provider replay) исполнился."""
    registry = _RecordingRegistry()
    agent = _make_agent(registry)
    agent._malformed_tool_call_ids["call_z"] = "ERROR: invalid JSON test"

    tc = ToolCallPart(id="call_z", name="ls", arguments={})
    r1 = await agent._execute_tool(tc, turn=1)
    assert r1.startswith("ERROR")
    assert registry.calls == []

    # Второй вызов с тем же id уже без malformed-метки — должен дойти.
    r2 = await agent._execute_tool(tc, turn=1)
    assert r2 == "ok"
    assert registry.calls == [("ls", {})]
