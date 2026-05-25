"""Audit F4: tool_use без id в anthropic-native стриме НЕ должен
исчезать после санитайзера.

Сценарий бага: anthropic-compat шим (например, прокси-роутер) шлёт
``content_block_start`` для блока ``tool_use``, но без поля ``id``
(или с пустой строкой). До фикса:

  1. ``active_tool[idx]["id"]`` ставился в None.
  2. Все ``TOOL_CALL_DELTA`` уходили в агент с ``tool_call_id=None``.
  3. ``content_block_stop`` рождал ``TOOL_CALL_DONE`` тоже с None.
  4. Агент создавал ``ToolCallPart(id="")`` через or-fallback.
  5. ``message_sanitization._repair_tool_pairing`` вырезал
     ассистента с пустым tool_call_id, а парный tool_result терял
     якорь и тоже выкидывался — модель никогда не видела результат
     успешно выполненной тулы.

Фикс: при пустом id синтезируем стабильный
``toolu_auto_<idx>_<uuid>``. Все последующие deltas/stop event'ы
получают этот id и санитайзер не имеет повода что-либо дропать.
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator
from unittest.mock import MagicMock

import pytest

from cogitum.core.events import ChunkKind
from cogitum.core.llm.providers.anthropic_native import AnthropicProvider


class _FakeResp:
    """Минимальный httpx-like объект для ``_parse_sse``: только
    ``aiter_lines`` (метод, возвращающий AsyncIterator[str])."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self) -> AsyncIterator[str]:
        for ln in self._lines:
            yield ln


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}"


def _provider() -> AnthropicProvider:
    # Конструктор ожидает ProviderState, но _parse_sse его не трогает
    # — достаточно заглушки, которая хранит только нужные поля.
    inst = AnthropicProvider.__new__(AnthropicProvider)
    inst._client = None
    inst.config = MagicMock()
    return inst


def _fake_lease():
    lease = MagicMock()
    lease.tokens_used = 0
    lease.record = MagicMock()
    return lease


async def _drain(provider: AnthropicProvider, lines: list[str]):
    resp = _FakeResp(lines)
    out = []
    async for chunk in provider._parse_sse(resp, _fake_lease()):
        out.append(chunk)
    return out


@pytest.mark.asyncio
async def test_tool_use_without_id_gets_synthetic_stable_id():
    """Когда content_block_start не содержит id, парсер должен
    синтезировать непустой стабильный id и использовать его на всех
    последующих deltas / на финальном TOOL_CALL_DONE."""
    provider = _provider()
    lines = [
        _sse({"type": "message_start", "message": {"usage": {"input_tokens": 1}}}),
        _sse({
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "name": "ls", "id": ""},
        }),
        _sse({
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"path": "/"}'},
        }),
        _sse({"type": "content_block_stop", "index": 0}),
        _sse({"type": "message_delta", "delta": {"stop_reason": "tool_use"}}),
        _sse({"type": "message_stop"}),
    ]

    chunks = await _drain(provider, lines)
    deltas = [c for c in chunks if c.kind == ChunkKind.TOOL_CALL_DELTA]
    dones = [c for c in chunks if c.kind == ChunkKind.TOOL_CALL_DONE]

    assert deltas, "ожидался хотя бы один TOOL_CALL_DELTA"
    assert dones, "ожидался TOOL_CALL_DONE"

    # Все id непустые и одинаковые.
    delta_ids = {c.tool_call_id for c in deltas}
    done_ids = {c.tool_call_id for c in dones}
    assert delta_ids == done_ids
    only_id = delta_ids.pop()
    assert only_id, "синтетический id не должен быть пустым"
    assert only_id.startswith("toolu_auto_"), only_id

    # Args дошли корректно.
    assert dones[0].tool_call_args == {"path": "/"}


@pytest.mark.asyncio
async def test_tool_use_with_none_id_synthesizes_id():
    provider = _provider()
    lines = [
        _sse({
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "name": "read_file", "id": None},
        }),
        _sse({
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{}'},
        }),
        _sse({"type": "content_block_stop", "index": 0}),
        _sse({"type": "message_stop"}),
    ]
    chunks = await _drain(provider, lines)
    dones = [c for c in chunks if c.kind == ChunkKind.TOOL_CALL_DONE]
    assert dones
    assert dones[0].tool_call_id
    assert dones[0].tool_call_id.startswith("toolu_auto_")


@pytest.mark.asyncio
async def test_tool_use_with_real_id_passthrough():
    """Если провайдер прислал нормальный id — мы его не подменяем."""
    provider = _provider()
    lines = [
        _sse({
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "name": "ls",
                "id": "toolu_01ABC",
            },
        }),
        _sse({
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{}'},
        }),
        _sse({"type": "content_block_stop", "index": 0}),
        _sse({"type": "message_stop"}),
    ]
    chunks = await _drain(provider, lines)
    dones = [c for c in chunks if c.kind == ChunkKind.TOOL_CALL_DONE]
    assert dones[0].tool_call_id == "toolu_01ABC"


@pytest.mark.asyncio
async def test_two_tool_uses_without_id_get_distinct_ids():
    """Два tool_use без id должны получить РАЗНЫЕ синтетические id —
    иначе пара tool_call/tool_result слипнется и санитайзер дедупит
    не то что надо."""
    provider = _provider()
    lines = [
        _sse({
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "name": "ls", "id": ""},
        }),
        _sse({"type": "content_block_stop", "index": 0}),
        _sse({
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "name": "pwd", "id": ""},
        }),
        _sse({"type": "content_block_stop", "index": 1}),
        _sse({"type": "message_stop"}),
    ]
    chunks = await _drain(provider, lines)
    dones = [c for c in chunks if c.kind == ChunkKind.TOOL_CALL_DONE]
    assert len(dones) == 2
    assert dones[0].tool_call_id != dones[1].tool_call_id
    assert all(d.tool_call_id for d in dones)
