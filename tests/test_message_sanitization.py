"""Tests for cogitum.core.message_sanitization.sanitize_messages_for_provider.

This is the load-bearing safety net for resume/compact-edge-cases that
caused the "model says it didn't get tool feedback" bug. Every branch of
the sanitizer needs a regression lock here — we will be tempted to
"simplify" it later and it must hold.
"""

from __future__ import annotations

from cogitum.core.events import (
    ImagePart,
    Message,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolResultPart,
)
from cogitum.core.message_sanitization import (
    _is_empty_assistant,
    _STUB_RESULT_TEXT,
    _VALID_API_ROLES,
    sanitize_messages_for_provider,
)


# ─── Helpers ─────────────────────────────────────────────────────────


def _user(text: str) -> Message:
    return Message(role="user", parts=[TextPart(text=text)])


def _assistant_text(text: str) -> Message:
    return Message(role="assistant", parts=[TextPart(text=text)])


def _assistant_tool_call(call_id: str, name: str = "terminal", args=None) -> Message:
    return Message(
        role="assistant",
        parts=[ToolCallPart(id=call_id, name=name, arguments=args or {})],
    )


def _tool_result(call_id: str, content: str = "ok") -> Message:
    return Message(
        role="tool",
        parts=[ToolResultPart(tool_call_id=call_id, content=content)],
    )


# ─── Identity / no-op ────────────────────────────────────────────────


def test_empty_input_returns_empty() -> None:
    assert sanitize_messages_for_provider([]) == []


def test_clean_history_unchanged() -> None:
    """A well-formed history must pass through without modification."""
    msgs = [
        _user("hi"),
        _assistant_text("hello"),
        _user("run ls"),
        _assistant_tool_call("c1", "terminal", {"command": "ls"}),
        _tool_result("c1", "file1\nfile2"),
        _assistant_text("done"),
    ]
    out = sanitize_messages_for_provider(msgs)
    assert len(out) == len(msgs)
    for a, b in zip(out, msgs):
        assert a.role == b.role
        assert a.parts == b.parts


def test_input_list_is_not_mutated() -> None:
    """Sanitizer must return a new list and never mutate the caller's."""
    original = [_user("hi"), _assistant_text("hello")]
    snapshot = list(original)
    sanitize_messages_for_provider(original)
    assert original == snapshot


# ─── Pass 1: invalid roles ───────────────────────────────────────────


def test_drops_messages_with_invalid_role() -> None:
    bad = Message(role="system_warning", parts=[TextPart(text="bogus")])  # type: ignore[arg-type]
    msgs = [_user("hi"), bad, _assistant_text("ok")]
    out = sanitize_messages_for_provider(msgs)
    assert len(out) == 2
    assert all(m.role in _VALID_API_ROLES for m in out)


def test_keeps_all_valid_roles() -> None:
    """Every role in the allowlist passes through when given a
    well-formed payload appropriate to that role."""
    msgs = [
        Message(role="system", parts=[TextPart(text="sys")]),
        Message(role="user", parts=[TextPart(text="u")]),
        Message(role="assistant", parts=[
            TextPart(text="a"),
            ToolCallPart(id="c1", name="t", arguments={}),
        ]),
        Message(role="tool", parts=[ToolResultPart(tool_call_id="c1", content="r")]),
    ]
    out = sanitize_messages_for_provider(msgs)
    assert [m.role for m in msgs] == [m.role for m in out]
    assert all(m.role in _VALID_API_ROLES for m in out)


# ─── Pass 2: orphan tool_results ─────────────────────────────────────


def test_orphan_tool_result_is_dropped() -> None:
    """tool_result without a matching assistant tool_call → drop."""
    msgs = [
        _user("hi"),
        # No assistant tool_call for "ghost"
        _tool_result("ghost", "stale output"),
        _user("anything"),
    ]
    out = sanitize_messages_for_provider(msgs)
    roles = [m.role for m in out]
    assert "tool" not in roles, "orphan tool message should be dropped"


