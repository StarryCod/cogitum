"""Audit F16: после эмиссии ``TOOL_CALL_DONE`` буфер ``tool_buffers``
в ``openai_compat`` должен полностью очищаться.

Сценарий бага: некоторые OpenAI-compat провайдеры (vllm с
auto-detection, anthropic-compat shim) шлют ``finish_reason`` дважды
— один раз в последнем ``delta`` chunk-е с ``tool_calls``, и
повторно в follow-up пустом chunk-е. Без очистки буфера второй
``finish_reason`` re-emit'ит каждый ``TOOL_CALL_DONE`` ещё раз.

Последствия:
  * двойные ``ToolCallPart`` с одним id → провайдер на следующий
    запрос отвечает 400 «duplicate tool_use_id»;
  * хуже — ``_dispatch_tool_calls`` запускает ВТОРУЮ копию тулы,
    даблит side-effect (write_file, terminal commands).

Фикс: ``tool_buffers.clear()`` сразу после цикла эмиссии в
``_parse_sse``.
"""
from __future__ import annotations

import json
from typing import AsyncIterator
from unittest.mock import MagicMock

import pytest

from cogitum.core.events import ChunkKind
from cogitum.core.llm.providers.openai_compat import OpenAICompatProvider


class _FakeResp:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self) -> AsyncIterator[str]:
        for ln in self._lines:
            yield ln


def _provider() -> OpenAICompatProvider:
    inst = OpenAICompatProvider.__new__(OpenAICompatProvider)
    inst._client = None
    inst.config = MagicMock()
    return inst


def _fake_lease():
    lease = MagicMock()
    lease.tokens_used = 0
    lease.record = MagicMock()
    return lease


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}"


async def _drain(provider: OpenAICompatProvider, lines: list[str]):
    resp = _FakeResp(lines)
    out = []
    async for chunk in provider._parse_sse(resp, _fake_lease()):
        out.append(chunk)
    return out


@pytest.mark.asyncio
async def test_tool_call_done_emitted_only_once_when_finish_reason_repeats():
    """Главный тест F16: provider шлёт finish_reason ДВАЖДЫ. Раньше
    мы дублировали TOOL_CALL_DONE; теперь второе должно быть no-op."""
    provider = _provider()

    lines = [
        # 1) дельта с tool_call
        _sse({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_abc",
                        "function": {"name": "ls", "arguments": '{"path": "/"}'},
                    }]
                },
                "finish_reason": None,
            }]
        }),
        # 2) chunk с finish_reason — первая эмиссия TOOL_CALL_DONE
        _sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        # 3) ВТОРОЙ chunk с finish_reason — без фикса дублировал бы
        _sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        "data: [DONE]",
    ]

    chunks = await _drain(provider, lines)
    dones = [c for c in chunks if c.kind == ChunkKind.TOOL_CALL_DONE]
    assert len(dones) == 1, f"ожидался ровно один TOOL_CALL_DONE, получено {len(dones)}"
    assert dones[0].tool_call_id == "call_abc"
    assert dones[0].tool_call_args == {"path": "/"}


@pytest.mark.asyncio
async def test_tool_call_done_emitted_once_per_tool_when_finish_in_same_chunk():
    """Провайдер шлёт tool_calls И finish_reason в одном payload, потом
    follow-up пустой chunk с finish_reason. Только один TOOL_CALL_DONE."""
    provider = _provider()

    lines = [
        # Один комбо-chunk: tool_call + finish_reason одновременно.
        _sse({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_combo",
                        "function": {"name": "pwd", "arguments": "{}"},
                    }]
                },
                "finish_reason": "tool_calls",
            }]
        }),
        # Follow-up пустой chunk с тем же finish_reason — частая
        # картина у vllm/anthropic-compat shim.
        _sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        "data: [DONE]",
    ]

    chunks = await _drain(provider, lines)
    dones = [c for c in chunks if c.kind == ChunkKind.TOOL_CALL_DONE]
    assert len(dones) == 1
    assert dones[0].tool_call_id == "call_combo"


@pytest.mark.asyncio
async def test_two_tools_emitted_once_each():
    """Два разных tool_call в батче, два повторных finish_reason — каждый
    эмитится ровно по одному разу."""
    provider = _provider()

    lines = [
        _sse({
            "choices": [{
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_a",
                            "function": {"name": "ls", "arguments": "{}"},
                        },
                        {
                            "index": 1,
                            "id": "call_b",
                            "function": {"name": "pwd", "arguments": "{}"},
                        },
                    ]
                },
                "finish_reason": None,
            }]
        }),
        _sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        _sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        "data: [DONE]",
    ]

    chunks = await _drain(provider, lines)
    dones = [c for c in chunks if c.kind == ChunkKind.TOOL_CALL_DONE]
    assert len(dones) == 2
    ids = {d.tool_call_id for d in dones}
    assert ids == {"call_a", "call_b"}


@pytest.mark.asyncio
async def test_no_tool_calls_no_emissions():
    """Sanity: чистый text-stream без tool_calls — ни одного TOOL_CALL_DONE."""
    provider = _provider()
    lines = [
        _sse({"choices": [{"delta": {"content": "hello"}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        "data: [DONE]",
    ]
    chunks = await _drain(provider, lines)
    dones = [c for c in chunks if c.kind == ChunkKind.TOOL_CALL_DONE]
    assert dones == []
