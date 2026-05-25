"""Regression test for P0-3 (audit_tools_history.md).

Anthropic streams a thinking block as ``thinking_delta`` chunks
followed by a final ``signature_delta`` carrying the cryptographic
signature the API needs to verify the model's reasoning on the next
turn. The signature arrives in its own chunk with empty thinking
text. The agent assembles the streaming chunks into a single
``ThinkingPart``.

Old behaviour: ``ThinkingPart.signature`` was overwritten on every
incoming THINKING chunk. Once a text-only chunk landed (or any chunk
with ``thinking_signature=None``), the signature got nulled. The
downstream ``normalize_messages_anthropic`` only emits the
``thinking`` content block when ``signature`` is non-empty — so a
nulled signature meant the entire thinking block was silently
dropped, breaking multi-turn reasoning.

The fix preserves the signature once we've seen one and only updates
it when a new non-empty signature arrives.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from cogitum.core.agent import Agent, AgentConfig
from cogitum.core.events import (
    ChunkKind,
    Message,
    StreamChunk,
    TextPart,
    ThinkingPart,
    Usage,
)
from cogitum.core.llm.events_helpers import normalize_messages_anthropic


# ──────────────────────────────────────────────────────────────────
# Mesh stub: replays a fixed chunk sequence so we can pin exactly
# how the agent merges thinking deltas + signature_delta into one
# ThinkingPart.
# ──────────────────────────────────────────────────────────────────

class _ScriptedMesh:
    def __init__(self, chunks: list[StreamChunk]) -> None:
        self._chunks = chunks
        self.providers: dict = {}

    def resolve(self, ref: str):  # pragma: no cover
        return []

    def list_resolved(self):  # pragma: no cover
        return []

    async def stream(self, req) -> AsyncIterator[StreamChunk]:
        for c in self._chunks:
            yield c

    async def aclose(self) -> None:  # pragma: no cover
        return None


class _Registry:
    def to_openai(self, tags=None):  # pragma: no cover
        return []

    def names(self):  # pragma: no cover
        return []


# ──────────────────────────────────────────────────────────────────
# Direct unit tests on normalize_messages_anthropic
# ──────────────────────────────────────────────────────────────────

def test_thinking_with_signature_serialises_to_anthropic_block() -> None:
    """A ThinkingPart that survived stream-merge with its signature
    must appear as a ``thinking`` content block in the Anthropic
    request body."""
    msg = Message(
        role="assistant",
        parts=[
            ThinkingPart(text="step one\nstep two", signature="sig-abc-123"),
            TextPart(text="final answer"),
        ],
    )
    _, wire = normalize_messages_anthropic([msg])

    assert len(wire) == 1
    blocks = wire[0]["content"]
    thinking_blocks = [b for b in blocks if b["type"] == "thinking"]
    assert len(thinking_blocks) == 1
    tb = thinking_blocks[0]
    assert tb["thinking"] == "step one\nstep two"
    assert tb["signature"] == "sig-abc-123"


def test_thinking_without_signature_is_dropped_from_wire() -> None:
    """Anthropic rejects ``thinking`` blocks without a signature, so
    the wire converter omits them. This pins the contract — and
    explains why losing the signature in stream merge is fatal."""
    msg = Message(
        role="assistant",
        parts=[
            ThinkingPart(text="reasoning here", signature=None),
            TextPart(text="final answer"),
        ],
    )
    _, wire = normalize_messages_anthropic([msg])
    blocks = wire[0]["content"]
    assert all(b["type"] != "thinking" for b in blocks), (
        "thinking block without signature must be omitted from wire"
    )


# ──────────────────────────────────────────────────────────────────
# Agent-level integration test — streaming chunk merge
# ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_streamed_thinking_signature_survives_through_merge() -> None:
    """Replay an Anthropic-style stream:
       1. thinking_delta  (text="part one", signature=None)
       2. thinking_delta  (text="part two", signature=None)
       3. signature_delta (text="",         signature="SIG-XYZ")
       4. text            (final answer)
       5. stop

    After the agent's loop, the stored assistant Message must contain
    a ThinkingPart whose signature is "SIG-XYZ" — not None.
    """
    chunks = [
        StreamChunk(kind=ChunkKind.THINKING, thinking="part one ", thinking_signature=None),
        StreamChunk(kind=ChunkKind.THINKING, thinking="part two", thinking_signature=None),
        # The signature_delta as Anthropic emits it: empty text,
        # signature carried in thinking_signature.
        StreamChunk(kind=ChunkKind.THINKING, thinking="", thinking_signature="SIG-XYZ"),
        StreamChunk(kind=ChunkKind.TEXT, text="final answer"),
        StreamChunk(kind=ChunkKind.USAGE, usage=Usage(input_tokens=10, output_tokens=5)),
        StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn"),
    ]

    agent = Agent(
        mesh=_ScriptedMesh(chunks),
        registry=_Registry(),
        config=AgentConfig(model="anthropic/claude-3-7-sonnet"),
    )

    # Drive the loop with a single user prompt and collect the final
    # assistant message off the agent's history.
    user_msg = Message(role="user", parts=[TextPart(text="hi")])
    queue: asyncio.Queue = asyncio.Queue()

    # Run the agent loop to completion.
    final_messages = await agent.run([user_msg], queue=queue)

    # Find the assistant message — should be the last one.
    asst = next(m for m in reversed(final_messages) if m.role == "assistant")
    thinking_parts = [p for p in asst.parts if isinstance(p, ThinkingPart)]
    assert thinking_parts, "agent must persist the streamed ThinkingPart"
    tp = thinking_parts[0]
    assert tp.text == "part one part two", (
        f"thinking text deltas must be merged in order; got {tp.text!r}"
    )
    assert tp.signature == "SIG-XYZ", (
        f"thinking signature was lost during stream merge — got "
        f"{tp.signature!r}. This is exactly the P0-3 regression: "
        f"normalize_messages_anthropic will silently drop this block "
        f"on the next turn."
    )


@pytest.mark.asyncio
async def test_signature_arriving_before_text_chunk_is_kept() -> None:
    """Edge case: a chunk with signature arrives, then a later
    THINKING chunk arrives with signature=None. Signature must
    persist — None must NOT overwrite a previously set value."""
    chunks = [
        StreamChunk(kind=ChunkKind.THINKING, thinking="alpha ", thinking_signature="SIG-EARLY"),
        # A trailing thinking delta with no signature must not nuke
        # the previously captured signature.
        StreamChunk(kind=ChunkKind.THINKING, thinking="beta", thinking_signature=None),
        StreamChunk(kind=ChunkKind.TEXT, text="done"),
        StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn"),
    ]

    agent = Agent(
        mesh=_ScriptedMesh(chunks),
        registry=_Registry(),
        config=AgentConfig(model="anthropic/claude-3-7-sonnet"),
    )

    user_msg = Message(role="user", parts=[TextPart(text="hi")])
    queue: asyncio.Queue = asyncio.Queue()
    final_messages = await agent.run([user_msg], queue=queue)
    asst = next(m for m in reversed(final_messages) if m.role == "assistant")
    thinking_parts = [p for p in asst.parts if isinstance(p, ThinkingPart)]
    assert thinking_parts
    assert thinking_parts[0].signature == "SIG-EARLY", (
        "later None-signature chunk must not overwrite an earlier "
        "captured signature"
    )
    assert thinking_parts[0].text == "alpha beta"


@pytest.mark.asyncio
async def test_thinking_part_with_signature_round_trips_through_wire() -> None:
    """End-to-end: stream → ThinkingPart → wire body. The signature
    must reach the Anthropic wire format intact."""
    chunks = [
        StreamChunk(kind=ChunkKind.THINKING, thinking="reasoning", thinking_signature=None),
        StreamChunk(kind=ChunkKind.THINKING, thinking="", thinking_signature="WIRE-SIG"),
        StreamChunk(kind=ChunkKind.TEXT, text="ok"),
        StreamChunk(kind=ChunkKind.STOP, stop_reason="end_turn"),
    ]
    agent = Agent(
        mesh=_ScriptedMesh(chunks),
        registry=_Registry(),
        config=AgentConfig(model="anthropic/claude-3-7-sonnet"),
    )
    user_msg = Message(role="user", parts=[TextPart(text="hi")])
    queue: asyncio.Queue = asyncio.Queue()
    final_messages = await agent.run([user_msg], queue=queue)

    # Extract just the assistant message and round-trip it.
    asst = next(m for m in reversed(final_messages) if m.role == "assistant")
    _, wire = normalize_messages_anthropic([asst])
    blocks = wire[0]["content"]
    thinking_blocks = [b for b in blocks if b["type"] == "thinking"]
    assert thinking_blocks, (
        "thinking block was dropped on wire conversion — the signature "
        "didn't survive stream merge"
    )
    assert thinking_blocks[0]["signature"] == "WIRE-SIG"
    assert thinking_blocks[0]["thinking"] == "reasoning"