def test_partial_orphan_in_multi_part_tool_message_keeps_the_valid_part() -> None:
    """Tool message with two ToolResultParts, one orphan-by-id —
    only the valid one survives. (Generic orphan case; the per-turn
    variant is exercised separately by
    `test_partial_orphan_inside_tool_message_keeps_valid_results_only`
    further below, which proves the same shape works under the
    per-turn pairing rewrite.)"""
    msgs = [
        _assistant_tool_call("c1", "terminal", {"command": "ls"}),
        Message(role="tool", parts=[
            ToolResultPart(tool_call_id="c1", content="ok"),       # valid
            ToolResultPart(tool_call_id="c-orphan", content="stale"),  # orphan
        ]),
    ]
    out = sanitize_messages_for_provider(msgs)
    tool_msgs = [m for m in out if m.role == "tool"]
    assert len(tool_msgs) == 1
    parts = tool_msgs[0].parts
    assert len(parts) == 1
    assert isinstance(parts[0], ToolResultPart)
    assert parts[0].tool_call_id == "c1"


# ─── Pass 2: missing tool_results (the bug we're fixing) ─────────────


def test_unanswered_tool_call_gets_stub_result() -> None:
    """assistant(tool_call) with no following tool(result) → inject
    stub. This is THE fix for the user's "model can't see feedback"
    bug after a /resume of an interrupted session."""
    msgs = [
        _user("run ls"),
        _assistant_tool_call("c1", "terminal", {"command": "ls"}),
        # No tool message follows. User immediately asks something else.
        _user("never mind, what's 2+2?"),
    ]
    out = sanitize_messages_for_provider(msgs)
    # Stub must land BETWEEN the assistant tool_call and the next user
    # message — otherwise role alternation breaks (assistant→user with
    # an orphan tool_call is what providers reject).
    roles = [m.role for m in out]
    assert roles == ["user", "assistant", "tool", "user"]
    stub = out[2]
    assert stub.role == "tool"
    assert len(stub.parts) == 1
    p = stub.parts[0]
    assert isinstance(p, ToolResultPart)
    assert p.tool_call_id == "c1"
    assert p.content == _STUB_RESULT_TEXT
    assert p.is_error is True


def test_multiple_unanswered_tool_calls_get_one_stub_message_with_all_results() -> None:
    """Single assistant message with N unanswered tool_calls → ONE
    stub tool message carrying N stub results, in original order."""
    msgs = [
        Message(
            role="assistant",
            parts=[
                ToolCallPart(id="c1", name="terminal", arguments={}),
                ToolCallPart(id="c2", name="read_file", arguments={"path": "x"}),
                ToolCallPart(id="c3", name="search_files", arguments={}),
            ],
        ),
        _user("continue"),
    ]
    out = sanitize_messages_for_provider(msgs)
    assert [m.role for m in out] == ["assistant", "tool", "user"]
    stub = out[1]
    ids = [p.tool_call_id for p in stub.parts if isinstance(p, ToolResultPart)]
    assert ids == ["c1", "c2", "c3"]
    assert all(
        p.content == _STUB_RESULT_TEXT
        for p in stub.parts
        if isinstance(p, ToolResultPart)
    )


def test_partially_answered_calls_only_get_stubs_for_missing_ids() -> None:
    """assistant has c1+c2, only c1 is answered → stub injected only
    for c2, the c1 answer is preserved."""
    msgs = [
        Message(
            role="assistant",
            parts=[
                ToolCallPart(id="c1", name="t", arguments={}),
                ToolCallPart(id="c2", name="t", arguments={}),
            ],
        ),
        _tool_result("c1", "real result"),
    ]
    out = sanitize_messages_for_provider(msgs)
    assert [m.role for m in out] == ["assistant", "tool", "tool"]
    real = out[1].parts[0]
    stub = out[2].parts[0]
    assert isinstance(real, ToolResultPart)
    assert isinstance(stub, ToolResultPart)
    assert real.tool_call_id == "c1" and real.content == "real result"
    assert stub.tool_call_id == "c2" and stub.content == _STUB_RESULT_TEXT


