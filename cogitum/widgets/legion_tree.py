"""
cogitum.widgets.legion_tree
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Full-screen modal that visualises a Legion swarm in real time.

Layout::

    ┌─ LEGION run-NNNNNN ─────────────────────────────────────────┐
    │                                                              │
    │              ┌─ L0 ─────────────────────────┐                │
    │              │  Magos's order: <root_goal>  │                │
    │              └──────────────────────────────┘                │
    │                            │                                 │
    │       ┌────────────────────┼────────────────────┐            │
    │   ┌── L1 ──┐           ┌── L1 ──┐           ┌── L1 ──┐       │
    │   │ alpha  │           │ beta   │           │ gamma  │       │
    │   │ ◆ run  │           │ ⏳ wait │           │ ◇ done │       │
    │   └────────┘           └────────┘           └────────┘       │
    │       │                                                      │
    │   ┌── L2 ──┐                                                 │
    │   │ a.1    │                                                 │
    │   │ ◆ run  │                                                 │
    │   └────────┘                                                 │
    │                                                              │
    │  ── Selected: alpha ──                                       │
    │  goal: ...                                                   │
    │  status: running   last: turn 2/12                           │
    │  output: ...                                                 │
    │                                                              │
    │  Esc close · ↑↓ select · Enter focus                         │
    └──────────────────────────────────────────────────────────────┘

Subscribes to ``LegionRun.events`` via a background task and refreshes
the tree on every node_added / node_status / message event. Stops on
run_done.

Selection is keyboard-driven: ↑/↓ cycle through nodes (depth-first
traversal), Enter pins the selection in the detail pane below.
"""
from __future__ import annotations

from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static
from rich.text import Text

from ..core.legion import (
    LegionEvent, LegionNode, LegionRun, NodeStatus,
)
from ..design import (
    BG, BG_SOFT, BRONZE, COPPER, GOLD, GOLD_DIM, GOLD_HI,
    MUTED, OK, RULE, RUST, SURFACE, TXT, TXT_DIM,
)


# Glyphs per status — keep small so cards stay compact.
_STATUS_GLYPH: dict[str, tuple[str, str]] = {
    NodeStatus.PENDING.value:       ("·",  GOLD_DIM),
    NodeStatus.RUNNING.value:       ("◆",  GOLD_HI),
    NodeStatus.AWAITING_SUB.value:  ("◇",  BRONZE),
    NodeStatus.DONE.value:          ("✓",  OK),
    NodeStatus.FAILED.value:        ("✗",  RUST),
    NodeStatus.CANCELLED.value:     ("∅",  MUTED),
}


def _status_text(status: str) -> Text:
    glyph, colour = _STATUS_GLYPH.get(status, ("?", TXT_DIM))
    return Text(f"{glyph} {status}", style=colour)


# ─────────────────────────────────────────────────────────────────────────
# Per-node card widget
# ─────────────────────────────────────────────────────────────────────────


class _LegionNodeCard(Static):
    """Single card rendering one cogitator. Click to select."""

    DEFAULT_CSS = """
    _LegionNodeCard {
        width: 28;
        height: 5;
        padding: 0 1;
        background: #161618;
        border: round #2A2620;
        color: #E6E1CF;
    }
    _LegionNodeCard.selected { border: round #F5C24A; }
    _LegionNodeCard.l0       { width: 50; height: 4; border: round #A8732D; }
    """

    def __init__(self, node_id: str, *, l0: bool = False, **kw) -> None:
        # node_id may be the special string "L0" for the root card.
        self._node_id = node_id
        self._l0 = l0
        super().__init__("", **kw)
        if l0:
            self.add_class("l0")

    def update_from(self, run: LegionRun, selected_id: str | None) -> None:
        out = Text()
        if self._l0:
            out.append("⚔ Magos's order\n", style=f"bold {GOLD_HI}")
            goal = run.root_goal[:80] or "(no goal)"
            out.append(goal, style=TXT)
        else:
            node = run.nodes.get(self._node_id)
            if node is None:
                out.append("(missing)", style=RUST)
            else:
                tag = f"L{node.depth} · {node.id}"
                out.append(tag + "\n", style=f"bold {GOLD}")
                goal = (node.goal or "")[:30]
                out.append(goal + "\n", style=TXT)
                out.append(_status_text(node.status.value))
                if node.last_action:
                    out.append("\n" + node.last_action[:30], style=TXT_DIM)
        # Highlight when selected.
        if self._node_id == (selected_id or ""):
            self.add_class("selected")
        else:
            self.remove_class("selected")
        self.update(out)


# ─────────────────────────────────────────────────────────────────────────
# The screen
# ─────────────────────────────────────────────────────────────────────────


