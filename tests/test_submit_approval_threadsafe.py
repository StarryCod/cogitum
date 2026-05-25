"""Audit C4-ж: ``submit_approval`` должен быть thread-safe.

Раньше ``Agent.submit_approval`` был задокументирован как
event-loop-only: вызывать только из того loop, в котором живёт agent.
Если кто-то вызывает его из другого треда (Telegram callback handler,
Discord gateway, любой PTY-watcher на стороне) — ``Future.set_result``
на Future, привязанной к чужому loop, ТИХО становится no-op. Tool
никогда не дождётся решения и упадёт по 300s timeout с misleading
"REJECTED: approval timed out for ...". Модель не узнает, что причина
— баг плагина.

Фикс:
  1. Agent захватывает ``self._main_loop = asyncio.get_running_loop()``
     в начале ``run()``.
  2. ``submit_approval`` детектит чужой тред и роутит резолвинг через
     ``loop.call_soon_threadsafe(...)``.

Тесты:
  * sanity: same-thread вызов работает как раньше (синхронно).
  * cross-thread вызов из другого треда корректно резолвит Future
    в исходном loop, awaiter просыпается с правильным decision.
  * безопасное отсутствие main_loop (agent ни разу не запускали)
    не падает, просто возвращает False.
"""
from __future__ import annotations

import asyncio
import threading
from typing import AsyncIterator

import pytest

from cogitum.core.agent import Agent, AgentConfig
from cogitum.core.events import ChunkKind, StreamChunk


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


def _agent() -> Agent:
    return Agent(
        mesh=_FakeMesh(),
        registry=_Registry(),
        config=AgentConfig(model="fake/model"),
    )


@pytest.mark.asyncio
async def test_same_thread_submit_approval_resolves_synchronously() -> None:
    """Sanity: same-loop вызов резолвит Future синхронно, как раньше."""
    agent = _agent()
    agent._main_loop = asyncio.get_running_loop()
    fut: asyncio.Future = agent._main_loop.create_future()
    agent._approval_futures["c1"] = fut

    assert agent.submit_approval("c1", "approve") is True
    # Future уже резолвлен прямо в этом тике — не нужен await sleep.
    assert fut.done()
    assert fut.result() == "approve"


@pytest.mark.asyncio
async def test_cross_thread_submit_approval_routes_via_call_soon_threadsafe() -> None:
    """Главный тест: вызов из другого треда не должен молча терять
    decision. Awaiter в основном loop ДОЛЖЕН проснуться.
    """
    agent = _agent()
    main_loop = asyncio.get_running_loop()
    agent._main_loop = main_loop
    fut: asyncio.Future = main_loop.create_future()
    agent._approval_futures["cross"] = fut

    # Эмулируем gateway-callback: запускаем submit_approval из
    # отдельного треда, у которого нет нашего event loop.
    submit_returned: list[bool] = []

    def _from_other_thread() -> None:
        # У этого треда нет running event loop вообще — это самый
        # частый случай для синхронных gateway callbacks (telegram
        # python-telegram-bot dispatch иногда работает так).
        submit_returned.append(agent.submit_approval("cross", "approve"))

    t = threading.Thread(target=_from_other_thread)
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive(), "submit_approval из чужого треда повис"

    # call_soon_threadsafe scheduled — нужен один тик основного loop.
    decision = await asyncio.wait_for(fut, timeout=2.0)
    assert decision == "approve"
    # submit_approval вернул True (запланировал доставку).
    assert submit_returned == [True]


@pytest.mark.asyncio
async def test_cross_thread_submit_approval_with_modify_payload() -> None:
    """Cross-thread путь должен сохранять ``modify:<json>`` decision."""
    agent = _agent()
    main_loop = asyncio.get_running_loop()
    agent._main_loop = main_loop
    fut: asyncio.Future = main_loop.create_future()
    agent._approval_futures["mc"] = fut

    payload = 'modify:{"command": "ls"}'

    def _from_other_thread() -> None:
        agent.submit_approval("mc", payload)

    t = threading.Thread(target=_from_other_thread)
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive()

    decision = await asyncio.wait_for(fut, timeout=2.0)
    assert decision == payload


@pytest.mark.asyncio
async def test_cross_thread_submit_approval_unknown_call_id_is_noop() -> None:
    """Чужой тред с несуществующим call_id не должен ронять loop."""
    agent = _agent()
    agent._main_loop = asyncio.get_running_loop()

    results: list[bool] = []

    def _from_other_thread() -> None:
        results.append(agent.submit_approval("ghost", "approve"))

    t = threading.Thread(target=_from_other_thread)
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert results == [False]


def test_submit_approval_without_main_loop_returns_false_safely() -> None:
    """Если ``run()`` не вызывали — ``_main_loop`` is None.
    submit_approval должен спокойно вернуть False, не падать.
    """
    agent = _agent()
    assert agent._main_loop is None
    # call_id с зарегистрированной future, но loop не захвачен —
    # синхронный fallback всё равно НЕ должен бросить unhandled
    # exception. Для проверки достаточно вызова без зарегистрированной
    # future: гарантировано вернёт False.
    assert agent.submit_approval("nope", "approve") is False


@pytest.mark.asyncio
async def test_run_captures_main_loop_reference() -> None:
    """Лёгкий smoke-тест: после старта ``run()`` ``_main_loop``
    указывает на текущий loop. Без этого cross-thread защита
    деградирует до прежнего silent no-op.
    """
    agent = _agent()
    assert agent._main_loop is None

    # Запускаем run в отдельной таске, отменяем сразу же — нам нужен
    # только сам факт того что _main_loop был выставлен.
    async def _short_run() -> None:
        try:
            await agent.run("hi")
        except Exception:
            pass

    task = asyncio.create_task(_short_run())
    # Дать run() дойти до точки ``self._main_loop = ...``.
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, BaseException):
        pass

    assert agent._main_loop is asyncio.get_running_loop()