def test_tool_result_separated_by_user_is_orphan() -> None:
    """Per-turn pairing: assistant(c1) → user → tool(c1).
    The user message ENDS the turn, so c1's "real" result that
    lands after a non-tool message belongs to no turn — it's a
    turn-local orphan and gets dropped. The assistant's c1 also
    ends up unanswered, so a stub is injected immediately after
    the assistant.

    Rationale: provider role-alternation requires assistant→tool
    to be contiguous. Allowing a non-tool message between them
    would shape the conversation as `assistant(tool_call), user,
    tool(result)`, which Anthropic rejects with a 400."""
    msgs = [
        _assistant_tool_call("c1"),
        _user("interrupting"),
        _tool_result("c1", "real"),
    ]
    out = sanitize_messages_for_provider(msgs)
    roles = [m.role for m in out]
    # Expected: [assistant, tool(STUB), user] — orphan tool dropped,
    # stub injected for c1 right after assistant.
    assert roles == ["assistant", "tool", "user"]
    stub = out[1].parts[0]
    assert isinstance(stub, ToolResultPart)
    assert stub.tool_call_id == "c1"
    assert stub.content == _STUB_RESULT_TEXT


# ─── Pass 3: thinking-only assistant ─────────────────────────────────


def test_thinking_only_assistant_is_dropped() -> None:
    msgs = [
        _user("hi"),
        Message(role="assistant", parts=[ThinkingPart(text="hmm")]),
        _user("anyone there?"),
    ]
    out = sanitize_messages_for_provider(msgs)
    # The thinking-only assistant is gone, the two surrounding user
    # messages get merged into one.
    assert [m.role for m in out] == ["user"]
    assert "hi" in out[0].text and "anyone there?" in out[0].text


def test_assistant_with_thinking_AND_text_is_kept() -> None:
    """Thinking + visible text together → not 'thinking-only'.
    Both parts must survive in their original order."""
    msgs = [
        _user("q"),
        Message(role="assistant", parts=[
            ThinkingPart(text="reasoning"),
            TextPart(text="answer"),
        ]),
    ]
    out = sanitize_messages_for_provider(msgs)
    assert len(out) == 2
    assert out[1].role == "assistant"
    types = [type(p) for p in out[1].parts]
    assert types == [ThinkingPart, TextPart]
    assert out[1].parts[0].text == "reasoning"
    assert out[1].parts[1].text == "answer"


def test_assistant_with_thinking_AND_tool_call_is_kept() -> None:
    """Thinking + tool_call → not 'thinking-only'."""
    msgs = [
        _user("q"),
        Message(role="assistant", parts=[
            ThinkingPart(text="reasoning"),
            ToolCallPart(id="c1", name="t", arguments={}),
        ]),
        _tool_result("c1"),
    ]
    out = sanitize_messages_for_provider(msgs)
    assert len(out) == 3
    assert isinstance(out[1].parts[1], ToolCallPart)


def test_whitespace_only_thinking_text_assistant_is_dropped() -> None:
    """End-to-end: an assistant with only whitespace ThinkingPart
    text and nothing else carries no payload — drop from wire."""
    msgs = [
        _user("q"),
        Message(role="assistant", parts=[ThinkingPart(text="   ")]),
        _user("ok?"),
    ]
    out = sanitize_messages_for_provider(msgs)
    # Empty thinking → assistant dropped → users merge.
    assert [m.role for m in out] == ["user"]


# ─── Pass 3: user merging after thinking-only drop ───────────────────


def test_user_messages_merged_only_when_thinking_drop_creates_adjacency() -> None:
    """user → user already in input must NOT be merged — that's the
    caller's choice. We only merge users that became adjacent
    BECAUSE we just removed the assistant between them."""
    msgs = [_user("a"), _user("b")]
    out = sanitize_messages_for_provider(msgs)
    # No drop happened, so two separate users survive.
    assert len(out) == 2


def test_user_merge_preserves_text_with_blank_line_separator() -> None:
    msgs = [
        _user("first"),
        Message(role="assistant", parts=[ThinkingPart(text="ignored")]),
        _user("second"),
    ]
    out = sanitize_messages_for_provider(msgs)
    assert len(out) == 1
    assert out[0].role == "user"
    assert out[0].text == "first\n\nsecond"


