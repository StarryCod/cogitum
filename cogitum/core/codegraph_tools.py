"""
cogitum.core.codegraph_tools
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Builtin Cogitum tools that wrap :mod:`cogitum.codegraph` so the agent can
explore arbitrary projects through a single set of tool calls.

Per-project state lives at ``<root>/.codegraph/graph.db``. We keep a
process-wide cache keyed by resolved root so flipping between projects
during a single agent session doesn't reopen SQLite on every call.

All tools are classified as ``low`` danger by ``classify_danger``: the
write surface is bounded to ``.codegraph/`` and everything else is a
read query. The tools never raise — exceptions surface as
``{'error': '<class>: <message>'}`` to keep the agent loop forgiving.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cogitum.codegraph import CodeGraph
from cogitum.core.tools import tool

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-project graph cache
# ---------------------------------------------------------------------------

# Resolved-root → CodeGraph singleton. SQLite connections aren't
# thread-safe across arbitrary threads, but the agent loop already
# serialises tool calls — the lock here just protects the dict against
# concurrent inserts.
_GRAPHS: dict[str, CodeGraph] = {}
_LOCK = threading.Lock()

_DB_DIR = ".codegraph"
_DB_NAME = "graph.db"


def _resolve_root(root: str | None) -> Path:
    """Resolve the user-supplied root, falling back to cwd."""

    return Path(root or ".").expanduser().resolve()


def _db_path(root: Path) -> Path:
    return root / _DB_DIR / _DB_NAME


def _validate_root(root: Path) -> str | None:
    """Return an error string if *root* isn't a usable project directory."""

    if not root.exists():
        return f"root does not exist: {root}"
    if not root.is_dir():
        return f"root is not a directory: {root}"
    return None


def _get_graph(root: str | None = None) -> CodeGraph:
    """Return the cached CodeGraph for *root*, creating one if needed.

    The DB file is only created if the caller actually writes (index or
    auto-index path). For pure-read queries against a stale path, the
    underlying ``CodeGraph.__init__`` opens the SQLite file on demand —
    which still works because we ``mkdir`` the parent directory here
    even before any data is written.
    """

    resolved = _resolve_root(root)
    key = str(resolved)
    with _LOCK:
        graph = _GRAPHS.get(key)
        if graph is not None:
            return graph
        db_dir = resolved / _DB_DIR
        db_dir.mkdir(parents=True, exist_ok=True)
        cg = CodeGraph(_db_path(resolved))
        cg.init(resolved)
        _GRAPHS[key] = cg
        return cg


def _close_all_graphs() -> None:
    """Close every cached graph and clear the cache.

    Wired into the agent's shutdown / aclose path so the SQLite handles
    don't leak between sessions.
    """

    with _LOCK:
        for cg in _GRAPHS.values():
            try:
                cg.close()
            except Exception:  # pragma: no cover - best-effort
                log.debug("error closing CodeGraph", exc_info=True)
        _GRAPHS.clear()


def _ensure_indexed(cg: CodeGraph, root: Path) -> None:
    """Auto-index if the DB file is missing data.

    The DB file always exists once :func:`_get_graph` runs (we mkdir +
    open), but it may have zero ``files`` rows when no index has run
    yet. Cheap COUNT(*) check; if empty, we run a full index.
    """

    cur = cg.db.connection.execute("SELECT COUNT(*) FROM files")
    n = int(cur.fetchone()[0])
    if n == 0:
        log.warning(
            "codegraph: no index found at %s; running auto-index",
            _db_path(root),
        )
        cg.index_all()


def _node_summary(node: Any, file_path: str | None = None) -> dict[str, Any]:
    """Compact dict representation of a Node for tool output."""

    sig = getattr(node, "signature", None)
    if sig and len(sig) > 200:
        sig = sig[:197] + "..."
    return {
        "name": node.name,
        "qualified_name": node.qualified_name,
        "kind": node.kind,
        "file": file_path,
        "line": node.range.start_line,
        "signature_preview": sig,
    }


def _file_path_for(cg: CodeGraph, file_id: str) -> str | None:
    """Resolve a node's ``file_id`` back to the indexed source path."""

    row = cg.db.connection.execute(
        "SELECT path FROM files WHERE id = ?", (file_id,)
    ).fetchone()
    return row["path"] if row else None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool(tags=["codegraph"])
