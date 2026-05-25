# Changelog

## v0.8.0 — 2026-05-25 — "CodeGraph + adaptive UI + tool-flow hardening"

This is a heavy stability release plus a major new feature foundation. Three
parallel tracks landed: tool-call output flow correctness, fully adaptive
TUI, and the foundations of a native Python CodeGraph (Phase 1 + 2 of a
3-week port).

Test count: **588 → 1059** (+471 over the 0.7 line). Zero regressions.

### Major: CodeGraph (Phase 1 + 2)

A native Python port of `@colbymchenry/codegraph`. No Node.js dependency,
no MCP wrapper — direct integration into the agent. Phase 1 (foundation)
and Phase 2 (parallel indexing + reference resolution + graph traversal)
are shipped; Phase 3 (markdown context builder + builtin agent tools) is
next.

What works today:
- `cogitum/codegraph/` package with deterministic AST-driven extraction
  via tree-sitter (Python only in 0.8.0; TS/JS/Rust/Go land in 0.9.0).
- SQLite + FTS5 storage with per-project graph at `<root>/.codegraph/graph.db`.
- Public API: `CodeGraph.init()`, `index_all(parallel=True)`, `search_nodes()`,
  `get_callers()`, `get_callees()`, `get_impact_radius()`, `find_paths()`,
  `class_hierarchy()`.
- `ExtractionOrchestrator` runs parsers across `ProcessPoolExecutor` workers
  (CPU-bound), single-writer SQLite stays in the parent process.
- `ReferenceResolver` upgrades `<unresolved>:<name>` sentinel edges to real
  Node IDs via four rules: same-module, imports, `self.method`, project-wide
  unique-name fallback.
- `GraphTraverser` runs BFS over callers/callees, computes impact radius,
  enumerates paths between symbols, walks class hierarchies.

Smoke-tested on Cogitum's own `cogitum/core` tree: **54 files, 795 nodes,
3,752 edges, 437 imports indexed in <1s**.

Builtin agent tools (`/codegraph init`, `/codegraph query`, `/codegraph context`,
etc.) and the markdown ContextBuilder are scheduled for the next release.

### Major: Tool-call output flow (the original bug)

The user-visible symptom — "tool output sometimes silently disappears
before reaching the model" — is fixed. Root cause was in
`cogitum/core/tools.py:ToolSpec.call`: a sync wrapper returning a coroutine
(common pattern for `functools.partial` over async, decorator wrappers)
fell through `iscoroutinefunction` (False) into `run_in_executor`, then the
coroutine object was `str()`-ed into the tool result. The model received
`<coroutine object foo at 0x...>` as a successful tool result.

Fix: `inspect.isawaitable(result) → await result` after dispatch. Plus 16
more tool-flow correctness fixes across pipeline / dispatch / history
layers, found in three rounds of audit + fix:

Pipeline:
- Anthropic streams synthesize a stable `toolu_auto_<idx>_<uuid>` id when
  the provider emits `tool_use` blocks with empty/missing id, so the
  sanitizer no longer drops paired tool_results as orphans.
- New `format_tool_result_for_model(result)` helper: `None`/`""` → `(no output)`,
  dict/list → `json.dumps(indent=2)`, bytes → utf-8 decode w/ base64 fallback,
  exceptions → `ERROR: <type>: <msg>` plus traceback. Wired into the primary
  agent loop, legion workers, and delegate workers — three execution paths,
  one canonical formatter.
- OpenAI assistant messages with `tool_calls` and empty content emit
  `content: null` instead of `""` (some providers HTTP 400 on the latter).
- `tool_buffers` cleared after `TOOL_CALL_DONE` in `openai_compat` (prevents
  double-execute on a follow-up empty chunk with the same finish_reason).
- Invalid JSON in tool arguments produces an `ERROR:` tool result via a new
  `_malformed_tool_call_ids` map (cleared at `Agent.run()` start) instead
  of injecting a `_raw` arg the model can't parse.

Dispatch:
- MCP empty content with `isError=False` returns `(no output)`. Empty
  content with `isError=True` returns a clear error string.
- `submit_approval` is now thread-safe via `loop.call_soon_threadsafe` —
  no more silent 300s timeouts when a TG callback fires from a worker thread.
- `Agent.aclose()` cancels all pending approval futures on shutdown.
- `_approval_queue=None` no longer silently auto-approves; it logs
  `log.warning` with the danger level and tool name.

History / compaction:
- Compaction summary now preserves the tool_call ↔ tool_result link with
  `[tool_result for <name>(<short_args>) id=<call_id>]` markers — the
  summarizer no longer emits ungrounded "tool_result: 42" lines.
- `ThinkingPart` now carries the model id that produced it; on `/model`
  switch mid-conversation, signatures are dropped on mismatch so Anthropic
  doesn't 400 on stale signed thinking blocks.
- Anthropic assistant messages with all-unsigned thinking get `(empty)`
  text fallback so the API accepts them.
- Sessions JSONL round-trip preserves the new `model` field on `ThinkingPart`.

### Major: Adaptive TUI

Cogitum had **zero** `on_resize` handlers and a sea of fixed widths. On
80x24 SSH clients (still the most common terminal size in the wild) modals
overflowed, the inspector fought the feed for space, and the figlet banner
clipped. Score went from 3/10 → 9/10.

- New global `App.on_resize` handler tags the App with `-narrow` (≤80
  cols), `-medium` (81-120), `-wide` (>120), and `-short` (≤24 rows).
  Every adaptive widget reacts via CSS class selectors.
- On `-narrow`: inspector pane hidden, feed takes full width. Modal frames
  drop to 100% width with `max-width: 95%` for setup screens.
- On `-short`: figlet banner collapses to a slim 1-line title row;
  composer max-height drops to 5 rows; CommandMenu max-height drops to 4.
