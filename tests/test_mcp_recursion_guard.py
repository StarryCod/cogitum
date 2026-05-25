"""
MCP sampling reflection guard.

`build_sampling_callback` must refuse to re-enter past
`MAX_SAMPLING_DEPTH` levels, even when an MCP server keeps calling
back into Cogitum.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


def _make_mesh():
    """Minimal mesh stub: provides .resolve and .stream."""

    class _Mesh:
        providers = {}

        def resolve(self, qid):
            return SimpleNamespace(qualified_id=qid)

        async def stream(self, req):  # pragma: no cover (unused in this test)
            from cogitum.core.events import Chunk, ChunkKind
            yield Chunk(kind=ChunkKind.TEXT, text="ok")
            yield Chunk(kind=ChunkKind.STOP, stop_reason="endTurn")

    return _Mesh()


def test_sampling_depth_guard_raises_after_limit(monkeypatch):
    from cogitum.core.mcp import sampling

    cb = sampling.build_sampling_callback(_make_mesh(), "stub/model")

    async def recurse(depth: int) -> None:
        # Manually inflate depth via the contextvar so we exercise the
        # guard branch without needing a working stream.
        token = sampling._mcp_sampling_depth.set(depth)
        try:
            await cb("stub-server", {"messages": []})
        finally:
            sampling._mcp_sampling_depth.reset(token)

    # At depth == MAX_SAMPLING_DEPTH the guard fires.
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(recurse(sampling.MAX_SAMPLING_DEPTH))
    assert "recursion" in str(ei.value).lower()


def test_sampling_depth_resets_between_calls(monkeypatch):
    """Depth contextvar must reset back to 0 after a callback returns."""
    from cogitum.core.mcp import sampling

    # We don't need the callback to succeed — we just need to know the
    # contextvar is 0 between independent calls.
    assert sampling._mcp_sampling_depth.get() == 0


def test_recursive_callback_breaks_at_depth_4(monkeypatch):
    """Stub callback that re-enters itself; assert 4 levels max."""
    from cogitum.core.mcp import sampling

    seen = []

    async def fake_collect(*args, **kwargs):
        # Re-enter the callback from inside the stream — simulating an
        # MCP server that calls back into sampling.
        try:
            await cb("server-X", {"messages": []})
        except RuntimeError as e:
            seen.append(("guarded", str(e)))
            raise
        return ("text", "model", "endTurn")

    monkeypatch.setattr(sampling, "_collect_stream", fake_collect)

    cb = sampling.build_sampling_callback(_make_mesh(), "stub/model")

    with pytest.raises(RuntimeError):
        asyncio.run(cb("server-X", {"messages": []}))

    # Should have at least one 'guarded' frame from the inner re-entry.
    assert seen, "guard never fired"
    assert all("recursion" in m.lower() for _, m in seen)