def codegraph_init(root: str = ".") -> dict:
    """Initialize a CodeGraph store for a project.

    Creates ``<root>/.codegraph/graph.db`` (if missing) and binds a
    CodeGraph instance to ``root``. Returns the resolved root, the DB
    path, and a status flag. Safe to call repeatedly — re-init is a
    no-op once the cached graph exists.

    root: Project directory. Defaults to the current working dir.
    """
    try:
        resolved = _resolve_root(root)
        err = _validate_root(resolved)
        if err:
            return {"error": f"ValueError: {err}"}
        cg = _get_graph(str(resolved))
        return {
            "root": str(resolved),
            "db": str(_db_path(resolved)),
            "status": "initialized",
            "indexed": cg.db.connection.execute(
                "SELECT COUNT(*) FROM files"
            ).fetchone()[0]
            > 0,
        }
    except Exception as e:  # pragma: no cover - defensive
        return {"error": f"{type(e).__name__}: {e}"}


@tool(tags=["codegraph"])
def codegraph_index(
    root: str = ".",
    parallel: bool = True,
    resolve: bool = True,
) -> dict:
    """Index every supported source file under *root*.

    Walks the project, parses with tree-sitter, and persists the graph
    to ``<root>/.codegraph/graph.db``. Synchronous — can take seconds
    on large repos. Re-indexing is idempotent: per-file purge runs
    before fresh inserts so the graph never accumulates stale rows.

    root: Project directory.
    parallel: Use a process pool for extraction.
    resolve: Run the reference resolver after extraction.
    """
    try:
        resolved = _resolve_root(root)
        err = _validate_root(resolved)
        if err:
            return {"error": f"ValueError: {err}"}
        cg = _get_graph(str(resolved))
        stats = cg.index_all(parallel=parallel, resolve=resolve)
        return {
            "db": str(_db_path(resolved)),
            "root": str(resolved),
            **stats,
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@tool(tags=["codegraph"])
def codegraph_query(query: str, root: str = ".", limit: int = 20) -> dict:
    """FTS5 search over indexed nodes (name, qname, signature, doc).

    Returns a list of compact symbol records. Auto-indexes the project
    if no index exists yet (logs a warning).

    query: Search expression (FTS5 syntax — bare words OK).
    root: Project directory.
    limit: Max results to return.
    """
    try:
        resolved = _resolve_root(root)
        err = _validate_root(resolved)
        if err:
            return {"error": f"ValueError: {err}"}
        cg = _get_graph(str(resolved))
        _ensure_indexed(cg, resolved)
        nodes = cg.search_nodes(query, limit=limit)
        results = [_node_summary(n, _file_path_for(cg, n.file_id)) for n in nodes]
        return {"query": query, "count": len(results), "results": results}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@tool(tags=["codegraph"])
def codegraph_callers(
    qualified_name: str, root: str = ".", limit: int = 50
) -> dict:
    """Find callers of *qualified_name*.

    Accepts either a full dotted qname or a bare simple name. Returns
    a list of compact records for the calling symbols, capped at
    ``limit``.

    qualified_name: Symbol to inspect (e.g. ``pkg.mod.func`` or ``func``).
    root: Project directory.
    limit: Max callers to return.
    """
    try:
        resolved = _resolve_root(root)
        err = _validate_root(resolved)
        if err:
            return {"error": f"ValueError: {err}"}
        if not qualified_name:
            return {"error": "ValueError: qualified_name is required"}
        cg = _get_graph(str(resolved))
        _ensure_indexed(cg, resolved)
        callers = cg.get_callers(qualified_name)[:limit]
        results = [
            _node_summary(n, _file_path_for(cg, n.file_id)) for n in callers
        ]
        return {"qualified_name": qualified_name, "count": len(results), "results": results}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@tool(tags=["codegraph"])
def codegraph_callees(
    qualified_name: str, root: str = ".", limit: int = 50
) -> dict:
    """Find callees of *qualified_name* (what this symbol calls).

    Skips unresolved targets. Same shape as ``codegraph_callers``.

    qualified_name: Symbol to inspect.
    root: Project directory.
    limit: Max callees to return.
    """
    try:
        resolved = _resolve_root(root)
        err = _validate_root(resolved)
        if err:
            return {"error": f"ValueError: {err}"}
        if not qualified_name:
            return {"error": "ValueError: qualified_name is required"}
        cg = _get_graph(str(resolved))
        _ensure_indexed(cg, resolved)
        callees = cg.get_callees(qualified_name)[:limit]
        results = [
            _node_summary(n, _file_path_for(cg, n.file_id)) for n in callees
        ]
        return {"qualified_name": qualified_name, "count": len(results), "results": results}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@tool(tags=["codegraph"])
def codegraph_context(
    qualified_name: str,
    root: str = ".",
    max_depth: int = 2,
    include_impact: bool = False,
) -> str:
    """Build a markdown context bundle for *qualified_name*.

    Composes search → node → callers → callees (optionally + impact)
    into a single markdown blob the LLM can consume in one read.
    THIS is the primary symbol-exploration tool — prefer it over
    chaining the per-axis queries.

    qualified_name: Symbol to inspect.
    root: Project directory.
    max_depth: Depth for impact section (when ``include_impact``).
    include_impact: Append an impact-radius summary.
    """
    try:
        resolved = _resolve_root(root)
        err = _validate_root(resolved)
        if err:
            return f"ERROR: {err}"
        if not qualified_name:
            return "ERROR: qualified_name is required"
        cg = _get_graph(str(resolved))
        _ensure_indexed(cg, resolved)

        # Phase 3.1 may land a richer cg.build_context; use it when present.
        builder = getattr(cg, "build_context", None)
        if callable(builder):
            try:
                out = builder(qualified_name)
                if isinstance(out, str):
                    return out
            except Exception:
                # Fall through to local formatter on any builder error.
                log.debug("cg.build_context failed; falling back", exc_info=True)

        # Local fallback formatter — mirrors the canonical layout from
        # codegraph/src/context (search + callers + callees + impact).
        targets = cg.db.find_nodes_by_qualified_name(qualified_name)
        if not targets and "." not in qualified_name:
            targets = cg.db.find_nodes_by_name(qualified_name, limit=10)

        lines: list[str] = [f"# Context: `{qualified_name}`", ""]

        if not targets:
            # Nothing matched — fall back to FTS so the agent at least
            # sees the closest hits instead of an empty context.
            fts = cg.search_nodes(qualified_name, limit=10)
            if not fts:
                lines.append(f"No symbol found for `{qualified_name}`.")
                return "\n".join(lines)
            lines.append("## Closest matches (FTS)")
            for n in fts:
                fp = _file_path_for(cg, n.file_id) or "?"
                lines.append(
                    f"- `{n.qualified_name or n.name}` ({n.kind}) — {fp}:{n.range.start_line}"
                )
            return "\n".join(lines)

        # Symbol section.
        lines.append("## Symbol")
        for tgt in targets:
            fp = _file_path_for(cg, tgt.file_id) or "?"
            lines.append(
                f"- **{tgt.qualified_name or tgt.name}** ({tgt.kind}) — `{fp}:{tgt.range.start_line}`"
            )
            if tgt.signature:
                lines.append(f"  ```\n  {tgt.signature.strip()}\n  ```")
            if tgt.docstring:
                doc = tgt.docstring.strip()
                if len(doc) > 400:
                    doc = doc[:397] + "..."
                lines.append(f"  > {doc}")
        lines.append("")

        # Callers.
        callers = cg.get_callers(qualified_name)
        lines.append(f"## Callers ({len(callers)})")
        if not callers:
            lines.append("_None._")
        else:
            for n in callers[:20]:
                fp = _file_path_for(cg, n.file_id) or "?"
                lines.append(
                    f"- `{n.qualified_name or n.name}` ({n.kind}) — {fp}:{n.range.start_line}"
                )
        lines.append("")

        # Callees.
        callees = cg.get_callees(qualified_name)
        lines.append(f"## Callees ({len(callees)})")
        if not callees:
            lines.append("_None._")
        else:
            for n in callees[:20]:
                fp = _file_path_for(cg, n.file_id) or "?"
                lines.append(
                    f"- `{n.qualified_name or n.name}` ({n.kind}) — {fp}:{n.range.start_line}"
                )
        lines.append("")

        # Impact (optional).
        if include_impact:
            try:
                impact = cg.get_impact_radius(qualified_name, depth=max_depth)
                lines.append(f"## Impact radius (depth ≤ {max_depth})")
                lines.append(
                    f"- direct callers: {len(impact.get('direct_callers', []))}"
                )
                lines.append(
                    f"- transitive callers: {len(impact.get('transitive_callers', []))}"
                )
                lines.append(f"- class uses: {len(impact.get('class_uses', []))}")
                lines.append(
                    f"- extends chain: {len(impact.get('extends_chain', []))}"
                )
                lines.append("")
            except Exception:
                log.debug("impact_radius failed for %s", qualified_name, exc_info=True)

        return "\n".join(lines).rstrip() + "\n"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


@tool(tags=["codegraph"])
def codegraph_impact(
    qualified_name: str, root: str = ".", max_depth: int = 3
) -> str:
    """Markdown summary of the impact radius of *qualified_name*.

    Answers "what breaks if I change this?" — direct callers,
    transitive callers, class uses, and extends-chain descendants.

    qualified_name: Symbol to analyse.
    root: Project directory.
    max_depth: BFS depth cap.
    """
    try:
        resolved = _resolve_root(root)
        err = _validate_root(resolved)
        if err:
            return f"ERROR: {err}"
        if not qualified_name:
            return "ERROR: qualified_name is required"
        cg = _get_graph(str(resolved))
        _ensure_indexed(cg, resolved)
        impact = cg.get_impact_radius(qualified_name, depth=max_depth)

        def _fmt(nodes: list) -> list[str]:
            out: list[str] = []
            for n in nodes[:20]:
                fp = _file_path_for(cg, n.file_id) or "?"
                out.append(
                    f"- `{n.qualified_name or n.name}` ({n.kind}) — {fp}:{n.range.start_line}"
                )
            if len(nodes) > 20:
                out.append(f"- _… {len(nodes) - 20} more_")
            return out

        lines = [
            f"# Impact: `{qualified_name}` (depth ≤ {max_depth})",
            "",
            f"## Direct callers ({len(impact['direct_callers'])})",
            *(_fmt(impact["direct_callers"]) or ["_None._"]),
            "",
            f"## Transitive callers ({len(impact['transitive_callers'])})",
            *(_fmt(impact["transitive_callers"]) or ["_None._"]),
            "",
            f"## Class uses ({len(impact['class_uses'])})",
            *(_fmt(impact["class_uses"]) or ["_None._"]),
            "",
            f"## Extends chain ({len(impact['extends_chain'])})",
            *(_fmt(impact["extends_chain"]) or ["_None._"]),
            "",
        ]
        return "\n".join(lines).rstrip() + "\n"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


@tool(tags=["codegraph"])
def codegraph_status(root: str = ".") -> dict:
    """Quick health check on the CodeGraph index.

    Reports DB path, file/node/edge counts, and the most recent index
    timestamp. Returns ``indexed: false`` and zero counts if no index
    has run yet.

    root: Project directory.
    """
    try:
        resolved = _resolve_root(root)
        err = _validate_root(resolved)
        if err:
            return {"error": f"ValueError: {err}"}
        db_path = _db_path(resolved)
        if not db_path.exists():
            return {
                "root": str(resolved),
                "db": str(db_path),
                "indexed": False,
                "files": 0,
                "nodes": 0,
                "edges": 0,
                "last_indexed": None,
            }
        cg = _get_graph(str(resolved))
        conn = cg.db.connection
        files = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
        nodes = int(conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
        edges = int(conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0])
        last_ts_row = conn.execute(
            "SELECT MAX(indexed_at) FROM files"
        ).fetchone()
        last_ts = last_ts_row[0] if last_ts_row else None
        last_iso = (
            datetime.fromtimestamp(float(last_ts), tz=timezone.utc).isoformat()
            if last_ts
            else None
        )
        return {
            "root": str(resolved),
            "db": str(db_path),
            "indexed": files > 0,
            "files": files,
            "nodes": nodes,
            "edges": edges,
            "last_indexed": last_iso,
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


__all__ = [
    "codegraph_init",
    "codegraph_index",
    "codegraph_query",
    "codegraph_callers",
    "codegraph_callees",
    "codegraph_context",
    "codegraph_impact",
    "codegraph_status",
    "_get_graph",
    "_close_all_graphs",
]