- `statusbar` switches between `_format_full()` and `_format_compact()`
  forms; compact drops verbose labels, ellipsizes the model id.
- `legion_tree` adds an L3 ultra-compact card class; cards downgrade
  L1→L2→L3 based on `app.size.width / num_cards`.
- `feed.ToolCallCard` replaces every literal `[:50]`, `[:65]`, `[:80]`
  truncation with `_truncate_for_screen(text)` that scales to `app.size.width`.
- `model_picker` hides its detail-scroll panel on narrow, list takes 100%.
- `session_picker` 90×28 fixed → adaptive with min/max.
- `banner._NARROW_THRESHOLD` lowered from 70 to `_LOGO_WIDTH-10` (floor 40).

74 new tests cover the breakpoint logic, individual widget responses, and
the truncation helpers.

### Other safety + quality wins

- Process group kill: `os.killpg` on POSIX, `CTRL_BREAK_EVENT`+`taskkill /T /F`
  on Windows. No more orphan children when an LLM-driven `bash -c '... &'`
  is killed.
- ProcessManager hard cap on background processes (32 default), `asyncio.Lock`
  on `spawn()` to fix the cap-check TOCTOU race.
- `output_lines` switched from list-with-resize to `collections.deque(maxlen=…)`
  for atomic eviction (no more lost lines under aggressive readers).
- `send_media` calls `_is_path_safe` and clamps danger to "medium". Sensitive
  paths list expanded with `.config/cogitum/auth.json`, `.netrc`,
  `.config/cogitum/providers.toml`.
- `classify_danger` NFKC-normalizes commands and strips Cf-category Unicode
  before the deny-list lookup (RTL/ZWSP bypass closed). Pipe-to-shell
  pattern (`curl ... | bash`, `wget ... | sh`, `Invoke-WebRequest ... | pwsh`)
  classified as ≥medium.
- `_BROWSER_LOCK` + module-level `_BROWSER_STATE` declaration (no more
  `globals()` race on first `browse(open=...)` call).
- SSRF guard: `_is_url_safe` rejects hostnames containing zero-width or
  RTL chars; popup-handler failures set `state['ssrf_guard_partial']` and
  the next `browse(...)` action returns an error pointing the operator at
  `browse(action='close')` to recover.
- `_approval_token_to_call_id` persisted to disk atomically (mode 0600,
  capped to 1024 entries on restore). Stale callbacks after a restart
  edit the message to `[stale — bot restarted, ignore]` and answer the
  callback with a clean toast.
- Markdown fallback in `send_message` no longer strips `\` (was killing
  Windows paths, regex, JSON in chat output). On parse error, re-sends
  with `parse_mode=None` instead.
- `_load_offset` now logs warnings and falls back to `-1` sentinel on
  corrupt offset files, so Telegram skips the 24h replay storm after
  a restart with a busted offset file.
- `tg_offset`, `telegram.toml`, `tg_approvals.json`, session JSONL all
  go through `atomic_write_text` with parent-dir fsync on POSIX.
- `_TokenScrubFilter` redacts bot tokens from tracebacks even in third-party
  loggers (`httpx`, `anyio`, `urllib3`).
- Operator-only commands (`/yolo`, `/godmode`, `/model`, `/reload`,
  `/resume`, `/title`, `/stop`, `/new`, `/compact`) refuse with a
  helpful "only the deployment owner can run this; use /tools or /help
  freely" message in groups.
- `/yolo on <minutes>` TTL with monotonic clock.
- `cog tg setup` reads bot token via `getpass.getpass` so it doesn't end
  up in shell history.
- TG API retries (`ReadTimeout` / `ConnectError` / `RemoteProtocolError`)
  with exponential backoff; HTTP 409 on `getUpdates` raises a clear
  "another bot polling — stop with cog tg stop" error.
- Windows daemon child gets `PYTHONIOENCODING=utf-8` + `PYTHONUTF8=1` so
  the log file isn't cp1251 mojibake.
- Ctrl+C signal handler on Windows now uses `loop.call_soon_threadsafe`
  to schedule `bot.stop()` (was crashing with `RuntimeError: no running
  event loop` from the signal handler thread).
- Wildcard `from .core.builtin_tools import *` removed from `app.py` and
  `gateway/telegram.py` — replaced with explicit side-effect import.
- `auth.json` now atomic-write with mode 0o600 from inode-create (no
  chmod-after window) and parent dir 0o700.
- `update_flow` TUI git pull wrapped in `asyncio.wait_for(timeout=120)`
  so a hung remote can't lock the UI forever.
- Persona lock pinned via `PERSONA_LOCK_VERSION` + sha256 snapshot test.
- Session store, atomic IO, redact, godmode-script gate — all already
  shipped in 0.7.x but now have full test coverage.

### Migration notes

No breaking config changes. `cog update` will pull this release. The first
launch on 0.8.0 will pip-reinstall to pick up `tree-sitter` and the four
default grammar packages (~10MB download).

`/yolo` semantics changed: TTL is now in minutes, not seconds. `/yolo on`
without a TTL means "until restart". `/yolo on 30` means "for 30 minutes
of monotonic time".

### Known limitations / Phase 3 backlog

- CodeGraph builtin agent tools (`/codegraph init`, `/codegraph context`, ...)
  not yet wired — coming in 0.8.1 (the partial implementation crashed
  on provider rate limits during the parallel write).
- TS / JS / Rust / Go extractors stubbed but not implemented — 0.9.0.
- Framework awareness (Django routes, FastAPI endpoints, etc.) — 0.9.0.
- File watcher integration with `codegraph_sync` — 0.9.x.

---

## Earlier releases

See git history before this changelog landed.
