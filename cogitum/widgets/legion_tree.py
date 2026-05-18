"""
cogitum.widgets.legion_tree
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Full-screen modal that visualises a Legion swarm in real time.

Visual model:

  ┌──────────────────── L0  Magos's Order ────────────────────┐
  │                                                            │
  │              <root_goal>                                   │
  │                                                            │
  └────────────────────────────┬───────────────────────────────┘
                               │
        ┌──────────────────────┼──────────────────────┐
        │                      │                      │
   ┌── L1 ──┐             ┌── L1 ──┐             ┌── L1 ──┐
   │ alpha  │             │ beta   │             │ gamma  │
   │ ◆ run  │             │ ⏳ wait │             │ ✓ done │
   └────┬───┘             └────────┘             └────────┘
        │
   ┌────┴────┐
   │         │
 ┌─L2─┐   ┌─L2─┐
 │a.1 │   │a.2 │
 │ ◆  │   │ ✓  │
 └────┘   └────┘

  ── alpha · L1 · ◆ running ──────────────────────────────────
   goal      Find all references to deprecated_fn
   last      turn 3/12
   parent    L0
   children  alpha.1, alpha.2

   output
     <up to 2KB>

Three render layers, each rebuilt on every poll tick:

  1. Tree section (top): L0 hero card, gold ┬ trunk, L1 cards
     spread along a centered row joined by a ─ branch line, L2
     children stacked vertically under their L1 with ┬├└ runners.
  2. Detail section (bottom): the currently selected node, with
     a gold rule, label/value rows in fixed-width columns, and
     a separately scrolling output pane.
  3. Footer: status counter + key hints.

Layout uses pure Static widgets with rich Text — no Tree() or
DataTable widget. This keeps the look consistent across Windows
Terminal / cmd.exe / posix terms (we already validated that route
in design.py with the unicode→ASCII fallback).

Subscribes to ``LegionRun.events`` AND polls run.nodes every 0.5s
so a screen opened mid-run still self-heals.
"""
from __future__ import annotations

import asyncio
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


def _status_text(status: str, *, bold: bool = False) -> Text:
    glyph, colour = _STATUS_GLYPH.get(status, ("?", TXT_DIM))
    style = f"bold {colour}" if bold else colour
    return Text(f"{glyph} {status}", style=style)


