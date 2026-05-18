"""
cogitum.core.legion
~~~~~~~~~~~~~~~~~~~

Cogitator Legion — recursive parallel sub-agent orchestrator.

Replaces the old `delegate_task` tool with a richer model:

  * **2-level hierarchy max.** L0 = the main Cogitum the user talks
    to. L1 = cogitators it spawns. L2 = sub-cogitators an L1 may
    spawn. L2 cannot call ``legion``.

  * **Async sibling messaging.** Every L1/L2 has an inbox. Other
    cogitators in the same swarm (siblings + parent) can drop
    messages with :meth:`LegionState.send_message`. The recipient
    sees the inbox flushed into its system prompt at the start of
    every turn — no synchronous wait, no deadlock risk.

  * **Realtime sibling roster.** Each turn a cogitator can read the
    current ``{id, goal, status, last_action}`` of every other
    cogitator in its swarm via :meth:`LegionState.roster_for`.

  * **Cascade lifecycle.** L1 stays in ``awaiting_sub`` while any of
    its L2 are running; killing an L1 cancels its L2 subtree.

  * **Tree visibility.** Every node, message, and status change is
    journalled into a ``LegionRun`` object. The TUI subscribes via
    asyncio.Queue events and renders the tree screen.

This module owns the runtime ONLY — pure asyncio + dataclasses, no
Textual imports. UI consumes events from ``LegionRun.events``.

Public surface:

    Legion          — singleton-per-process orchestrator
    LegionRun       — one user-initiated swarm invocation (root + tree)
    LegionNode      — one cogitator node (L1 or L2)
    LegionMessage   — sibling message
    NodeStatus      — lifecycle enum
    LegionEvent     — events emitted to subscribers (TUI)

Top-level convenience:

    legion = get_legion()
    run = await legion.dispatch_l1(parent=None, tasks=[{...}, ...])
    async for event in run.events_iter():
        ...

The legion tool (:mod:`cogitum.core.builtin_tools`) wraps this for
the agent: an L0 calling ``legion(tasks=[...])`` produces a
``LegionRun``; the agent's main loop awaits it and feeds the
aggregated result back into L0's context.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Awaitable, Callable

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Hard limits — keep us from setting our own keys on fire
# ─────────────────────────────────────────────────────────────────────────

MAX_DEPTH = 2                # L0 → L1 → L2; L2 is a leaf, no further legion calls
MAX_L1_SIBLINGS = 5          # main can spawn up to 5 cogitators in parallel
MAX_L2_SIBLINGS = 3          # each L1 can spawn up to 3 sub-cogitators
ROSTER_FIELD_LEN = 80        # truncate goal/last_action when shown to siblings
MESSAGE_BODY_MAX = 2000      # sibling messages are short by design


# ─────────────────────────────────────────────────────────────────────────
# Enums + dataclasses
# ─────────────────────────────────────────────────────────────────────────


class NodeStatus(str, Enum):
    """Lifecycle of a single cogitator node."""

    PENDING = "pending"           # spawned, queued, not yet running
    RUNNING = "running"           # actively talking to LLM / executing tools
    AWAITING_SUB = "awaiting_sub" # spawned L2, waiting for them to finish
    DONE = "done"                 # produced output successfully
    FAILED = "failed"             # exception or unrecoverable error
    CANCELLED = "cancelled"       # parent killed or user requested stop


@dataclass(slots=True)
class LegionMessage:
    """One sibling-to-sibling message inside a LegionRun."""

    sender_id: str            # node id, or "L0" for top-level Cogitum
    recipient_id: str         # node id, or "*" for broadcast in the run
    body: str
    timestamp: float = field(default_factory=time.time)


@dataclass(slots=True)
class LegionEvent:
    """Tree-update event emitted to TUI subscribers.

    ``kind`` values:
        ``node_added``       payload = {"node_id": ..., "parent_id": ..., "goal": ...}
        ``node_status``      payload = {"node_id": ..., "status": ..., "last_action": ...}
        ``node_token``       payload = {"node_id": ..., "delta": <text>}
        ``message``          payload = {"sender": ..., "recipient": ..., "body": ...}
        ``run_done``         payload = {"summary": <aggregated text>}
    """

    kind: str
    payload: dict[str, Any]
    timestamp: float = field(default_factory=time.time)


@dataclass
class LegionNode:
    """A single cogitator node — either L1 or L2."""

    id: str                                  # globally unique within the run
    parent_id: str | None                    # None for L1, an L1 id for L2
    depth: int                               # 1 for L1, 2 for L2
    goal: str                                # what this cogitator should do
    context: str = ""                        # extra context the L1 may pass down
    status: NodeStatus = NodeStatus.PENDING
    last_action: str = ""                    # short label of current activity
    output: str = ""                         # final text the cogitator produced
    error: str = ""                          # populated if status == FAILED
    inbox: deque[LegionMessage] = field(default_factory=deque)
    children: list[str] = field(default_factory=list)  # ids of L2 spawned by this L1
    started_at: float = 0.0
    finished_at: float = 0.0
    tokens_used: int = 0


@dataclass
class LegionRun:
    """One swarm invocation — root + entire tree of cogitators.

    Created by :meth:`Legion.start_run`. Lives until every node has
    reached a terminal status and the aggregated summary is computed.
    """

    run_id: str
    root_goal: str                           # the L0 task that triggered the swarm
    nodes: dict[str, LegionNode] = field(default_factory=dict)
    events: asyncio.Queue[LegionEvent] = field(default_factory=asyncio.Queue)
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    summary: str = ""                        # set when run completes
    cancelled: bool = False

    # --------- public read API ---------

    def l1_nodes(self) -> list[LegionNode]:
        return [n for n in self.nodes.values() if n.depth == 1]

    def children_of(self, node_id: str) -> list[LegionNode]:
        node = self.nodes.get(node_id)
        if node is None:
            return []
        return [self.nodes[cid] for cid in node.children if cid in self.nodes]

    def is_complete(self) -> bool:
        return all(
            n.status in (NodeStatus.DONE, NodeStatus.FAILED, NodeStatus.CANCELLED)
            for n in self.nodes.values()
        )

    async def events_iter(self) -> AsyncIterator[LegionEvent]:
        """Async iterator over events. Stops when ``run_done`` is seen."""
        while True:
            ev = await self.events.get()
            yield ev
            if ev.kind == "run_done":
                return

    # --------- convenience helpers used by the runtime ---------

    def _emit(self, kind: str, **payload: Any) -> None:
        try:
            self.events.put_nowait(LegionEvent(kind=kind, payload=payload))
        except asyncio.QueueFull:
            logger.debug("legion events queue full — dropping %s", kind)


# ─────────────────────────────────────────────────────────────────────────
# Roster + message rendering for cogitator system prompts
# ─────────────────────────────────────────────────────────────────────────


def _truncate(text: str, n: int = ROSTER_FIELD_LEN) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


def render_roster_for(run: LegionRun, viewer_id: str) -> str:
    """Build the realtime sibling roster block for a cogitator's prompt.

    Each cogitator on every turn sees a fresh snapshot of:
      * its parent (if any) — goal + status
      * itself        — goal + status (own self-anchor)
      * its siblings  — goal + status + last_action
      * its children  — if it spawned L2

    The block is plain text, drop-in into the system prompt.
    """
    me = run.nodes.get(viewer_id)
    if me is None:
        return ""

    lines: list[str] = ["═══ LEGION ROSTER ═══"]

    # Parent
    if me.parent_id and me.parent_id in run.nodes:
        p = run.nodes[me.parent_id]
        lines.append(f"  parent  [{p.id}]  goal: {_truncate(p.goal)}  status: {p.status.value}")
    else:
        lines.append(f"  parent  [L0]  goal: {_truncate(run.root_goal)}  status: orchestrating")

    # Self
    lines.append(
        f"  YOU     [{me.id}]  goal: {_truncate(me.goal)}  status: {me.status.value}"
    )

    # Siblings: same parent, different id
    siblings = [
        n for n in run.nodes.values()
        if n.id != me.id
        and n.parent_id == me.parent_id
    ]
    if siblings:
        lines.append("  siblings:")
        for n in siblings:
            la = f"  ({_truncate(n.last_action, 40)})" if n.last_action else ""
            lines.append(
                f"    [{n.id}]  {_truncate(n.goal)}  · {n.status.value}{la}"
            )

    # Children (only relevant if this node is an L1 with L2 spawned)
    if me.children:
        lines.append("  children:")
        for cid in me.children:
            c = run.nodes.get(cid)
            if c is None:
                continue
            lines.append(
                f"    [{c.id}]  {_truncate(c.goal)}  · {c.status.value}"
            )

    return "\n".join(lines)


def render_inbox_for(node: LegionNode) -> str:
    """Drain the node's inbox into a prompt block. Empty string when
    no messages — caller should skip injection in that case."""
    if not node.inbox:
        return ""

    lines: list[str] = ["═══ INBOX (messages from siblings) ═══"]
    while node.inbox:
        m = node.inbox.popleft()
        # Truncate body so a malicious sibling can't blow up our context.
        body = m.body[:MESSAGE_BODY_MAX]
        if len(m.body) > MESSAGE_BODY_MAX:
            body += " …(truncated)"
        lines.append(f"  from [{m.sender_id}]: {body}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────
# Legion orchestrator (singleton)
# ─────────────────────────────────────────────────────────────────────────


# Type for the worker callable that actually drives an LLM-backed
# cogitator. Plugged in by the agent layer at startup so this module
# stays UI- and provider-agnostic.
#
# Signature:  worker(node, run, send_message, spawn_l2) -> str
#
#   node          LegionNode for this cogitator
#   run           the LegionRun (use it to read roster)
#   send_message  callable(recipient_id: str, body: str) for sibling chatter
#   spawn_l2      callable(tasks: list[dict]) -> awaitable[list[str]]
#                 None when this node is L2 (no further nesting allowed)
#
# Returns the cogitator's final text output.
WorkerCallable = Callable[
    [LegionNode, "LegionRun", Callable[[str, str], None],
     Callable[[list[dict]], Awaitable[list[str]]] | None],
    Awaitable[str],
]


class Legion:
    """Process-wide swarm orchestrator.

    Holds active runs, registers the worker callable, dispatches L1
    and L2 tasks, and aggregates results. UI talks to it via
    :meth:`get_run` and :meth:`active_runs`.
    """

    def __init__(self) -> None:
        self._runs: dict[str, LegionRun] = {}
        self._worker: WorkerCallable | None = None
        self._next_node_n = 0
        self._lock = asyncio.Lock()

    def register_worker(self, worker: WorkerCallable) -> None:
        """Wire in the agent-backed worker. Called once at startup."""
        self._worker = worker

    def active_runs(self) -> list[LegionRun]:
        return [r for r in self._runs.values() if not r.is_complete()]

    def get_run(self, run_id: str) -> LegionRun | None:
        return self._runs.get(run_id)

    # --------------------------------------------------------------
    # Public dispatch — called by the legion tool at L0
    # --------------------------------------------------------------

    async def start_run(
        self,
        root_goal: str,
        tasks: list[dict],
    ) -> LegionRun:
        """Spawn an L1 swarm for the given tasks under a fresh run.

        ``tasks`` is a list of ``{id?, goal, context?}`` dicts. ``id`` is
        optional — if absent we generate ``L1-<n>``. Returns the
        :class:`LegionRun` once all L1 (and recursively their L2)
        nodes have terminated.
        """
        if self._worker is None:
            raise RuntimeError(
                "Legion.start_run called but no worker is registered. "
                "Call Legion.register_worker() at agent startup."
            )
        if not tasks:
            raise ValueError("legion: tasks list is empty")
        if len(tasks) > MAX_L1_SIBLINGS:
            raise ValueError(
                f"legion: max {MAX_L1_SIBLINGS} L1 cogitators per run "
                f"(got {len(tasks)})"
            )

        run_id = f"run-{int(time.time() * 1000) % 1_000_000:06d}"
        run = LegionRun(run_id=run_id, root_goal=root_goal)
        self._runs[run_id] = run

        # Materialise all L1 nodes first so siblings see each other in
        # the roster from turn 1.
        l1_nodes: list[LegionNode] = []
        for i, t in enumerate(tasks):
            node_id = self._coerce_id(t.get("id"), depth=1, default_n=i)
            if node_id in run.nodes:
                # Duplicate id from the LLM — disambiguate.
                node_id = f"{node_id}-{i}"
            node = LegionNode(
                id=node_id,
                parent_id=None,
                depth=1,
                goal=str(t.get("goal", "")).strip(),
                context=str(t.get("context", "")).strip(),
            )
            run.nodes[node_id] = node
            l1_nodes.append(node)
            run._emit("node_added",
                      node_id=node_id, parent_id=None, goal=node.goal, depth=1)

        # Run all L1 in parallel.
        await asyncio.gather(*[self._run_node(run, n) for n in l1_nodes])

        # Aggregate.
        run.summary = self._aggregate_l1(run)
        run.finished_at = time.time()
        run._emit("run_done", summary=run.summary)
        return run

    # --------------------------------------------------------------
    # Internal — node execution
    # --------------------------------------------------------------

    async def _run_node(self, run: LegionRun, node: LegionNode) -> None:
        """Drive a single cogitator node through its lifecycle."""
        if run.cancelled:
            self._set_status(run, node, NodeStatus.CANCELLED)
            return

        self._set_status(run, node, NodeStatus.RUNNING)
        node.started_at = time.time()

        # Wire the per-node spawn_l2 callable. None for L2 nodes —
        # they cannot recurse further.
        if node.depth >= MAX_DEPTH:
            spawn_l2: Callable[[list[dict]], Awaitable[list[str]]] | None = None
        else:
            async def spawn_l2(tasks: list[dict]) -> list[str]:
                return await self._spawn_l2(run, node, tasks)

        # Wire send_message.
        def send_message(recipient_id: str, body: str) -> None:
            self.deliver_message(run, sender_id=node.id,
                                 recipient_id=recipient_id, body=body)

        try:
            assert self._worker is not None
            output = await self._worker(node, run, send_message, spawn_l2)
        except asyncio.CancelledError:
            self._set_status(run, node, NodeStatus.CANCELLED)
            raise
        except Exception as exc:
            logger.exception("legion node %s failed", node.id)
            node.error = f"{type(exc).__name__}: {exc}"
            self._set_status(run, node, NodeStatus.FAILED, last_action=node.error[:80])
            return

        node.output = output
        node.finished_at = time.time()
        self._set_status(run, node, NodeStatus.DONE)

    async def _spawn_l2(
        self,
        run: LegionRun,
        parent: LegionNode,
        tasks: list[dict],
    ) -> list[str]:
        """Worker-callable hook: parent (L1) wants to spawn L2 children.

        Marks parent ``AWAITING_SUB``, materialises children so they
        see each other, runs them in parallel, then returns the list
        of their outputs in input order.
        """
        if parent.depth != 1:
            raise RuntimeError(
                "legion: only L1 cogitators may spawn L2 (got depth=%d)"
                % parent.depth
            )
        if not tasks:
            return []
        if len(tasks) > MAX_L2_SIBLINGS:
            raise ValueError(
                f"legion: max {MAX_L2_SIBLINGS} L2 cogitators per L1 "
                f"(got {len(tasks)})"
            )

        prev_status = parent.status
        self._set_status(run, parent, NodeStatus.AWAITING_SUB,
                         last_action=f"spawning {len(tasks)} sub-cogitators")

        l2_nodes: list[LegionNode] = []
        for i, t in enumerate(tasks):
            base_id = self._coerce_id(t.get("id"), depth=2, default_n=i)
            node_id = f"{parent.id}.{base_id}"
            if node_id in run.nodes:
                node_id = f"{node_id}-{i}"
            node = LegionNode(
                id=node_id,
                parent_id=parent.id,
                depth=2,
                goal=str(t.get("goal", "")).strip(),
                context=str(t.get("context", "")).strip(),
            )
            run.nodes[node_id] = node
            parent.children.append(node_id)
            l2_nodes.append(node)
            run._emit("node_added",
                      node_id=node_id, parent_id=parent.id,
                      goal=node.goal, depth=2)

        await asyncio.gather(*[self._run_node(run, n) for n in l2_nodes])

        # Restore parent to RUNNING so it can produce its final output.
        self._set_status(run, parent, NodeStatus.RUNNING,
                         last_action="aggregating sub-cogitator outputs")

        return [n.output for n in l2_nodes]

    # --------------------------------------------------------------
    # Messaging
    # --------------------------------------------------------------

    def deliver_message(
        self,
        run: LegionRun,
        *,
        sender_id: str,
        recipient_id: str,
        body: str,
    ) -> None:
        """Deliver a sibling message. ``recipient_id == "*"`` broadcasts
        to every node in the run except the sender."""
        msg = LegionMessage(
            sender_id=sender_id,
            recipient_id=recipient_id,
            body=body[:MESSAGE_BODY_MAX],
        )
        if recipient_id == "*":
            for n in run.nodes.values():
                if n.id != sender_id:
                    n.inbox.append(msg)
        else:
            target = run.nodes.get(recipient_id)
            if target is None:
                logger.info("legion: dropping message to unknown id %s", recipient_id)
                return
            target.inbox.append(msg)
        run._emit("message",
                  sender=sender_id, recipient=recipient_id, body=msg.body)

    # --------------------------------------------------------------
    # Aggregation + helpers
    # --------------------------------------------------------------

    @staticmethod
    def _aggregate_l1(run: LegionRun) -> str:
        """Build the summary the L0 caller sees as the legion tool result.

        Format is a JSON-friendly text block per L1 node, in the order
        they were dispatched:

            ── [L1-0] refactor auth ──
            status: done
            output: <text...>

            ── [L1-1] write tests ──
            status: failed
            error: ...
        """
        out: list[str] = []
        for n in run.l1_nodes():
            out.append(f"── [{n.id}] {n.goal} ──")
            out.append(f"status: {n.status.value}")
            if n.error:
                out.append(f"error: {n.error}")
            if n.children:
                out.append(f"sub-cogitators: {len(n.children)}")
            if n.output:
                out.append(f"output: {n.output}")
            out.append("")
        return "\n".join(out).rstrip()

    def _coerce_id(self, raw: Any, *, depth: int, default_n: int) -> str:
        """Sanitise / generate a node id."""
        if raw and isinstance(raw, str) and raw.strip():
            # Allow letters/digits/dash/underscore/dot.
            cleaned = "".join(
                c if (c.isalnum() or c in "-_.") else "_"
                for c in raw.strip()
            )
            if cleaned:
                return cleaned
        prefix = "L1" if depth == 1 else "L2"
        return f"{prefix}-{default_n}"

    def _set_status(
        self,
        run: LegionRun,
        node: LegionNode,
        status: NodeStatus,
        *,
        last_action: str = "",
    ) -> None:
        node.status = status
        if last_action:
            node.last_action = last_action
        run._emit("node_status",
                  node_id=node.id, status=status.value,
                  last_action=node.last_action)

    # --------------------------------------------------------------
    # Cancellation
    # --------------------------------------------------------------

    async def cancel_run(self, run_id: str) -> None:
        """Mark a run cancelled. Worker tasks should observe this on
        their next yield and bail out gracefully."""
        run = self._runs.get(run_id)
        if run is None:
            return
        run.cancelled = True
        for node in run.nodes.values():
            if node.status in (NodeStatus.PENDING, NodeStatus.RUNNING,
                               NodeStatus.AWAITING_SUB):
                self._set_status(run, node, NodeStatus.CANCELLED,
                                 last_action="run cancelled")


# ─────────────────────────────────────────────────────────────────────────
# Singleton accessor
# ─────────────────────────────────────────────────────────────────────────


_INSTANCE: Legion | None = None


def get_legion() -> Legion:
    """Return the process-wide :class:`Legion` orchestrator."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = Legion()
    return _INSTANCE


__all__ = [
    "MAX_DEPTH",
    "MAX_L1_SIBLINGS",
    "MAX_L2_SIBLINGS",
    "NodeStatus",
    "LegionMessage",
    "LegionEvent",
    "LegionNode",
    "LegionRun",
    "Legion",
    "WorkerCallable",
    "get_legion",
    "render_roster_for",
    "render_inbox_for",
]
