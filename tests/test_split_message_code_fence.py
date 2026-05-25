"""Audit cosmetic 1: split_message must rebalance fences across chunks.

When a long fenced code block crosses Telegram's 4096-char limit,
the previous implementation cut the string at the boundary without
caring about the open fence. Result on the wire:

  chunk[0]: ```python\n<8000 chars of code>\n  <-- no closing fence
  chunk[1]: ...more code\n```\n                  <-- naked content,
                                                       stray ```

Telegram renders the second chunk as plain text with backticks
visible, and the first chunk's "code block" never terminates.

The fix tracks fence open/close state per line; when a chunk
boundary lands inside an open fence, the current chunk gets a
synthetic ``` closer appended and the next chunk is opened with
the same ```<lang> header.
"""
from __future__ import annotations

from cogitum.gateway.tg_formatter import split_message


def test_split_inside_python_fence_rebalances():
    body = "before\n\n```python\n" + ("print('x')\n" * 600) + "```\n\nafter"
    chunks = split_message(body, max_len=2000)
    assert len(chunks) > 1

    for i, chunk in enumerate(chunks):
        # Every chunk must have a balanced number of ``` toggles.
        n = chunk.count("```")
        assert n % 2 == 0, (
            f"chunk {i} has {n} ``` markers (unbalanced):\n{chunk[-200:]}"
        )

    # Continuation chunks open with ```python so highlighting persists.
    for i, chunk in enumerate(chunks[1:], start=1):
        if "print(" in chunk:
            assert chunk.startswith("```python\n"), (
                f"chunk {i} did not re-open with ```python; "
                f"first 80: {chunk[:80]!r}"
            )


def test_split_no_lang_fence_rebalances():
    body = "```\n" + ("X" * 5000) + "\n```\n"
    chunks = split_message(body, max_len=1500)
    assert len(chunks) > 1
    for i, chunk in enumerate(chunks):
        n = chunk.count("```")
        assert n % 2 == 0, (
            f"chunk {i} has {n} ``` markers (unbalanced):\n{chunk[-200:]}"
        )


def test_split_outside_fence_unchanged():
    """Prose-only input must split exactly like the old implementation
    (paragraph boundary first, then line, then hard cut)."""
    body = ("paragraph one.\n\n" * 200) + "tail"
    chunks = split_message(body, max_len=500)
    assert len(chunks) > 1
    # No chunk should contain a ``` it didn't start with.
    for chunk in chunks:
        assert "```" not in chunk


def test_split_multiple_fences_state_resets():
    """Two separate fences in one body — splitter should not leak
    state between them."""
    body = (
        "intro\n\n"
        "```python\nfirst block\n```\n\n"
        + ("filler line\n" * 400)
        + "\n```bash\necho hello\n```\n"
    )
    chunks = split_message(body, max_len=1500)
    for i, chunk in enumerate(chunks):
        n = chunk.count("```")
        assert n % 2 == 0, (
            f"chunk {i} unbalanced ({n} fences):\n{chunk}"
        )


def test_short_message_unchanged():
    body = "hello\n```py\nx = 1\n```\n"
    out = split_message(body, max_len=4096)
    assert out == [body]


def test_chunks_under_max_len():
    body = "```py\n" + ("a = 1\n" * 2000) + "```\n"
    chunks = split_message(body, max_len=2000)
    for i, chunk in enumerate(chunks):
        assert len(chunk) <= 2000, (
            f"chunk {i} exceeded max_len: {len(chunk)} bytes"
        )


def test_continuation_preserves_language_tag():
    """Specifically: language tag from the opening fence carries over
    into the second chunk's reopener."""
    body = "```rust\n" + ("fn x() {}\n" * 1000) + "```"
    chunks = split_message(body, max_len=1500)
    assert len(chunks) >= 2
    # Second chunk should open with ```rust, not ```.
    assert chunks[1].startswith("```rust\n"), chunks[1][:80]