class LegionTreeScreen(ModalScreen[None]):
    """Full-screen modal showing a Legion run as a live tree.

    Pass the ``LegionRun`` you want to view — typically obtained via
    ``cogitum.core.legion.get_legion().get_run(run_id)``. The screen
    subscribes to ``run.events`` and refreshes itself; if the run is
    already complete, it renders the final state once.
    """

    DEFAULT_CSS = """
    LegionTreeScreen { background: #0E0E11; }
    #legion-shell {
        width: 100%;
        height: 100%;
        padding: 1 2;
    }
    #legion-title  { color: #F5C24A; text-style: bold; height: 1; }
    #legion-status { color: #9C957D; height: 1; padding-bottom: 1; }

    /* Tree area: L0 in the centre, then horizontal rows of L1 / L2. */
    #legion-l0-row { height: auto; align: center middle; padding: 1 0; }
    #legion-l1-row {
        height: auto;
        layout: horizontal;
        align: center top;
        padding: 1 0;
    }
    .legion-l1-cell {
        layout: vertical;
        align: center top;
        padding: 0 1;
    }
    .legion-l2-row {
        layout: horizontal;
        height: auto;
        align: center top;
        padding-top: 1;
    }
    .legion-l2-cell {
        padding: 0 1;
    }

    /* Detail pane at the bottom — full-width readout for selected node. */
    #legion-detail {
        height: 1fr;
        background: #161618;
        border: round #2A2620;
        padding: 1 2;
        margin-top: 1;
    }
    #legion-detail-title { color: #F5C24A; text-style: bold; height: 1; }

    #legion-foot { height: 1; padding-top: 1; color: #7A5A1A; }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss", "close"),
        Binding("up", "prev_node", "↑ prev"),
        Binding("down", "next_node", "↓ next"),
    ]

    def __init__(self, run: LegionRun) -> None:
        super().__init__()
        self._run = run
        # Visible nodes in selection order: depth-first, L1 before its L2 children.
        self._visible_order: list[str] = []
        self._selected_id: str | None = None
        # Track of mounted card widgets so we update in place instead of rebuilding.
        self._card_by_id: dict[str, _LegionNodeCard] = {}
        self._sub_task = None  # subscription task, started in on_mount

    # -------- compose ----------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="legion-shell"):
            yield Static(f"⚔ LEGION  {self._run.run_id}", id="legion-title")
            yield Static(self._status_line(), id="legion-status")
            # L0 root card.
            with Horizontal(id="legion-l0-row"):
                root = _LegionNodeCard("L0", l0=True)
                self._card_by_id["L0"] = root
                yield root
            # L1 + their L2 children.
            yield VerticalScroll(id="legion-tree-body")
            # Detail pane.
            with Vertical(id="legion-detail"):
                yield Static("── Selected node ──", id="legion-detail-title")
                yield Static("", id="legion-detail-body")
            yield Static(
                "Esc close  ·  ↑↓ select node  ·  click card to select",
                id="legion-foot",
            )

    async def on_mount(self) -> None:
        # Initial render of L1+L2 + first refresh of L0 + detail.
        self._refresh_full_tree()
        # Subscribe to live events.
        import asyncio
        self._sub_task = asyncio.create_task(self._subscribe())

    async def on_unmount(self) -> None:
        if self._sub_task and not self._sub_task.done():
            self._sub_task.cancel()

    # -------- subscription ----------------------------------------------

    async def _subscribe(self) -> None:
        try:
            async for ev in self._run.events_iter():
                self._handle_event(ev)
                if ev.kind == "run_done":
                    break
        except Exception:
            # Subscription dies silently — the screen still works for
            # whatever state was captured up to that point.
            pass

    def _handle_event(self, ev: LegionEvent) -> None:
        # Cheap path: a node already on screen → update it in place.
        nid = ev.payload.get("node_id")
        if ev.kind in ("node_status", "node_token") and nid in self._card_by_id:
            self._card_by_id[nid].update_from(self._run, self._selected_id)
            self._update_detail()
            self._update_status_line()
            return
        if ev.kind == "node_added":
            # New node in the tree → rebuild the body.
            self._refresh_full_tree()
            return
        if ev.kind == "run_done":
            self._update_status_line()
            return

    # -------- rendering --------------------------------------------------

    def _status_line(self) -> str:
        total = len(self._run.nodes)
        done = sum(1 for n in self._run.nodes.values()
                   if n.status in (NodeStatus.DONE, NodeStatus.FAILED, NodeStatus.CANCELLED))
        running = sum(1 for n in self._run.nodes.values()
                      if n.status in (NodeStatus.RUNNING, NodeStatus.AWAITING_SUB))
        if self._run.is_complete():
            tag = "complete"
        elif total == 0:
            tag = "empty"
        else:
            tag = f"{running} running · {done}/{total} done"
        return f"  {tag}  ·  {len(self._run.l1_nodes())} L1 · {total} total"

    def _update_status_line(self) -> None:
        try:
            self.query_one("#legion-status", Static).update(self._status_line())
        except Exception:
            pass

    def _refresh_full_tree(self) -> None:
        """Tear down the L1/L2 area and rebuild from current run state.

        Called on ``node_added`` events and once at mount. Cheaper
        path (status updates) goes through :meth:`_handle_event`
        directly without a rebuild.
        """
        try:
            body = self.query_one("#legion-tree-body", VerticalScroll)
        except Exception:
            return
        # Drop existing L1/L2 cards (keep the L0 card untouched).
        for child in list(body.children):
            child.remove()
        # Reset card registry except for L0.
        self._card_by_id = {k: v for k, v in self._card_by_id.items() if k == "L0"}
        self._visible_order = []

        l1_nodes = self._run.l1_nodes()
        if not l1_nodes:
            return

        l1_row = Horizontal(id="legion-l1-row")
        body.mount(l1_row)

        for l1 in l1_nodes:
            cell = Vertical(classes="legion-l1-cell")
            l1_row.mount(cell)
            l1_card = _LegionNodeCard(l1.id)
            self._card_by_id[l1.id] = l1_card
            self._visible_order.append(l1.id)
            cell.mount(l1_card)
            # L2 row (its children).
            children = self._run.children_of(l1.id)
            if children:
                l2_row = Horizontal(classes="legion-l2-row")
                cell.mount(l2_row)
                for c in children:
                    l2_cell = Vertical(classes="legion-l2-cell")
                    l2_row.mount(l2_cell)
                    l2_card = _LegionNodeCard(c.id)
                    self._card_by_id[c.id] = l2_card
                    self._visible_order.append(c.id)
                    l2_cell.mount(l2_card)

        # Refresh card contents now that they're mounted.
        if self._selected_id is None and self._visible_order:
            self._selected_id = self._visible_order[0]
        for nid, card in self._card_by_id.items():
            card.update_from(self._run, self._selected_id)
        self._update_detail()
        self._update_status_line()

    def _update_detail(self) -> None:
        try:
            body = self.query_one("#legion-detail-body", Static)
            title = self.query_one("#legion-detail-title", Static)
        except Exception:
            return

        if not self._selected_id:
            title.update("── Selected node ──")
            body.update(Text("(no selection)", style=TXT_DIM))
            return

        node = self._run.nodes.get(self._selected_id)
        if node is None:
            title.update("── Selected node ──")
            body.update(Text("(removed)", style=RUST))
            return

        title.update(Text(f"── [{node.id}]  L{node.depth}  ──", style=f"bold {GOLD_HI}"))
        out = Text()
        out.append("goal:    ", style=TXT_DIM)
        out.append((node.goal or "(none)") + "\n", style=TXT)
        out.append("status:  ", style=TXT_DIM)
        out.append_text(_status_text(node.status.value))
        out.append("\n")
        if node.last_action:
            out.append("last:    ", style=TXT_DIM)
            out.append(node.last_action + "\n", style=TXT)
        if node.parent_id:
            out.append("parent:  ", style=TXT_DIM)
            out.append(node.parent_id + "\n", style=GOLD)
        if node.children:
            out.append("children: ", style=TXT_DIM)
            out.append(", ".join(node.children) + "\n", style=GOLD)
        if node.error:
            out.append("\nerror:\n", style=RUST)
            out.append(node.error + "\n", style=TXT)
        if node.output:
            out.append("\noutput:\n", style=GOLD_DIM)
            # Show up to 2KB of output here; the full text lives in node.output.
            text = node.output if len(node.output) <= 2000 else node.output[:2000] + "\n…(truncated)"
            out.append(text, style=TXT)
        body.update(out)

    # -------- key bindings ----------------------------------------------

    def action_prev_node(self) -> None:
        if not self._visible_order:
            return
        idx = (self._visible_order.index(self._selected_id)
               if self._selected_id in self._visible_order else 0)
        idx = (idx - 1) % len(self._visible_order)
        self._selected_id = self._visible_order[idx]
        for card in self._card_by_id.values():
            card.update_from(self._run, self._selected_id)
        self._update_detail()

    def action_next_node(self) -> None:
        if not self._visible_order:
            return
        idx = (self._visible_order.index(self._selected_id)
               if self._selected_id in self._visible_order else -1)
        idx = (idx + 1) % len(self._visible_order)
        self._selected_id = self._visible_order[idx]
        for card in self._card_by_id.values():
            card.update_from(self._run, self._selected_id)
        self._update_detail()

    def action_dismiss(self) -> None:
        self.dismiss(None)


__all__ = ["LegionTreeScreen"]