def _trim(text: str, n: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


# ─────────────────────────────────────────────────────────────────────────
# Per-node card widgets
# ─────────────────────────────────────────────────────────────────────────


class _NodeCard(Static):
    """Generic cogitator card with depth-aware styling.

    L0 (root):  wide hero card with a brighter gold border, used once at the
                top to anchor the tree visually
    L1:         standard bronze-bordered card, mid-width
    L2:         compact muted-bordered card, narrow

    All three accept the same update_from(run, selected_id) refresh.
    """

    DEFAULT_CSS = """
    _NodeCard.l0 {
        width: 56;
        height: 5;
        padding: 0 2;
        background: #161618;
        border: round #F5C24A;
        color: #E6E1CF;
    }
    _NodeCard.l1 {
        width: 30;
        height: 6;
        padding: 0 1;
        background: #161618;
        border: round #A8732D;
        color: #E6E1CF;
    }
    _NodeCard.l2 {
        width: 22;
        height: 5;
        padding: 0 1;
        background: #1A1A1D;
        border: round #5A5648;
        color: #C8C2A8;
    }
    _NodeCard.selected {
        border: heavy #F5C24A;
    }
    """

    def __init__(self, node_id: str, depth: int, **kw) -> None:
        # node_id may be the special string "L0" for the root card.
        self._node_id = node_id
        self._depth = depth          # 0 / 1 / 2
        super().__init__("", **kw)
        if depth == 0:
            self.add_class("l0")
        elif depth == 1:
            self.add_class("l1")
        else:
            self.add_class("l2")

    def update_from(self, run: LegionRun, selected_id: str | None) -> None:
        out = Text()
        if self._depth == 0:
            out.append("⚔ MAGOS'S ORDER\n", style=f"bold {GOLD_HI}")
            out.append(_trim(run.root_goal, 100), style=TXT)
        else:
            node = run.nodes.get(self._node_id)
            if node is None:
                out.append("(missing)", style=RUST)
            else:
                # Header line: id + level tag.
                tag = "L1" if self._depth == 1 else "L2"
                title_style = f"bold {GOLD}" if self._depth == 1 else f"bold {COPPER}"
                out.append(f"{tag} · {node.id}\n", style=title_style)
                # Goal — width-trimmed for the card size.
                goal_w = 26 if self._depth == 1 else 18
                out.append(_trim(node.goal, goal_w) + "\n", style=TXT)
                # Status row.
                out.append_text(_status_text(node.status.value))
                # Last action (only on L1 — L2 too narrow for it).
                if self._depth == 1 and node.last_action:
                    out.append("\n")
                    out.append(_trim(node.last_action, 26), style=TXT_DIM)
        if self._node_id == (selected_id or ""):
            self.add_class("selected")
        else:
            self.remove_class("selected")
        self.update(out)


class _ConnectorRow(Static):
    """Pure-text horizontal/vertical box-drawing line. Used to visually
    join L0→L1 and L1→L2 levels."""

    DEFAULT_CSS = """
    _ConnectorRow {
        height: 1;
        width: 100%;
        text-align: center;
        color: #A8732D;
    }
    """

    def __init__(self, glyphs: str, **kw) -> None:
        super().__init__(glyphs, **kw)


# ─────────────────────────────────────────────────────────────────────────
# The screen
# ─────────────────────────────────────────────────────────────────────────


class LegionTreeScreen(ModalScreen[None]):
    """Full-screen modal: live tree + detail pane for a Legion run."""

    DEFAULT_CSS = """
    LegionTreeScreen { background: #0E0E11; }
    #legion-shell { width: 100%; height: 100%; padding: 1 2; }

    /* Header strip */
    #legion-title  { color: #F5C24A; text-style: bold; height: 1; }
    #legion-status { color: #9C957D; height: 1; padding-bottom: 1; }

    /* Tree section */
    #legion-tree-area { height: auto; }

    #legion-l0-row { height: auto; align: center middle; padding: 1 0; }

    /* L1 row: each L1 + its L2 stack lives in a vertical column,
       columns spread horizontally and centered as a group. */
    #legion-l1-row {
        height: auto;
        layout: horizontal;
        align: center top;
    }
    .l1-column {
        layout: vertical;
        align: center top;
        padding: 0 1;
        height: auto;
        width: auto;
    }
    .l2-stack {
        layout: vertical;
        align: center top;
        height: auto;
        width: auto;
    }

    /* Detail pane */
    #legion-detail {
        height: 1fr;
        background: #161618;
        border: round #2A2620;
        padding: 1 2;
        margin-top: 1;
    }
    #legion-detail-title { color: #F5C24A; text-style: bold; height: 1; }
    #legion-detail-body { color: #E6E1CF; padding-top: 1; }

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
        # Visible nodes in selection order: L0 first, then depth-first
        # L1 → its L2 children → next L1.
        self._visible_order: list[str] = []
        self._selected_id: str | None = None
        self._card_by_id: dict[str, _NodeCard] = {}
        self._sub_task: asyncio.Task | None = None
        self._tick_task: asyncio.Task | None = None

    # -------- compose ----------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="legion-shell"):
            yield Static(f"⚔ LEGION  {self._run.run_id}", id="legion-title")
            yield Static(self._status_line(), id="legion-status")
            yield VerticalScroll(id="legion-tree-area")
            with Vertical(id="legion-detail"):
                yield Static("── Selected node ──", id="legion-detail-title")
                yield Static("", id="legion-detail-body")
            yield Static(
                "Esc close  ·  ↑↓ select node  ·  click card to select",
                id="legion-foot",
            )

    async def on_mount(self) -> None:
        # Initial render — uses run.nodes directly so a screen opened
        # AFTER the swarm started still shows the existing nodes.
        self._refresh_full_tree()
        # Live subscription (real-time) + 0.5s self-heal poll.
        self._sub_task = asyncio.create_task(self._subscribe())
        self._tick_task = asyncio.create_task(self._tick_refresh())

    async def on_unmount(self) -> None:
        for t in (self._sub_task, self._tick_task):
            if t and not t.done():
                t.cancel()

    # -------- subscription ----------------------------------------------

    async def _subscribe(self) -> None:
        try:
            async for ev in self._run.events_iter():
                self._handle_event(ev)
                if ev.kind == "run_done":
                    break
        except Exception:
            pass

    def _handle_event(self, ev: LegionEvent) -> None:
        nid = ev.payload.get("node_id")
        if ev.kind in ("node_status", "node_token") and nid in self._card_by_id:
            self._card_by_id[nid].update_from(self._run, self._selected_id)
            if nid == self._selected_id:
                self._update_detail()
            self._update_status_line()
            return
        if ev.kind == "node_added":
            self._refresh_full_tree()
            return
        if ev.kind == "run_done":
            self._update_status_line()
            return

    async def _tick_refresh(self) -> None:
        """Cheap 0.5s poll over run.nodes — self-heal in case events are
        lost (queue full, dropped before subscription, etc.)."""
        try:
            while True:
                await asyncio.sleep(0.5)
                # Detect tree-shape change before card content change so
                # newly-spawned L2 nodes get materialised first.
                if any(nid not in self._card_by_id for nid in self._run.nodes):
                    self._refresh_full_tree()
                else:
                    for card in self._card_by_id.values():
                        card.update_from(self._run, self._selected_id)
                    self._update_detail()
                    self._update_status_line()
                if self._run.is_complete():
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    # -------- header / status ------------------------------------------

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

    # -------- tree rendering --------------------------------------------

    def _refresh_full_tree(self) -> None:
        """Drop and rebuild the tree section.

        Section ordering: L0 hero → trunk connector → L1 row, where each
        L1 column holds L1 card + (optional) connector + L2 stack.
        """
        try:
            area = self.query_one("#legion-tree-area", VerticalScroll)
        except Exception:
            return

        # Clear everything.
        for child in list(area.children):
            child.remove()
        self._card_by_id = {}
        self._visible_order = []

        # ── L0 hero ────────────────────────────────────────────────
        l0_row = Horizontal(id="legion-l0-row")
        area.mount(l0_row)
        l0_card = _NodeCard("L0", depth=0)
        self._card_by_id["L0"] = l0_card
        self._visible_order.append("L0")
        l0_row.mount(l0_card)

        l1_nodes = self._run.l1_nodes()
        if not l1_nodes:
            self._set_selection_default()
            self._render_all_cards()
            return

        # ── Trunk + branch ────────────────────────────────────────
        # Vertical drop from L0, then a horizontal branch matching
        # the number of L1 columns. The branch glyph builds itself
        # from L1 count: ┌─┴─┬─┴─┐ etc.
        area.mount(_ConnectorRow("│"))
        area.mount(_ConnectorRow(_branch_line(len(l1_nodes))))

        # ── L1 row + L2 stacks ────────────────────────────────────
        l1_row = Horizontal(id="legion-l1-row")
        area.mount(l1_row)

        for l1 in l1_nodes:
            col = Vertical(classes="l1-column")
            l1_row.mount(col)

            l1_card = _NodeCard(l1.id, depth=1)
            self._card_by_id[l1.id] = l1_card
            self._visible_order.append(l1.id)
            col.mount(l1_card)

            children = self._run.children_of(l1.id)
            if children:
                # Trunk drop from L1 + L2 branch sized to children count.
                col.mount(_ConnectorRow("│"))
                col.mount(_ConnectorRow(_branch_line(len(children), narrow=True)))

                stack = Vertical(classes="l2-stack")
                col.mount(stack)
                # Lay L2 children horizontally so the branch line
                # connects properly. Each L2 card is narrower than L1.
                l2_row = Horizontal()
                stack.mount(l2_row)
                for c in children:
                    l2_card = _NodeCard(c.id, depth=2)
                    self._card_by_id[c.id] = l2_card
                    self._visible_order.append(c.id)
                    l2_row.mount(l2_card)

        self._set_selection_default()
        self._render_all_cards()
        self._update_detail()
        self._update_status_line()

    def _render_all_cards(self) -> None:
        for card in self._card_by_id.values():
            card.update_from(self._run, self._selected_id)

    def _set_selection_default(self) -> None:
        """Pick a sensible default when current selection is gone."""
        if self._selected_id in self._visible_order:
            return
        # Prefer the first L1 node, fall back to L0, then nothing.
        for nid in self._visible_order:
            if nid != "L0":
                self._selected_id = nid
                return
        self._selected_id = self._visible_order[0] if self._visible_order else None

    # -------- detail pane -----------------------------------------------

    def _update_detail(self) -> None:
        try:
            body = self.query_one("#legion-detail-body", Static)
            title = self.query_one("#legion-detail-title", Static)
        except Exception:
            return

        if not self._selected_id:
            title.update(Text("── Selected node ──", style=f"bold {GOLD_DIM}"))
            body.update(Text("(no selection)", style=TXT_DIM))
            return

        # Special case: the L0 hero card.
        if self._selected_id == "L0":
            title.update(Text("── L0  Magos's Order ──", style=f"bold {GOLD_HI}"))
            out = Text()
            out.append("goal\n", style=f"bold {GOLD}")
            out.append("  " + (self._run.root_goal or "(none)") + "\n", style=TXT)
            out.append("\n")
            out.append("legion run\n", style=f"bold {GOLD}")
            out.append("  " + self._run.run_id + "\n", style=TXT_DIM)
            body.update(out)
            return

        node = self._run.nodes.get(self._selected_id)
        if node is None:
            title.update(Text("── (removed) ──", style=f"bold {RUST}"))
            body.update(Text("This node no longer exists in the run.", style=RUST))
            return

        # Title: id, level, status (status is what changes most — colour it).
        ttitle = Text()
        ttitle.append(f"── {node.id}", style=f"bold {GOLD_HI}")
        ttitle.append(f"  ·  L{node.depth}", style=GOLD)
        ttitle.append("  ·  ")
        ttitle.append_text(_status_text(node.status.value, bold=True))
        ttitle.append(" ──", style=GOLD_DIM)
        title.update(ttitle)

        # Body: aligned key/value rows.
        out = Text()
        _kv(out, "goal", node.goal or "(none)")
        if node.last_action:
            _kv(out, "last", node.last_action)
        _kv(out, "parent", node.parent_id or "L0 (Magos)")
        if node.children:
            _kv(out, "children", ", ".join(node.children))

        if node.error:
            out.append("\n")
            out.append("error\n", style=f"bold {RUST}")
            for line in node.error.splitlines() or [node.error]:
                out.append("  " + line + "\n", style=TXT)

        if node.output:
            out.append("\n")
            out.append("output\n", style=f"bold {GOLD}")
            text = node.output if len(node.output) <= 2000 else node.output[:2000] + "\n…(truncated)"
            for line in text.splitlines() or [text]:
                out.append("  " + line + "\n", style=TXT)
        body.update(out)

    # -------- key bindings ---------------------------------------------

    def action_prev_node(self) -> None:
        if not self._visible_order:
            return
        idx = (self._visible_order.index(self._selected_id)
               if self._selected_id in self._visible_order else 0)
        idx = (idx - 1) % len(self._visible_order)
        self._selected_id = self._visible_order[idx]
        self._render_all_cards()
        self._update_detail()

    def action_next_node(self) -> None:
        if not self._visible_order:
            return
        idx = (self._visible_order.index(self._selected_id)
               if self._selected_id in self._visible_order else -1)
        idx = (idx + 1) % len(self._visible_order)
        self._selected_id = self._visible_order[idx]
        self._render_all_cards()
        self._update_detail()

    def action_dismiss(self) -> None:
        self.dismiss(None)


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────


def _branch_line(n: int, *, narrow: bool = False) -> str:
    """Build a horizontal branch glyph that visually feeds N children
    from one parent above it.

    n=1:   '│'                 (no spread, just a vertical drop)
    n=2:   '┌─┴─┐'
    n=3:   '┌──┬──┴──┬──┐'
    n=N:   ┌ then alternating ─/┬ pattern with the centre as ┴.

    ``narrow=True`` shortens segment width so L2 connector rows fit
    above L2 cards without being too sparse.
    """
    if n <= 0:
        return ""
    if n == 1:
        return "│"
    seg_w = 4 if narrow else 7
    seg = "─" * seg_w

    parts: list[str] = ["┌"]
    centre = (n - 1) / 2
    for i in range(n - 1):
        parts.append(seg)
        # Insert ┴ at the centre (parent join), ┬ elsewhere.
        if i == int(centre) and (n % 2 == 1):
            parts.append("┴")
        elif (i == int(centre) - 1 or i == int(centre)) and (n % 2 == 0) \
                and parts.count("┴") == 0 and i + 1 == int(centre + 0.5):
            parts.append("┴")
        else:
            parts.append("┬")
    parts.append(seg)
    parts.append("┐")

    # If we never inserted ┴ (even-N edge), force one at the middle.
    line = "".join(parts)
    if "┴" not in line:
        # Replace the middle ┬ with ┴.
        idx = len(line) // 2
        # Find the closest ┬ to idx.
        best = min(
            (i for i, ch in enumerate(line) if ch == "┬"),
            key=lambda i: abs(i - idx),
            default=None,
        )
        if best is not None:
            line = line[:best] + "┴" + line[best + 1 :]
    return line


def _kv(out: Text, key: str, value: str) -> None:
    """Append a `key   value` row to the detail body Text.

    Key is fixed-width (10 chars) and dim; value is parchment-on-card
    primary text and gets the line break.
    """
    out.append(f"  {key:<10}", style=TXT_DIM)
    out.append(value + "\n", style=TXT)


__all__ = ["LegionTreeScreen"]
