"""Shared dispatcher for the ``/codegraph`` (alias ``/cg``) slash command.

The CLI/TUI (``cogitum.app``) and the Telegram gateway both expose the same
sub-command surface (``init|index|query|callers|callees|context|status``).
Putting the parsing + tool-invocation here keeps the two ``_handle_command``
dispatchers tiny and the help text identical across surfaces.

The codegraph builtin tools live in :mod:`cogitum.core.codegraph_tools` and
return either ``dict`` (init/index/query/callers/callees/status) or ``str``
(context/impact). Errors come back as ``{"error": "..."}`` from dicts and as
``"ERROR: ..."`` from strings; both shapes get normalised here into a single
:class:`CodegraphResponse` for the front-end to render.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any


SUBCOMMANDS = (
    "init",
    "index",
    "query",
    "callers",
    "callees",
    "context",
    "status",
)


USAGE = (
    "usage: /codegraph <subcommand> [args]\n"
    "  init     [root]            — bind/create graph at root (default cwd)\n"
    "  index    [root]            — index every supported file\n"
    "  query    <text>            — FTS5 search\n"
    "  callers  <qname>           — list callers of a symbol\n"
    "  callees  <qname>           — list callees of a symbol\n"
    "  context  <qname>           — markdown context bundle\n"
    "  status                     — graph stats\n"
    "alias: /cg"
)


@dataclass
class CodegraphResponse:
    """Outcome of a single ``/codegraph <sub>`` invocation.

    Attributes
    ----------
    sub:        Sub-command that ran (or the raw input on a parse error).
    ok:         False when the dispatcher rejected input or the tool errored.
    text:       Plain-text body. Already formatted (markdown for context,
                bullet list for queries, table for status). Front-ends pass
                this through ``escape_md`` themselves where appropriate; the
                payload itself is NOT pre-escaped.
    is_markdown: True when ``text`` is markdown that should be sent as-is to
                Telegram (the formatter already produced safe MarkdownV2 — we
                just split into chunks). False for short single-line replies
                that the front-end is free to wrap.
    raw:        The underlying tool result (dict or str), for tests.
    """

    sub: str
    ok: bool
    text: str
    is_markdown: bool = False
    raw: Any = None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse(rest: str) -> tuple[str, list[str]]:
    """Split ``rest`` (the part after ``/codegraph``) into (sub, args).

    Empty input returns ``("", [])``. ``shlex.split`` handles quoted args so
    ``/codegraph query "foo bar"`` works the way users expect.
    """

    rest = (rest or "").strip()
    if not rest:
        return "", []
    try:
        parts = shlex.split(rest)
    except ValueError:
        # Unbalanced quotes — fall back to whitespace split so the user
        # gets a usage hint instead of a hard crash.
        parts = rest.split()
    if not parts:
        return "", []
    return parts[0].lower(), parts[1:]


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def _fmt_node(item: dict) -> str:
    """One-line bullet for a node summary dict from codegraph_tools."""

    qn = item.get("qualified_name") or item.get("name") or "?"
    kind = item.get("kind") or "?"
    file = item.get("file") or "?"
    line = item.get("line") or 0
    return f"- `{qn}` ({kind}) — {file}:{line}"


def _fmt_query(result: dict) -> str:
    n = result.get("count", 0)
    q = result.get("query", "")
    rows = result.get("results", [])
    head = f"FTS results for `{q}` — {n} hit(s)"
    if not rows:
        return head + "\n_No matches._"
    lines = [head]
    for row in rows:
        lines.append(_fmt_node(row))
    return "\n".join(lines)


def _fmt_callers_callees(result: dict, label: str) -> str:
    n = result.get("count", 0)
    qn = result.get("qualified_name", "")
    rows = result.get("results", [])
    head = f"{label} of `{qn}` — {n}"
    if not rows:
        return head + "\n_None._"
    lines = [head]
    for row in rows:
        lines.append(_fmt_node(row))
    return "\n".join(lines)


def _fmt_status(result: dict) -> str:
    files = result.get("files", 0)
    nodes = result.get("nodes", 0)
    edges = result.get("edges", 0)
    indexed = result.get("indexed", False)
    last = result.get("last_indexed") or "—"
    db = result.get("db", "?")
    root = result.get("root", "?")
    indexed_glyph = "✓" if indexed else "✗"
    return (
        "CodeGraph status\n"
        f"  root        : {root}\n"
        f"  db          : {db}\n"
        f"  indexed     : {indexed_glyph}\n"
        f"  files       : {files}\n"
        f"  nodes       : {nodes}\n"
        f"  edges       : {edges}\n"
        f"  last_indexed: {last}"
    )


def _fmt_init(result: dict) -> str:
    return (
        f"CodeGraph initialized\n"
        f"  root: {result.get('root', '?')}\n"
        f"  db  : {result.get('db', '?')}\n"
        f"  indexed: {'yes' if result.get('indexed') else 'no'}"
    )


def _fmt_index(result: dict) -> str:
    return (
        f"CodeGraph indexed\n"
        f"  root      : {result.get('root', '?')}\n"
        f"  files     : {result.get('files', 0)}\n"
        f"  nodes     : {result.get('nodes', 0)}\n"
        f"  edges     : {result.get('edges', 0)}\n"
        f"  imports   : {result.get('imports', 0)}\n"
        f"  workers   : {result.get('workers', 1)}\n"
        f"  resolved  : {result.get('resolved', 0)}\n"
        f"  remaining : {result.get('still_unresolved', 0)}\n"
        f"  time      : {result.get('time', 0)}s"
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def dispatch(rest: str) -> CodegraphResponse:
    """Parse ``rest`` and invoke the matching codegraph builtin tool.

    Returns a :class:`CodegraphResponse`. Never raises — every failure
    mode (unknown sub, missing arg, importable but failing tool) lands as
    ``ok=False`` with a human-readable ``text``.

    Imports the tools module lazily so:
      1. The core agent process doesn't pay the import cost when the user
         never touches ``/codegraph``.
      2. When Phase 3.2 hasn't landed yet (or the tree-sitter wheel is
         missing on a stripped-down install), we fall back to a clear
         ``ImportError`` message instead of crashing the whole gateway.
    """

    sub, args = parse(rest)
    if not sub:
        return CodegraphResponse(sub="", ok=False, text=USAGE)

    if sub not in SUBCOMMANDS:
        return CodegraphResponse(
            sub=sub,
            ok=False,
            text=f"unknown subcommand: {sub!r}\n{USAGE}",
        )

    # Lazy import — keeps codegraph optional at gateway start time.
    try:
        from cogitum.core import codegraph_tools as ct
    except ImportError as e:  # pragma: no cover - defensive
        return CodegraphResponse(
            sub=sub,
            ok=False,
            text=f"✕ codegraph not available: {e}",
        )

    try:
        if sub == "init":
            root = args[0] if args else "."
            res = ct.codegraph_init(root=root)
            if isinstance(res, dict) and "error" in res:
                return CodegraphResponse(sub, False, f"✕ codegraph error: {res['error']}", raw=res)
            return CodegraphResponse(sub, True, _fmt_init(res), raw=res)

        if sub == "index":
            root = args[0] if args else "."
            res = ct.codegraph_index(root=root)
            if isinstance(res, dict) and "error" in res:
                return CodegraphResponse(sub, False, f"✕ codegraph error: {res['error']}", raw=res)
            return CodegraphResponse(sub, True, _fmt_index(res), raw=res)

        if sub == "query":
            if not args:
                return CodegraphResponse(sub, False, "usage: /codegraph query <text>")
            q = " ".join(args)
            res = ct.codegraph_query(query=q)
            if isinstance(res, dict) and "error" in res:
                return CodegraphResponse(sub, False, f"✕ codegraph error: {res['error']}", raw=res)
            return CodegraphResponse(sub, True, _fmt_query(res), raw=res)

        if sub == "callers":
            if not args:
                return CodegraphResponse(sub, False, "usage: /codegraph callers <qname>")
            qn = args[0]
            res = ct.codegraph_callers(qualified_name=qn)
            if isinstance(res, dict) and "error" in res:
                return CodegraphResponse(sub, False, f"✕ codegraph error: {res['error']}", raw=res)
            return CodegraphResponse(sub, True, _fmt_callers_callees(res, "Callers"), raw=res)

        if sub == "callees":
            if not args:
                return CodegraphResponse(sub, False, "usage: /codegraph callees <qname>")
            qn = args[0]
            res = ct.codegraph_callees(qualified_name=qn)
            if isinstance(res, dict) and "error" in res:
                return CodegraphResponse(sub, False, f"✕ codegraph error: {res['error']}", raw=res)
            return CodegraphResponse(sub, True, _fmt_callers_callees(res, "Callees"), raw=res)

        if sub == "context":
            if not args:
                return CodegraphResponse(sub, False, "usage: /codegraph context <qname>")
            qn = args[0]
            res = ct.codegraph_context(qualified_name=qn)
            if isinstance(res, str) and res.startswith("ERROR:"):
                return CodegraphResponse(sub, False, f"✕ codegraph error: {res[6:].strip()}", raw=res)
            return CodegraphResponse(sub, True, str(res), is_markdown=True, raw=res)

        if sub == "status":
            root = args[0] if args else "."
            res = ct.codegraph_status(root=root)
            if isinstance(res, dict) and "error" in res:
                return CodegraphResponse(sub, False, f"✕ codegraph error: {res['error']}", raw=res)
            return CodegraphResponse(sub, True, _fmt_status(res), raw=res)
    except Exception as e:  # pragma: no cover - defensive
        return CodegraphResponse(
            sub=sub, ok=False, text=f"✕ codegraph error: {type(e).__name__}: {e}",
        )

    # Unreachable — SUBCOMMANDS guard above.
    return CodegraphResponse(sub=sub, ok=False, text=USAGE)


__all__ = [
    "CodegraphResponse",
    "SUBCOMMANDS",
    "USAGE",
    "dispatch",
    "parse",
]
