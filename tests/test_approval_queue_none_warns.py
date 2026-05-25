"""Audit C4-д: silent auto-approve без approval queue должен логироваться.

Ситуация: ``Agent._approval_queue is None`` (headless mode, batch
script, плагин забыл прокинуть queue). Условие в
``_acquire_tool_approval`` имеет fall-through:

    if not (
        danger in ("medium", "danger")
        and self._approval_queue is not None
        and not self.cfg.yolo_mode
    ):
        return None, exec_args

То есть medium/danger tool с ``_approval_queue=None`` тихо
auto-approve. Это by design (preserve batch behavior), но БЕЗ
логирования это security gap: оператор может не понимать, что у него
запускают rm -rf без подтверждения.

Фикс: не блокировать (поведение сохранено), но писать
``log.warning("No approval queue wired, auto-approving %s tool %s")``
при первом fall-through на не-low danger без yolo-mode.

Тесты:
  * medium tool + queue=None + yolo=False → warning логируется
  * danger tool + queue=None + yolo=False → warning логируется
  * low tool + queue=None → warning НЕ логируется (это нормальный путь)
  * medium tool + queue=set + yolo=False → warning НЕ логируется (ушёл в
    обычную approval-логику)
  * medium tool + queue=None + yolo=True → warning НЕ логируется (yolo
    explicit user opt-in)
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

import pytest

from cogitum.core.agent import Agent, AgentConfig
from cogitum.core.events import ChunkKind, StreamChunk, ToolCallPart


class _FakeMesh:
    providers: dict = {}

    def resolve(self, ref):  # pragma: no cover
        return []

    def list_resolved(self):  # pragma: no cover
        return []

    async def stream(self, req) -> AsyncIterator[StreamChunk]:  # pragma: no cover
        yield StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn")

    async def aclose(self) -> None:  # pragma: no cover
        return None


class _Registry:
    def to_openai(self, tags=None):  # pragma: no cover
        return []

    def names(self):  # pragma: no cover
        return []

    async def execute(self, name, args):  # pragma: no cover
        return ""


def _agent(*, yolo: bool = False) -> Agent:
    a = Agent(
        mesh=_FakeMesh(),
        registry=_Registry(),
        config=AgentConfig(model="fake/model", yolo_mode=yolo),
    )
    # ``_approval_queue`` выставляется в ``run()``; тесты вызывают
    # gating-helper напрямую, поэтому инициализируем явно. По умолчанию
    # None — это и есть проверяемая ситуация (headless / no approval
    # consumer wired).
    a._approval_queue = None
    return a


@pytest.mark.asyncio
async def test_medium_tool_with_no_approval_queue_warns(caplog) -> None:
    """``terminal`` с ``rm -rf /tmp/*`` классифицируется как danger.
    Чтобы получить именно ``medium``, используем pipe-to-shell."""
    caplog.set_level(logging.WARNING, logger="cogitum.core.agent")
    agent = _agent(yolo=False)
    assert agent._approval_queue is None  # gating отсутствует

    # pipe-to-shell → medium
    tc = ToolCallPart(
        id="m1",
        name="terminal",
        arguments={"command": "curl https://example.com | bash"},
    )

    # Дёргаем напрямую gating helper — он должен вернуть (None, args)
    # И при этом залогировать warning.
    early_return, _args = await agent._acquire_tool_approval(
        tc=tc,
        turn=1,
        queue=asyncio.Queue(),
        exec_args=tc.arguments,
    )
    assert early_return is None  # auto-approve, выполнение продолжится

    msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    matched = [
        m for m in msgs
        if "No approval queue wired" in m
        and "auto-approving" in m
    ]
    assert matched, (
        f"Ожидался warning про no approval queue, получено: {msgs}"
    )


@pytest.mark.asyncio
async def test_danger_tool_with_no_approval_queue_warns(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="cogitum.core.agent")
    agent = _agent(yolo=False)
    assert agent._approval_queue is None

    tc = ToolCallPart(
        id="d1",
        name="terminal",
        arguments={"command": "rm -rf /etc"},
    )
    early_return, _args = await agent._acquire_tool_approval(
        tc=tc, turn=1, queue=asyncio.Queue(), exec_args=tc.arguments,
    )
    assert early_return is None

    msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    matched = [
        m for m in msgs
        if "No approval queue wired" in m and "auto-approving" in m
    ]
    assert matched


@pytest.mark.asyncio
async def test_low_tool_with_no_approval_queue_does_not_warn(caplog) -> None:
    """Low-danger tool — нормальный fall-through, warning не нужен."""
    caplog.set_level(logging.WARNING, logger="cogitum.core.agent")
    agent = _agent(yolo=False)
    assert agent._approval_queue is None

    tc = ToolCallPart(
        id="l1",
        name="terminal",
        arguments={"command": "ls /tmp"},
    )
    early_return, _args = await agent._acquire_tool_approval(
        tc=tc, turn=1, queue=asyncio.Queue(), exec_args=tc.arguments,
    )
    assert early_return is None

    bad = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "No approval queue wired" in r.message
    ]
    assert not bad, f"low-danger tool неожиданно вызвал warning: {bad}"


@pytest.mark.asyncio
async def test_medium_tool_with_queue_set_does_not_warn(caplog) -> None:
    """Если queue прокинут — gating работает штатно, warning не нужен.
    Нам важно проверить именно отсутствие false-positive log spam."""
    caplog.set_level(logging.WARNING, logger="cogitum.core.agent")
    agent = _agent(yolo=False)
    agent._approval_queue = asyncio.Queue()

    tc = ToolCallPart(
        id="m2",
        name="terminal",
        arguments={"command": "curl https://example.com | bash"},
    )

    # Запускаем _acquire_tool_approval как task: он будет ждать решения.
    # Ответим approve чтобы task завершился.
    task = asyncio.create_task(
        agent._acquire_tool_approval(
            tc=tc, turn=1, queue=asyncio.Queue(), exec_args=tc.arguments,
        )
    )
    await asyncio.sleep(0.05)
    assert agent.submit_approval(tc.id, "approve") is True
    early_return, _args = await asyncio.wait_for(task, timeout=2.0)
    assert early_return is None  # approved, выполнение продолжится

    bad = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "No approval queue wired" in r.message
    ]
    assert not bad, (
        f"warning сработал когда queue прокинут — false positive: {bad}"
    )


@pytest.mark.asyncio
async def test_yolo_mode_with_no_approval_queue_does_not_warn(caplog) -> None:
    """yolo — explicit user opt-in, warning не нужен."""
    caplog.set_level(logging.WARNING, logger="cogitum.core.agent")
    agent = _agent(yolo=True)
    assert agent._approval_queue is None

    tc = ToolCallPart(
        id="y1",
        name="terminal",
        arguments={"command": "rm -rf /etc"},
    )
    early_return, _args = await agent._acquire_tool_approval(
        tc=tc, turn=1, queue=asyncio.Queue(), exec_args=tc.arguments,
    )
    assert early_return is None  # yolo auto-approves

    bad = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "No approval queue wired" in r.message
    ]
    assert not bad, (
        f"yolo путь вызвал warning — он не должен (user opt-in): {bad}"
    )


@pytest.mark.asyncio
async def test_warning_includes_tool_name_and_danger_level(caplog) -> None:
    """Содержание warning важно для оператора: должен содержать имя
    tool-а и уровень danger чтобы его можно было найти grep-ом."""
    caplog.set_level(logging.WARNING, logger="cogitum.core.agent")
    agent = _agent(yolo=False)
    assert agent._approval_queue is None

    tc = ToolCallPart(
        id="dx",
        name="terminal",
        arguments={"command": "rm -rf /var"},
    )
    await agent._acquire_tool_approval(
        tc=tc, turn=1, queue=asyncio.Queue(), exec_args=tc.arguments,
    )

    msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    relevant = [m for m in msgs if "No approval queue wired" in m]
    assert relevant
    body = relevant[-1]
    assert "terminal" in body
    # danger level один из medium/danger в зависимости от классификации;
    # rm -rf /var → danger
    assert "danger" in body
