# CodeGraph Python Port — Implementation Plan

**Goal:** Port `@colbymchenry/codegraph` (TypeScript, 31k LOC) to native Python inside Cogitum, exposing it as builtin tools to the agent. No Node.js dependency, no MCP wrapper — direct Python integration. Ship as v0.8.0.

**Reference codebase:** `/home/starred/Cogitum/codegraph/` (read-only — PORT, don't fork; do NOT vendor in dist).

**Architecture:**

```
files → CodeGraphIndexer (tree-sitter) → SQLite (nodes/edges/files, FTS5)
              ↓
       ReferenceResolver (imports, name-matching, framework patterns)
              ↓
       GraphTraverser (callers, callees, impact radius)
              ↓
       ContextBuilder (markdown for agent consumption)
              ↓
       Cogitum builtin tools (codegraph_index, codegraph_callers, …)
```

**Tech Stack:**
- Python 3.11+ (existing Cogitum)
- `tree-sitter` (PyPI) + per-language grammar packages (`tree-sitter-python`, `tree-sitter-typescript`, etc.)
- `sqlite3` stdlib + FTS5 (Python's bundled SQLite has FTS5 since 3.7)
- `watchdog` for file watching (or stdlib + polling fallback)
- No new heavy deps

**Scope breakdown (what we port vs skip):**

| Module | TS LOC | Port? | Note |
|---|---|---|---|
| db/ (schema, queries) | 2041 | YES | Drop-in SQLite |
| extraction/ (orchestrator + languages) | 5892 + 1839 | YES (phase by phase) | Python first, then TS, JS, Rust, Go |
| resolution/ + frameworks/ | 2951 + 4487 | YES (Python frameworks first) | Django, FastAPI, Flask first |
| graph/ (BFS/DFS, impact) | 1099 | YES | Pure logic |
| context/ (markdown builder) | 1405 | YES | |
| search/ (FTS5 query parser) | 526 | YES | |
| sync/ (file watcher) | 540 | YES | watchdog |
| installer/, bin/, ui/, mcp/ | ~5500 | **SKIP** | Cogitum exposes as builtin tools directly |

**Estimate:** 2-3 weeks via incremental delegate_task batches.

---

## Phases

### Phase 1: Foundation (today)
- 1.1: Plan saved (this file)
- 1.2: `cogitum/codegraph/` package skeleton + types
- 1.3: SQLite schema + migrations + connection layer
- 1.4: Tree-sitter loader for Python language (single-language MVP)
- 1.5: Basic extractor for Python (functions, classes, imports)
- 1.6: Smoke test: index Cogitum's own `cogitum/` and query

### Phase 2: Indexer + Graph (week 1)
- 2.1: ExtractionOrchestrator (parallel parsing via `concurrent.futures`)
- 2.2: ReferenceResolver (imports + name matching)
- 2.3: GraphTraverser (callers, callees, BFS/DFS, impact radius)
- 2.4: FTS5 search parser
- 2.5: Tests: 50+ pytest cases on a synthetic Python project

### Phase 3: ContextBuilder + Cogitum tools (week 1)
- 3.1: ContextBuilder (markdown formatter for AI)
- 3.2: Builtin tools: `codegraph_init`, `codegraph_index`, `codegraph_query`, `codegraph_callers`, `codegraph_callees`, `codegraph_context`, `codegraph_status`
- 3.3: Tool tests + integration with agent loop
- 3.4: Slash commands: `/codegraph init`, `/codegraph status`

### Phase 4: More languages (week 2)
- 4.1: TypeScript / JavaScript extractor
- 4.2: Rust extractor
- 4.3: Go extractor
- 4.4: 6 more (Java, Ruby, C#, PHP, Swift, Kotlin) — minimum viable

### Phase 5: Frameworks (week 2)
- 5.1: Django routes/views
- 5.2: FastAPI routes
- 5.3: Flask routes
- 5.4: Express, Rails, Spring (lower priority)

### Phase 6: File watcher + sync (week 3)
- 6.1: `watchdog`-based watcher with debounce
- 6.2: Git-hook integration (post-checkout, post-merge)
- 6.3: `codegraph_sync` builtin tool

### Phase 7: Polish + release (week 3)
- 7.1: Performance benchmark vs npm CodeGraph (target: ≤2× slower acceptable)
- 7.2: `/help` updates, README section
- 7.3: Migration helper for existing `.codegraph/` SQLite databases (compatible schema)
- 7.4: Bump to 0.8.0, CHANGELOG, release

---

## Phase 1 detail — Foundation (today's work)

### Task 1.1: Save this plan ✅
This file.

### Task 1.2: Package skeleton

**Files:**
- Create: `cogitum/codegraph/__init__.py`
- Create: `cogitum/codegraph/types.py` (NodeKind, EdgeKind, dataclasses)
- Create: `cogitum/codegraph/codegraph.py` (main `CodeGraph` class — public API)

NodeKind / EdgeKind ported from `codegraph/src/types.ts`:
- NodeKind = Literal['file', 'module', 'class', 'struct', 'interface', 'trait', 'protocol', 'function', 'method', 'property', 'field', 'variable', 'constant', 'enum', 'enum_member', 'type_alias', 'namespace', 'parameter', 'import', 'export', 'route', 'component']
- EdgeKind = Literal['contains', 'calls', 'imports', 'exports', 'extends', 'implements', 'references', 'type_of', 'returns', 'instantiates', 'overrides', 'decorates']

Dataclasses:
- `Node` (id, kind, name, qualified_name, file_id, range, signature, docstring)
- `Edge` (src_id, dst_id, kind, file_id, range)
- `File` (id, path, hash, mtime, language)

### Task 1.3: SQLite schema

**File:** `cogitum/codegraph/db/schema.sql` (port of `codegraph/src/db/schema.sql`)

Schema tables: `files`, `nodes`, `edges`, `imports`, plus FTS5 virtual table `nodes_fts`.

**File:** `cogitum/codegraph/db/connection.py` — DatabaseConnection wrapper around `sqlite3` with prepared statements.

### Task 1.4: Tree-sitter loader

**File:** `cogitum/codegraph/extraction/parser.py`
- Load tree-sitter Python language via `tree_sitter_python` package
- Parse file → tree → root node
- Lazy load other languages on demand

### Task 1.5: Python extractor

**File:** `cogitum/codegraph/extraction/languages/python.py` (port of `codegraph/src/extraction/languages/python.ts`)

Extract:
- `def` → function/method node
- `class` → class node
- `import X`, `from X import Y` → import node + edge
- `<name>(args)` calls inside functions → calls edge

### Task 1.6: Smoke test

**File:** `tests/codegraph/test_smoke.py`

```python
def test_index_cogitum_self():
    cg = CodeGraph(":memory:")
    cg.init(root="cogitum/")
    cg.index_all()
    nodes = cg.search_nodes("agent", limit=10)
    assert any("Agent" in n.name for n in nodes)
    callers = cg.get_callers("Agent.run")
    assert len(callers) > 0
```

Must pass before declaring Phase 1 done.

---

## Open questions to resolve later

- Do we make `.codegraph/` schema bit-compatible with the npm CodeGraph so users can migrate? **Yes** — schema_version column, port verbatim.
- Do we expose CLI subcommand `cog codegraph index` or only via slash command? **Both.**
- Tree-sitter grammar packages are ~10MB each — bundle 3-4 default, lazy-load rest? **Lazy-load all, document deps.**
- Async vs sync API? **Sync core + async wrapper for the agent tool layer** (file I/O is the bottleneck, not CPU; tree-sitter is sync).

---

**Ready to execute.** Phase 1 will be split across 5 delegate_task batches with pytest gates.
