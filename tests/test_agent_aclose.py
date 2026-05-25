"""F4: Agent.aclose() must cancel pending approval futures."""
from __future__ import annotations

import asyncio

import pytest


def _make_agent():
    from cogitum.core.agent import Agent, AgentConfig
    from cogitum.core.tools import ToolRegistry

    class _StubMesh:
        def __init__(self):
            self.providers = {}

        async def aclose(self):
            return None

        def list_resolved(self):
            return []

        def resolve(self, _):
            return None

    return Agent(
        mesh=_StubMesh(),
        registry=ToolRegistry(),
        config=AgentConfig(model="x"),
    )


@pytest.mark.asyncio
async def test_aclose_cancels_pending_futures():
    agent = _make_agent()
    loop = asyncio.get_running_loop()

    # Plant two pending approval futures.
    fut_a = loop.create_future()
    fut_b = loop.create_future()
    agent._approval_futures["call-A"] = fut_a
    agent._approval_futures["call-B"] = fut_b

    await agent.aclose()

    assert fut_a.cancelled(), "future A should be cancelled"
    assert fut_b.cancelled(), "future B should be cancelled"
    assert agent._approval_futures == {}, "futures dict must be cleared"


@pytest.mark.asyncio
async def test_aclose_idempotent():
    agent = _make_agent()
    # No pending futures — must not raise.
    await agent.aclose()
    await agent.aclose()


@pytest.mark.asyncio
async def test_aclose_skips_already_done():
    agent = _make_agent()
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    fut.set_result("approve")
    agent._approval_futures["already-done"] = fut

    await agent.aclose()
    # Done future should NOT be re-cancelled (no exception, no state flip).
    assert fut.done() and not fut.cancelled()
    assert agent._approval_futures == {}


# pytest-asyncio is auto-mode in this repo via pytest plugin discovery,
# but if not we register the asyncio markers explicitly via mark.
# Keep tests using asyncio.run as fallback.
def test_aclose_cancels_pending_futures_sync():
    async def _run():
        agent = _make_agent()
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        agent._approval_futures["call-X"] = fut
        await agent.aclose()
        return fut

    fut = asyncio.run(_run())
    assert fut.cancelled()