# ─── Real-world bug repro: /resume after interrupted run ─────────────


def test_resume_after_max_turns_truncation_is_fixed() -> None:
    """Reproduce the exact symptom: a session got cut off after the
    20th tool batch (assistant emitted tool_calls, the loop exited
    BEFORE the tool message was committed). User /resumes and types a
    new question. Provider sees orphan tool_calls, model claims it
    didn't get tool feedback.

    Sanitizer must turn this into a valid wire shape that the model
    can reason about."""
    history = [
        _user("start the long task"),
        _assistant_text("on it"),
        _assistant_tool_call("c-final", "terminal", {"command": "build"}),
        # NO tool result on disk — exactly what happened to the user.
        _user("are you still there?"),
    ]
    out = sanitize_messages_for_provider(history)
    # The shape provider receives:
    assert [m.role for m in out] == [
        "user", "assistant", "assistant", "tool", "user"
    ]
    stub = out[3].parts[0]
    assert isinstance(stub, ToolResultPart)
    assert stub.tool_call_id == "c-final"
    # Model reads this stub and now KNOWS the previous tool execution
    # was lost — instead of pretending it never happened.
    assert "Result unavailable" in stub.content


# ─── Empty-id handling (issue 1 from review) ─────────────────────────


def test_empty_id_tool_call_is_stripped() -> None:
    """assistant.tool_call with id='' has no answerable contract.
    It must be stripped from the assistant message rather than
    sent to the provider with no result."""
    msgs = [
        Message(
            role="assistant",
            parts=[
                ToolCallPart(id="", name="terminal", arguments={}),
                ToolCallPart(id="c1", name="terminal", arguments={"command": "ls"}),
            ],
        ),
        _tool_result("c1"),
    ]
    out = sanitize_messages_for_provider(msgs)
    asst = next(m for m in out if m.role == "assistant")
    ids = [p.id for p in asst.parts if isinstance(p, ToolCallPart)]
    assert ids == ["c1"]
    # And no stub injected for the empty-id one
    tools = [m for m in out if m.role == "tool"]
    assert len(tools) == 1
    assert all(
        p.tool_call_id == "c1" for p in tools[0].parts
        if isinstance(p, ToolResultPart)
    )


def test_empty_id_tool_result_is_dropped() -> None:
    """tool message with empty tool_call_id has no addressable
    parent. Drop it rather than confusing the provider."""
    msgs = [
        _user("hi"),
        Message(role="tool", parts=[
            ToolResultPart(tool_call_id="", content="floating"),
        ]),
        _user("anything"),
    ]
    out = sanitize_messages_for_provider(msgs)
    assert all(m.role != "tool" for m in out)


def test_tool_message_with_no_tool_result_parts_is_dropped() -> None:
    """A tool message with only TextPart (or empty parts list) is
    a malformed shape no provider accepts."""
    msgs = [
        _user("hi"),
        Message(role="tool", parts=[TextPart(text="weird")]),
        _user("ok"),
    ]
    out = sanitize_messages_for_provider(msgs)
    assert all(m.role != "tool" for m in out)


# ─── User-merge preserves images (issue 2 from review) ───────────────


def test_user_merge_preserves_image_parts() -> None:
    """Vision flow regression: user messages containing ImagePart
    must not lose the image when merged across a thinking-only
    assistant."""
    img = ImagePart(url="https://example.com/cat.png")
    msgs = [
        Message(role="user", parts=[TextPart(text="what is this"), img]),
        Message(role="assistant", parts=[ThinkingPart(text="thinking…")]),
        _user("anyone there?"),
    ]
    out = sanitize_messages_for_provider(msgs)
    assert len(out) == 1
    parts = out[0].parts
    assert any(isinstance(p, ImagePart) and p.url == img.url for p in parts), (
        "image lost during user-merge"
    )
    text_parts = [p for p in parts if isinstance(p, TextPart)]
    assert len(text_parts) == 1
    assert "what is this" in text_parts[0].text
    assert "anyone there?" in text_parts[0].text


