"""
cogitum.core.tool_result_format
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Single source of truth for converting an arbitrary tool return value into
the string the LLM sees as a ``ToolResultPart``.

Originally lived inside ``cogitum.core.agent`` as ``_format_tool_result_for_model``.
Extracted here so subworker paths (``cogitum.core.legion_worker``,
``cogitum.core.delegate``) can reuse the SAME normalisation as the primary
agent loop. Closes audit gaps GAP-10a and GAP-10b: dict/list/None/bytes
results from a Legion or Delegate worker are now formatted consistently with
builtin tools (JSON for dict/list, base64 fallback for binary, ``"(no
output)"`` sentinel for blanks).

The function is idempotent: passing a string back through it is a no-op
(unless the string is empty/whitespace, in which case it becomes
``"(no output)"``).
"""
from __future__ import annotations

import base64
import json
import traceback
from typing import Any


def format_tool_result_for_model(result: Any) -> str:
    """Convert any tool return value into the string the LLM sees.

    Centralises the F6/F7 audit fixes so every tool result hits the
    model with predictable, parseable text:

      * ``None`` and empty/whitespace-only strings → ``"(no output)"``
        so the model doesn't read a blank ToolResultPart as "the tool
        crashed silently" and re-call. Matches the hermes-agent
        convention.
      * ``dict`` / ``list`` → ``json.dumps`` with ``ensure_ascii=False``
        and indent=2. Plain ``str(d)`` would emit a Python repr like
        ``{'a': 1}`` — single quotes break naive JSON parsing on the
        model side.
      * ``bytes`` / ``bytearray`` → decoded as UTF-8 with ``replace``;
        if decoding produces a control-char-heavy string we fall back
        to base64 so binary blobs still travel safely.
      * ``BaseException`` → ``"ERROR: <type>: <msg>"`` plus the
        formatted traceback if available. Tools normally don't raise
        past ``_execute_tool``'s except blocks, but registry returns
        an exception object are not unheard of.
      * Everything else → ``str(result)``.
    """
    if result is None:
        return "(no output)"

    if isinstance(result, str):
        if not result.strip():
            return "(no output)"
        return result

    if isinstance(result, BaseException):
        tb = "".join(
            traceback.format_exception(type(result), result, result.__traceback__)
        ).strip()
        head = f"ERROR: {type(result).__name__}: {result}"
        if tb and tb != head:
            return f"{head}\n{tb}"
        return head

    if isinstance(result, (bytes, bytearray)):
        try:
            decoded = bytes(result).decode("utf-8")
        except UnicodeDecodeError:
            return base64.b64encode(bytes(result)).decode("ascii")
        # If the decoded payload is mostly non-printable, prefer base64
        # so the model isn't fed a wall of \x escapes.
        printable = sum(1 for ch in decoded if ch.isprintable() or ch in "\r\n\t")
        if decoded and printable / len(decoded) < 0.7:
            return base64.b64encode(bytes(result)).decode("ascii")
        return decoded if decoded.strip() else "(no output)"

    if isinstance(result, (dict, list, tuple)):
        try:
            return json.dumps(result, ensure_ascii=False, indent=2, default=str)
        except (TypeError, ValueError):
            return str(result)

    return str(result)


__all__ = ["format_tool_result_for_model"]
