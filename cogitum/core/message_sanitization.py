"""Pre-call message sanitization — orphan tool_use/tool_result repair,
role allowlist, thinking-only filter, empty-id pruning.

Why this exists
---------------

The agent's message buffer can drift out of provider-acceptable shape
through several legitimate routes that have nothing to do with bugs:

  1. ``/resume`` of a session whose previous run got cut off after the
     ``assistant(tool_calls=...)`` was committed but BEFORE the
     matching ``tool(tool_results=...)`` landed on disk. Provider
     receives an orphan tool_call with no answer.
  2. Auto-compaction of the older head — if a tool_result's matching
     tool_call lived in the head but the result lives in the
     preserved tail, we end up with the result and no call.
  3. Provider returns an assistant turn whose only payload is
     ``ThinkingPart`` content. Anthropic rejects this on the very
     next replay with HTTP 400 "The final block in an assistant
     message cannot be `thinking`".
  4. Sessions persisted across version boundaries can carry roles
     the current provider doesn't accept.
  5. Tool calls or tool results with empty/None ids — possible after
     stream interruptions or version migrations. These slip through
     pairing logic (an empty id matches nothing) and reach the wire.

Hermes-agent solves this by running a ``sanitize_api_messages`` pass
unconditionally before every LLM call. We adopt the same contract
adapted to our ``Message``/``parts`` model.

Contract
--------

Input: caller's ``list[Message]`` or any iterable.
Output: a NEW list (input list never mutated). The Message and Part
**objects** in the output are shared with the input where unchanged
— only messages we had to repair are rebuilt. Callers must not mutate
the returned objects in place; treat the result as read-only.

  * No orphan ``ToolResultPart`` (would crash OpenAI compat with
    ``"No tool call found for function call output"``).
  * Every ``ToolCallPart`` with a non-empty id has a matching
    ``ToolResultPart`` — missing ones get a stub explaining the
    result was lost. ToolCallParts with empty/None ids cannot be
    answered structurally and are dropped from the assistant turn.
  * No tool-role messages without any ToolResultPart payload.
  * Assistant turns whose only payload is reasoning are dropped
    from the wire copy. Stored history keeps them — only the
    transient sent-to-provider list is cleaned. If two adjacent
    user messages result from the drop, they merge and ALL their
    non-text parts (e.g. images) are preserved.
  * Assistant turns with no real payload at all (parts=[],
    text-only with whitespace) are dropped.
  * Messages with roles outside ``_VALID_API_ROLES`` are removed.

This module is INTENTIONALLY pure. Idempotent: ``sanitize(sanitize(x))
== sanitize(x)``. No I/O, no logging side effects beyond debug-level
breadcrumbs guarded by ``log.isEnabledFor(DEBUG)``.

Every behaviour below has a unit test.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from cogitum.core.events import (
    ContentPart,
    ImagePart,
    Message,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolResultPart,
)


log = logging.getLogger(__name__)


# Roles the OpenAI/Anthropic chat APIs accept. ``function`` and
# ``developer`` are kept for forward-compat with provider extensions
# that use them; we never produce them ourselves but we shouldn't
# silently drop them either. Hermes-agent uses the same set.
_VALID_API_ROLES: frozenset[str] = frozenset(
    {"system", "user", "assistant", "tool", "function", "developer"}
)


# Stub used to fill in a missing tool_result. The text is deliberately
# short and machine-parseable so the model can recognize it and react
# instead of pretending the call never happened.
_STUB_RESULT_TEXT = (
    "[Result unavailable — original tool execution did not finish or "
    "its result was lost during compaction. Re-run the tool if the "
    "data is still needed.]"
)


# Public ───────────────────────────────────────────────────────────────


def sanitize_messages_for_provider(messages: Iterable[Message]) -> list[Message]:
    """Return a sanitized COPY of ``messages`` safe to send to a provider.

    The input list is never mutated. The returned list is fresh, but
    Message/Part objects inside it are shared with the input where
    unchanged — callers must not mutate them in place. Treat the
    return value as read-only.

    Each transformation is implemented as its own pass so callers
    (and tests) can reason about them individually.

    Order matters:
      1. role allowlist  — drop garbage first so later passes operate
         on a clean role distribution
      2. tool pairing    — repair orphan tool_calls / tool_results,
         strip empty-id parts, drop empty tool messages
      3. assistant payload — drop thinking-only and content-empty
         assistant turns; merge users that became adjacent as a
         result, preserving non-text parts
    """
    msgs: list[Message] = list(messages)
    msgs = _drop_invalid_roles(msgs)
    msgs = _repair_tool_pairing(msgs)
    msgs = _drop_empty_assistants_and_merge_users(msgs)
    return msgs


# Pass 1: role allowlist ──────────────────────────────────────────────


def _drop_invalid_roles(messages: list[Message]) -> list[Message]:
    """Drop messages whose role is outside the API allowlist.

    Old sessions on disk can carry roles like ``"system_warning"`` or
    application-internal markers that the provider would reject. These
    have no semantic place in a wire-format request — drop silently.
    """
    out: list[Message] = []
    for msg in messages:
        if msg.role not in _VALID_API_ROLES:
            if log.isEnabledFor(logging.DEBUG):
                log.debug(
                    "sanitizer: dropping message with invalid role %r",
                    msg.role,
                )
            continue
        out.append(msg)
    return out


# Pass 2: tool_call ↔ tool_result pairing ─────────────────────────────


def _repair_tool_pairing(messages: list[Message]) -> list[Message]:
    """Fix orphaned tool_call/tool_result pairs and empty-id parts.

    Strategy is PER-TURN, not global. A "turn" here is one assistant
    message followed by zero or more contiguous tool messages. Each
    turn is the only legitimate scope for matching tool_call ids to
    tool_results — global matching is wrong because the same id can
    legitimately appear in two different turns (e.g. /resume of a
    session whose ids happened to collide, or a producer that recycles
    short ids).

    Per-turn passes:
      1. Strip ToolCallParts with empty/None ids from assistant
         messages (they can't be answered, structurally garbage).
      2. Strip ToolResultParts with empty/None tool_call_ids from tool
         messages and drop tool messages that have no usable parts at
         all (tool with only TextPart, etc.).
      3. For each assistant turn:
           - collect the ids of THIS turn's tool_calls (deduped)
           - walk forward through contiguous tool messages
           - drop ToolResultParts whose id isn't in this turn's call
             set (turn-local orphan)
           - inject one stub tool message at the end of the turn for
             every id in the call set that has no answer

    We never reorder messages or move them across role boundaries.
    Idempotent: a buffer that already passed through this function
    will pass through again unchanged.

       buffer in
       │
       ├─ Pass 2a: strip empty-id parts (per-message)
       │
       └─ Pass 2b: per-turn pairing
            for each [assistant, tool*, tool*, …]:
              ids_called   = {non-empty ids on this assistant}
              ids_answered = {ids on contiguous tools, intersected
                              with ids_called}
              drop turn-local orphans (in tools but not in ids_called)
              append stub tool msg for ids_called − ids_answered
    """
    # Pass 2a: strip empty-id parts and malformed tool messages.
    pruned_msgs: list[Message] = []
    for msg in messages:
        if msg.role == "assistant":
            kept = [
                p for p in msg.parts
                if not (isinstance(p, ToolCallPart) and not p.id)
            ]
            if len(kept) != len(msg.parts):
                if log.isEnabledFor(logging.DEBUG):
                    log.debug(
                        "sanitizer: stripped empty-id tool_call(s) from assistant"
                    )
                pruned_msgs.append(_replace_parts(msg, kept))
                continue
            pruned_msgs.append(msg)
            continue
        if msg.role == "tool":
            kept = [
                p for p in msg.parts
                if isinstance(p, ToolResultPart) and p.tool_call_id
            ]
            if not kept:
                if log.isEnabledFor(logging.DEBUG):
                    log.debug(
                        "sanitizer: dropping malformed tool message "
                        "(no valid ToolResultParts)"
                    )
                continue
            if len(kept) != len(msg.parts):
                pruned_msgs.append(_replace_parts(msg, kept))
                continue
            pruned_msgs.append(msg)
            continue
        pruned_msgs.append(msg)

    # Pass 2b: per-turn pairing.
    out: list[Message] = []
    i = 0
    n = len(pruned_msgs)
    n_free_floating_dropped = 0
    n_orphans_dropped = 0
    n_stubs_injected = 0

    while i < n:
        msg = pruned_msgs[i]

        if msg.role == "tool":
            # Free-floating tool message (no preceding assistant turn
            # owned it — we'd already have consumed it via the inner
            # loop below). Structurally invalid wire shape; drop.
            n_free_floating_dropped += 1
            i += 1
            continue

        if msg.role != "assistant":
            out.append(msg)
            i += 1
            continue

        # Collect THIS assistant turn's tool_call ids, dedup-preserving
        # order so stubs come out in the order the model emitted them.
        tool_calls_in_turn = [
            p for p in msg.parts
            if isinstance(p, ToolCallPart) and p.id
        ]
        if not tool_calls_in_turn:
            out.append(msg)
            i += 1
            continue

        ids_called: set[str] = set()
        ordered_unique_calls: list[ToolCallPart] = []
        for tc in tool_calls_in_turn:
            if tc.id in ids_called:
                continue
            ids_called.add(tc.id)
            ordered_unique_calls.append(tc)

        # Emit the assistant message verbatim. (We don't dedup the
        # tool_calls inside the assistant message itself — the model
        # actually emitted them, and providers tolerate duplicates
        # there. We DO dedup at stub time so a single result block
        # never carries two ToolResultParts with the same id.)
        out.append(msg)
        i += 1

        # Walk forward through contiguous tool messages for this turn.
        ids_answered: set[str] = set()
        while i < n and pruned_msgs[i].role == "tool":
            tool_msg = pruned_msgs[i]
            i += 1
            local_kept: list[ToolResultPart] = []
            for p in tool_msg.parts:
                if not isinstance(p, ToolResultPart):
                    continue
                if p.tool_call_id not in ids_called:
                    n_orphans_dropped += 1
                    continue
                local_kept.append(p)
                ids_answered.add(p.tool_call_id)
            if not local_kept:
                # Whole tool message was turn-local orphans → drop.
                continue
            if len(local_kept) != len(tool_msg.parts):
                out.append(_replace_parts(tool_msg, local_kept))
            else:
                out.append(tool_msg)

        # Inject stubs for any tool_calls in THIS turn that no
        # following tool message answered. Dedup'd by id so we never
        # produce duplicate ToolResultPart ids in the same wire block.
        unanswered = [
            tc for tc in ordered_unique_calls
            if tc.id not in ids_answered
        ]
        if unanswered:
            n_stubs_injected += len(unanswered)
            stub_parts = [
                ToolResultPart(
                    tool_call_id=tc.id,
                    content=_STUB_RESULT_TEXT,
                    is_error=True,
                )
                for tc in unanswered
            ]
            out.append(Message(
                role="tool",
                parts=stub_parts,
                provider="sanitizer",
            ))

    if (
        log.isEnabledFor(logging.DEBUG)
        and (n_free_floating_dropped or n_orphans_dropped or n_stubs_injected)
    ):
        log.debug(
            "sanitizer: tool-pairing repairs — "
            "free_floating=%d orphans=%d stubs=%d",
            n_free_floating_dropped,
            n_orphans_dropped,
            n_stubs_injected,
        )

    return out


# Pass 3: assistant payload sanitization ──────────────────────────────


def _drop_empty_assistants_and_merge_users(
    messages: list[Message],
) -> list[Message]:
    """Drop assistant turns with no useful wire payload.

    See ``_is_empty_assistant`` for the exact predicate.

    If dropping such a turn leaves two adjacent user messages, merge
    them — preserving every non-text part (images etc.) so vision
    flows survive. We ONLY merge users that became adjacent BECAUSE
    we just dropped an assistant; legitimate user→user input is
    untouched.

    A merge that would produce a wire-illegal empty user message
    (parts=[]) drops BOTH messages — keeping ``prev``'s whitespace
    would 400 the provider just as much as the empty merge would.
    """
    out: list[Message] = []
    just_dropped = False
    for msg in messages:
        if _is_empty_assistant(msg):
            if log.isEnabledFor(logging.DEBUG):
                log.debug(
                    "sanitizer: dropping empty/thinking-only assistant turn"
                )
            just_dropped = True
            continue
        if (
            just_dropped
            and msg.role == "user"
            and out
            and out[-1].role == "user"
        ):
            merged = _merge_user_messages(out[-1], msg)
            if merged.parts:
                out[-1] = merged
            else:
                # Both prev and new were empty/whitespace. Producing
                # parts=[] would 400 the provider; keeping prev's
                # whitespace would also 400. Drop both.
                out.pop()
            just_dropped = False
            continue
        out.append(msg)
        just_dropped = False
    return out


def _is_empty_assistant(msg: Message) -> bool:
    """True if this assistant turn carries no useful wire payload.

    Strict whitelist of what counts as payload — anything not on the
    list is NOT payload, so version-migration garbage (a ToolResultPart
    accidentally on an assistant message, a future part type) doesn't
    silently sneak through as "real":

        TextPart      with non-empty stripped text   → payload
        ToolCallPart  with non-empty id              → payload
        ImagePart     (always)                       → payload

    Everything else (ThinkingPart, empty TextPart, ToolCallPart with
    no id, malformed types) does NOT count as payload.

    The whole turn is dropped from the wire copy when this returns
    True. Stored history keeps it; this only affects what reaches
    the provider.
    """
    if msg.role != "assistant":
        return False
    for part in msg.parts:
        if isinstance(part, TextPart):
            if part.text and part.text.strip():
                return False
            continue
        if isinstance(part, ToolCallPart):
            if part.id:
                return False
            continue
        if isinstance(part, ImagePart):
            return False
        # ThinkingPart — explicitly NOT payload (Anthropic 400 risk)
        # Anything else — explicitly NOT payload
    return True


# Helpers ─────────────────────────────────────────────────────────────


def _replace_parts(msg: Message, parts: list[ContentPart]) -> Message:
    """Build a new Message identical to ``msg`` but with new parts.

    Preserves id, timestamp, provider, model so audit logs and TUI
    keep their references stable.
    """
    return Message(
        role=msg.role,
        parts=parts,
        id=msg.id,
        timestamp=msg.timestamp,
        provider=msg.provider,
        model=msg.model,
    )


def _merge_user_messages(prev: Message, new: Message) -> Message:
    """Merge two user messages preserving non-text parts.

    Both prev and new are user-role messages. Text from both is
    concatenated with a blank-line separator; ImagePart and any
    other non-Text/non-Thinking parts from BOTH messages are kept,
    in order (prev's first, then new's). ThinkingPart is internal
    scratch and is never preserved on a merge — users don't emit
    thinking anyway, so this is just a defensive filter.

    The merged message keeps prev's id (so chat-thread continuity
    in the TUI doesn't jump) and takes new's timestamp (so the
    merge is dated by the latest contribution). Note that the
    original interleaving of text and image parts is NOT preserved:
    all non-text parts come first, then a single trailing TextPart
    with the concatenated content.

    If both prev and new contributed nothing (no text, no non-text
    parts, or only whitespace text), the result has parts=[]. That's
    a wire-illegal shape; the caller is responsible for reacting —
    in this module's only call site
    (``_drop_empty_assistants_and_merge_users``) we pop ``prev`` so
    neither whitespace-only message reaches the wire.
    """
    prev_text = prev.text
    new_text = new.text
    if prev_text and new_text:
        merged_text = prev_text + "\n\n" + new_text
    else:
        merged_text = prev_text or new_text

    non_text_parts: list[ContentPart] = []
    for src in (prev, new):
        for part in src.parts:
            if isinstance(part, (TextPart, ThinkingPart)):
                continue
            non_text_parts.append(part)

    merged_parts: list[ContentPart] = list(non_text_parts)
    # Only emit a TextPart if the merged text has actual content.
    # Whitespace-only text is wire-illegal on Anthropic and a waste
    # everywhere else — leave it out so the caller can detect the
    # degenerate "no real payload" case via `not merged.parts`.
    if merged_text and merged_text.strip():
        merged_parts.append(TextPart(text=merged_text))

    return Message(
        role="user",
        parts=merged_parts,
        id=prev.id,
        timestamp=new.timestamp,
        provider=prev.provider,
        model=prev.model,
    )