# ─── Empty-payload assistant (issue 4 from review) ───────────────────


def test_empty_assistant_no_parts_is_dropped() -> None:
    """assistant with parts=[] is meaningless and must not be sent."""
    msgs = [
        _user("q"),
        Message(role="assistant", parts=[]),
        _user("anyone?"),
    ]
    out = sanitize_messages_for_provider(msgs)
    # Empty assistant dropped → users merged
    assert [m.role for m in out] == ["user"]


def test_empty_assistant_with_only_whitespace_text_is_dropped() -> None:
    msgs = [
        _user("q"),
        Message(role="assistant", parts=[TextPart(text="   \n\t  ")]),
        _user("anyone?"),
    ]
    out = sanitize_messages_for_provider(msgs)
    assert [m.role for m in out] == ["user"]


# ─── Multiple thinking-only assistants between users ─────────────────


def test_multiple_thinking_only_assistants_collapse_users() -> None:
    msgs = [
        _user("a"),
        Message(role="assistant", parts=[ThinkingPart(text="…")]),
        Message(role="assistant", parts=[ThinkingPart(text="…")]),
        _user("b"),
    ]
    out = sanitize_messages_for_provider(msgs)
    assert len(out) == 1
    assert "a" in out[0].text and "b" in out[0].text


# ─── Allowlist matches the source-of-truth set ───────────────────────


def test_developer_role_is_kept() -> None:
    """Forward-compat: 'developer' role from OpenAI Responses API
    must pass through, not get dropped."""
    m = Message(role="developer", parts=[TextPart(text="x")])  # type: ignore[arg-type]
    out = sanitize_messages_for_provider([m])
    assert len(out) == 1
    assert out[0].role == "developer"


def test_function_role_is_kept() -> None:
    """Forward-compat: legacy 'function' role from OpenAI tool-call
    style must pass through."""
    m = Message(role="function", parts=[TextPart(text="x")])  # type: ignore[arg-type]
    out = sanitize_messages_for_provider([m])
    assert len(out) == 1
    assert out[0].role == "function"


# ─── Idempotency ─────────────────────────────────────────────────────


def test_sanitizer_is_idempotent() -> None:
    """sanitize(sanitize(x)) == sanitize(x). Critical for
    correctness when the same buffer flows through retry loops.

    Strong assertion: parts must be equivalent at the structural
    level, not just count. Compares by repr so a regression that
    flipped is_error or swapped text would fail.
    """
    history = [
        _user("hi"),
        _assistant_tool_call("c-final", "terminal", {"command": "build"}),
        _user("are you still there?"),
    ]
    once = sanitize_messages_for_provider(history)
    twice = sanitize_messages_for_provider(once)
    assert [m.role for m in once] == [m.role for m in twice]
    for a, b in zip(once, twice):
        assert len(a.parts) == len(b.parts)
        for pa, pb in zip(a.parts, b.parts):
            assert type(pa) is type(pb)
            assert repr(pa) == repr(pb), (
                f"idempotency violated:\n  first  pass: {pa!r}\n  second pass: {pb!r}"
            )


def test_sanitizer_idempotent_on_clean_input() -> None:
    """Idempotency must hold on already-clean input too. Strong
    structural assertion via repr."""
    msgs = [
        _user("hi"),
        _assistant_tool_call("c1"),
        _tool_result("c1"),
        _assistant_text("done"),
    ]
    once = sanitize_messages_for_provider(msgs)
    twice = sanitize_messages_for_provider(once)
    assert [m.role for m in once] == [m.role for m in twice]
    for a, b in zip(once, twice):
        assert len(a.parts) == len(b.parts)
        for pa, pb in zip(a.parts, b.parts):
            assert type(pa) is type(pb)
            assert repr(pa) == repr(pb)


# ─── _is_empty_assistant predicate edges ─────────────────────────────


def test_is_empty_assistant_for_no_parts() -> None:
    assert _is_empty_assistant(Message(role="assistant", parts=[])) is True


def test_is_empty_assistant_for_whitespace_text() -> None:
    msg = Message(role="assistant", parts=[TextPart(text="   ")])
    assert _is_empty_assistant(msg) is True


