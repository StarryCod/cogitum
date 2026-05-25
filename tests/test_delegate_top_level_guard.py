"""M1 audit: ``_run_delegate_workers`` / ``_run_delegate_experts`` must
catch every non-cancellation exception and return an ``ERROR: …``
string, so a single bad payload can't kill the agent run loop.

The dispatcher classifier (``_result_indicates_error``) then surfaces
that string as an error ToolResultPart, which the parent model can
see and recover from.
"""
from __future__ import annotations

import asyncio

import pytest


def _make_agent():
    from cogitum.core.agent import Agent, AgentConfig

    cfg = AgentConfig(model="mock", platform="cli")
    cfg.tools_enabled = True

    class _Mesh:
        def list_resolved(self):
            return []

        def resolve(self, qid):
            return None

        async def stream(self, req):
            if False:
                yield None  # pragma: no cover

    class _Reg:
        def to_openai(self, tags=None):
            return []

        def names(self):
            return []

        async def execute(self, name, args):  # pragma: no cover
            return ""

    return Agent(mesh=_Mesh(), registry=_Reg(), config=cfg)


# ── Workers ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_workers_catches_run_workers_exception(monkeypatch):
    agent = _make_agent()

    async def boom(*a, **kw):
        raise RuntimeError("worker pool exploded")

    # Patch the import target inside _run_delegate_workers (it does a
    # local ``from .delegate import run_workers, WorkerTask``).
    import cogitum.core.delegate as _del
    monkeypatch.setattr(_del, "run_workers", boom)

    result = await agent._run_delegate_workers('[{"id":"a","goal":"g"}]')
    assert isinstance(result, str)
    assert result.startswith("ERROR:"), result
    assert "delegate_workers" in result.lower() or "worker" in result.lower()
    assert "RuntimeError" in result or "exploded" in result


@pytest.mark.asyncio
async def test_workers_catches_unknown_exception_during_setup(monkeypatch):
    """An exception during task-list construction (bad shape) must also
    be wrapped, not propagated."""
    agent = _make_agent()

    # Force WorkerTask to raise on construction so the task-build loop
    # crashes mid-flight.
    import cogitum.core.delegate as _del

    class BoomTask:
        def __init__(self, **kw):
            raise ValueError("malformed task entry")

    monkeypatch.setattr(_del, "WorkerTask", BoomTask)

    result = await agent._run_delegate_workers('[{"id":"a","goal":"g"}]')
    assert result.startswith("ERROR:"), result
    assert "ValueError" in result or "malformed" in result


@pytest.mark.asyncio
async def test_workers_invalid_json_returns_error_not_raise():
    """Pre-existing ERROR path stays intact (regression guard)."""
    agent = _make_agent()
    result = await agent._run_delegate_workers("{not json")
    assert result.startswith("ERROR:")
    assert "invalid delegate payload" in result.lower()


@pytest.mark.asyncio
async def test_workers_propagates_cancellation(monkeypatch):
    """``asyncio.CancelledError`` MUST propagate so the run loop's own
    cleanup can fire — wrapping it in ERROR: would orphan tasks."""
    agent = _make_agent()

    async def cancelled(*a, **kw):
        raise asyncio.CancelledError()

    import cogitum.core.delegate as _del
    monkeypatch.setattr(_del, "run_workers", cancelled)

    with pytest.raises(asyncio.CancelledError):
        await agent._run_delegate_workers('[{"id":"a","goal":"g"}]')


# ── Experts ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_experts_catches_run_review_exception(monkeypatch):
    agent = _make_agent()

    async def boom(*a, **kw):
        raise RuntimeError("review board crashed")

    import cogitum.core.delegate as _del
    monkeypatch.setattr(_del, "run_expert_review", boom)

    result = await agent._run_delegate_experts('{"content":"x","experts":[]}')
    assert result.startswith("ERROR:"), result
    assert "RuntimeError" in result or "crashed" in result


@pytest.mark.asyncio
async def test_experts_invalid_json_returns_error_not_raise():
    agent = _make_agent()
    result = await agent._run_delegate_experts("{not json")
    assert result.startswith("ERROR:")
    assert "invalid delegate payload" in result.lower()


@pytest.mark.asyncio
async def test_experts_propagates_cancellation(monkeypatch):
    agent = _make_agent()

    async def cancelled(*a, **kw):
        raise asyncio.CancelledError()

    import cogitum.core.delegate as _del
    monkeypatch.setattr(_del, "run_expert_review", cancelled)

    with pytest.raises(asyncio.CancelledError):
        await agent._run_delegate_experts('{"content":"x","experts":[]}')