def test_is_empty_assistant_false_for_real_text() -> None:
    msg = Message(role="assistant", parts=[TextPart(text="real")])
    assert _is_empty_assistant(msg) is False


def test_is_empty_assistant_false_for_tool_call() -> None:
    msg = Message(role="assistant", parts=[ToolCallPart(id="c1", name="t", arguments={})])
    assert _is_empty_assistant(msg) is False


def test_is_empty_assistant_false_for_image() -> None:
    msg = Message(role="assistant", parts=[ImagePart(url="x")])
    assert _is_empty_assistant(msg) is False


def test_is_empty_assistant_false_for_user_role() -> None:
    msg = Message(role="user", parts=[])
    assert _is_empty_assistant(msg) is False


# ─── Output is a NEW list (mutation-safety contract) ─────────────────


def test_output_is_a_new_list_even_on_no_op() -> None:
    """Caller must be able to rely on `out is not input` always."""
    inp = [_user("hi"), _assistant_text("hello")]
    out = sanitize_messages_for_provider(inp)
    assert out is not inp


# ─── Stub injection across out-of-order tool message runs ────────────


def test_stub_skipped_when_real_result_lands_in_a_later_tool_message() -> None:
    """Real tool messages that come AFTER a stubbed turn already
    answered the call → no double answer."""
    msgs = [
        Message(
            role="assistant",
            parts=[
                ToolCallPart(id="c1", name="t", arguments={}),
                ToolCallPart(id="c2", name="t", arguments={}),
            ],
        ),
        _tool_result("c1", "first"),
        _tool_result("c2", "second"),
    ]
    out = sanitize_messages_for_provider(msgs)
    # No stubs needed
    assert [m.role for m in out] == ["assistant", "tool", "tool"]
    # No "Result unavailable" anywhere
    for m in out:
        for p in m.parts:
            if isinstance(p, ToolResultPart):
                assert _STUB_RESULT_TEXT not in p.content


# ─── Per-turn pairing (round-2 adversarial findings) ─────────────────


def test_same_call_id_reused_in_two_turns_each_needs_its_own_pairing() -> None:
    """Adversarial bug 1: when the SAME tool_call.id appears in two
    different assistant turns, each turn must be checked
    independently. Old global-set logic considered a once-answered
    id forever-answered, so the second turn's tool_call had no stub
    and provider rejected it.

    First turn answered → no stub. Second turn unanswered → stub.
    """
    msgs = [
        _assistant_tool_call("c1"),
        _tool_result("c1", "first"),
        _user("ok do it again"),
        _assistant_tool_call("c1"),
        # No tool result for the second turn's c1.
        _user("are you stuck?"),
    ]
    out = sanitize_messages_for_provider(msgs)
    # Expected: [asst, tool(real), user, asst, tool(STUB), user]
    assert [m.role for m in out] == [
        "assistant", "tool", "user", "assistant", "tool", "user"
    ]
    # First tool message has the real result.
    real = out[1].parts[0]
    assert isinstance(real, ToolResultPart)
    assert real.content == "first"
    # Second tool message is the stub.
    stub = out[4].parts[0]
    assert isinstance(stub, ToolResultPart)
    assert stub.tool_call_id == "c1"
    assert stub.content == _STUB_RESULT_TEXT


def test_duplicate_call_id_in_same_assistant_message_dedups_stubs() -> None:
    """Adversarial bug 2: an assistant emits two tool_calls with
    the same id in ONE message (idempotence weirdness, replay
    artifact, or producer bug). The stub block must NOT carry two
    ToolResultParts with the same id — providers reject duplicate
    ids in a single tool_results block.
    """
    msgs = [
        Message(role="assistant", parts=[
            ToolCallPart(id="c1", name="t", arguments={}),
            ToolCallPart(id="c1", name="t", arguments={}),
        ]),
        _user("anything"),
    ]
    out = sanitize_messages_for_provider(msgs)
    # Tool message must exist with EXACTLY ONE stub for c1.
    tool_msgs = [m for m in out if m.role == "tool"]
    assert len(tool_msgs) == 1
    stubs = [p for p in tool_msgs[0].parts if isinstance(p, ToolResultPart)]
    ids = [s.tool_call_id for s in stubs]
    assert ids == ["c1"], f"expected one stub for c1, got {ids}"


def test_turn_local_orphan_tool_result_is_dropped() -> None:
    """A tool_result whose id doesn't belong to the immediately
    preceding assistant turn is a turn-local orphan (likely from a
    corrupted/merged session) and must be dropped — not allowed to
    cross into a different turn's pairing."""
    msgs = [
        _assistant_tool_call("c1"),
        # Orphan tool result for "c-other" — belongs to no turn here.
        Message(role="tool", parts=[
            ToolResultPart(tool_call_id="c-other", content="floating"),
        ]),
        _tool_result("c1", "real"),
    ]
    out = sanitize_messages_for_provider(msgs)
    # The orphan tool message is dropped; c1's real result survives.
    tool_msgs = [m for m in out if m.role == "tool"]
    assert len(tool_msgs) == 1
    p = tool_msgs[0].parts[0]
    assert isinstance(p, ToolResultPart)
    assert p.tool_call_id == "c1"
    assert p.content == "real"


def test_partial_orphan_inside_tool_message_keeps_valid_results_only() -> None:
    """A tool message contains both a turn-local orphan AND a valid
    result. Only the valid one survives."""
    msgs = [
        _assistant_tool_call("c1"),
        Message(role="tool", parts=[
            ToolResultPart(tool_call_id="c1", content="real"),
            ToolResultPart(tool_call_id="c-orphan", content="ghost"),
        ]),
    ]
    out = sanitize_messages_for_provider(msgs)
    tool_msgs = [m for m in out if m.role == "tool"]
    assert len(tool_msgs) == 1
    parts = tool_msgs[0].parts
    assert len(parts) == 1
    p = parts[0]
    assert isinstance(p, ToolResultPart)
    assert p.tool_call_id == "c1"


# ─── ThinkingPart in user messages ────────────────────────────────────


def test_thinking_in_user_message_is_filtered_during_merge() -> None:
    """ThinkingPart shouldn't appear in user messages but version
    migrations / weird import paths could plant one. _merge_user_messages
    drops ThinkingPart from both sides, keeps text + non-text parts."""
    img = ImagePart(url="x")
    msgs = [
        Message(role="user", parts=[
            TextPart(text="see image"),
            ThinkingPart(text="should not be here"),
            img,
        ]),
        Message(role="assistant", parts=[ThinkingPart(text="…")]),
        Message(role="user", parts=[
            ThinkingPart(text="also not here"),
            TextPart(text="what is it?"),
        ]),
    ]
    out = sanitize_messages_for_provider(msgs)
    assert len(out) == 1
    parts = out[0].parts
    # No ThinkingPart left.
    assert not any(isinstance(p, ThinkingPart) for p in parts)
    # Image survived.
    assert any(isinstance(p, ImagePart) for p in parts)
    # Both texts merged.
    text_parts = [p for p in parts if isinstance(p, TextPart)]
    assert len(text_parts) == 1
    assert "see image" in text_parts[0].text
    assert "what is it?" in text_parts[0].text


def test_empty_user_merge_falls_back_to_dropping_both() -> None:
    """If both user messages are empty/whitespace, the merge would
    produce parts=[]. Instead we drop BOTH (whitespace user would
    also 400 the provider) — never leak an empty-parts user."""
    msgs = [
        Message(role="user", parts=[TextPart(text="   ")]),
        Message(role="assistant", parts=[ThinkingPart(text="x")]),
        Message(role="user", parts=[TextPart(text="\n\n")]),
    ]
    out = sanitize_messages_for_provider(msgs)
    for m in out:
        if m.role == "user":
            assert m.parts != [], "empty-parts user message leaked through"
            # And no whitespace-only user reached the wire either.
            txt = "".join(
                p.text for p in m.parts if isinstance(p, TextPart)
            )
            assert txt.strip(), "whitespace-only user leaked through"