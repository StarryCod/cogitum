"""
Model picker modal — `/models` command.

Layout:
  ┌──────────────── Pick a model ─────────────────────────────────┐
  │ search:  k                                          [filters] │
  │ ┌─ list ──────────────────────────────┐ ┌─ details ─────────┐ │
  │ │ canopywave/moonshotai/kimi-k2.6   ◆ │ │ Kimi K2.6         │ │
  │ │ openrouter/x-ai/grok-4              │ │ ctx 200k · out 128k│ │
  │ │ ...                                 │ │ caps: text,vision  │ │
  │ │                                     │ │ pricing: free      │ │
  │ │                                     │ │ keys: 1 active     │ │
  │ └─────────────────────────────────────┘ └────────────────────┘ │
  │ [text] [vision] [reasoning] [tools] [free] [<128k] [≥128k]    │
  └────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Key
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, ListItem, ListView, Static

from ..core.llm.capabilities import Capability
from ..core.llm.mesh import Mesh, ResolvedModel
from ..design import (
    BRONZE,
    COPPER,
    GOLD,
    GOLD_DIM,
    GOLD_HI,
    MUTED,
    OK,
    RUST,
    TXT,
    TXT_DIM,
)


_FILTERS: list[tuple[str, str]] = [
    # (label, capability_name or "free" / "<128k" / "≥128k")
    ("text", "TEXT"),
    ("vision", "VISION"),
    ("reasoning", "REASONING"),
    ("tools", "TOOLS"),
    ("caching", "CACHING"),
    ("free", "free"),
    ("<128k", "<128k"),
    ("≥128k", "LONG_CONTEXT"),
]


# ---------------------------------------------------------------------------
# Item rendering
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class _Entry:
    resolved: ResolvedModel
    score: float = 0.0

    @property
    def display_id(self) -> str:
        return self.resolved.qualified_id

    @property
    def search_haystack(self) -> str:
        m = self.resolved.model
        return " ".join(
            (
                self.resolved.qualified_id,
                m.display,
                " ".join(m.aliases),
                " ".join(m.capabilities.to_strings()),
            )
        ).lower()


def _render_row(entry: _Entry, *, active_keys: int, query: str = "") -> Text:
    m = entry.resolved.model
    line = Text()

    # Provider prefix + model id, with optional query highlight
    prov = entry.resolved.provider.id + "/"
    line.append(prov, style=GOLD_DIM)
    if query and query in m.id.lower():
        # Highlight first match
        idx = m.id.lower().find(query)
        line.append(m.id[:idx], style=GOLD)
        line.append(m.id[idx:idx+len(query)], style=f"bold {GOLD_HI}")
        line.append(m.id[idx+len(query):], style=GOLD)
    else:
        line.append(m.id, style=GOLD)

    line.append("   ", style="")
    line.append(m.display or "—", style=TXT_DIM)

    line.append("   ", style="")
    if Capability.REASONING in m.capabilities:
        line.append("◆ ", style=BRONZE)
    if Capability.VISION in m.capabilities:
        line.append("◇ ", style=BRONZE)
    if Capability.TOOLS in m.capabilities:
        line.append("⚒ ", style=BRONZE)

    line.append(f"  ctx {_human_k(m.context_window):>6}", style=MUTED)

    # key health hint
    if active_keys == 0:
        line.append("   no keys", style=RUST)
    elif active_keys == 1:
        pass
    else:
        line.append(f"   {active_keys} keys", style=OK)
    return line


def _human_k(n: int) -> str:
    if n >= 1_000_000:
        return f"{n // 1_000_000}M"
    if n >= 1000:
        return f"{n // 1000}k"
    return str(n)


# ---------------------------------------------------------------------------
# Picker
# ---------------------------------------------------------------------------

class ModelPicker(ModalScreen[ResolvedModel | None]):
    """Modal screen presenting all mesh models, with fuzzy filter and detail pane."""

    DEFAULT_CSS = """
    ModelPicker {
        align: center middle;
        background: rgba(0, 0, 0, 0.55);
    }
    #picker-shell {
        width: 84%;
        max-width: 130;
        height: 80%;
        background: #161618;
        border: round #7A5A1A;
        padding: 1 2;
        overflow-y: auto;
        scrollbar-size-vertical: 1;
        scrollbar-color: #161618;
        scrollbar-background: #161618;
        scrollbar-background-active: #161618;
        scrollbar-background-hover: #161618;
        scrollbar-color-active: #161618;
        scrollbar-color-hover: #161618;
    }
    #picker-title {
        height: 1;
        color: #F5C24A;
        text-style: bold;
    }
    #picker-search {
        height: 3;
        background: #1C1C1F;
        border: round #2A2620;
        color: #E6E1CF;
        padding: 0 1;
        margin: 1 0 1 0;
    }
    #picker-search:focus {
        border: round #A8732D;
    }
    #picker-body {
        height: 1fr;
    }
    #picker-list {
        width: 2fr;
        background: #0E0E11;
        border: round #2A2620;
        padding: 0 1;
        scrollbar-size-vertical: 1;
        scrollbar-color: #0E0E11;
        scrollbar-background: #0E0E11;
        scrollbar-background-active: #0E0E11;
        scrollbar-background-hover: #0E0E11;
        scrollbar-color-active: #0E0E11;
        scrollbar-color-hover: #0E0E11;
    }
    #picker-list > ListItem.--highlight,
    #picker-list > ListItem:hover {
        background: #261E10;
    }
    #picker-detail-scroll {
        width: 1fr;
        min-width: 32;
        background: #1C1C1F;
        border: round #2A2620;
        padding: 1 2;
        margin-left: 1;
        overflow-y: auto;
        scrollbar-size-vertical: 1;
        scrollbar-color: #1C1C1F;
        scrollbar-background: #1C1C1F;
        scrollbar-color-active: #1C1C1F;
        scrollbar-color-hover: #1C1C1F;
        scrollbar-background-active: #1C1C1F;
        scrollbar-background-hover: #1C1C1F;
    }
    #picker-detail {
        height: auto;
    }
    #picker-filters {
        height: 1;
        margin-top: 1;
        color: #9C957D;
    }
    #picker-status {
        height: 1;
        color: #7A5A1A;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "close"),
        Binding("enter", "select", "select"),
        Binding("tab", "cycle_filter", "filter", show=False),
        Binding("shift+tab", "cycle_filter_back", "filter back", show=False),
        Binding("ctrl+t", "toggle_filter", "toggle"),
    ]

    class Picked(Message):
        def __init__(self, resolved: ResolvedModel) -> None:
            super().__init__()
            self.resolved = resolved

    def __init__(
        self,
        mesh: Mesh,
        *,
        current: str | None = None,
    ) -> None:
        super().__init__()
        self._mesh = mesh
        self._all = [_Entry(r) for r in mesh.list_resolved()]
        self._current = current
        self._filtered: list[_Entry] = list(self._all)
        self._active_filter_idx = 0
        self._enabled_filters: set[str] = set()

    # ------------------------------------------------------------------
    # compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-shell"):
            yield Static("Pick a model — type to filter, Enter to choose, Esc to cancel", id="picker-title")
            yield Input(placeholder="search by id / alias / provider", id="picker-search")
            with Horizontal(id="picker-body"):
                yield ListView(id="picker-list")
                with VerticalScroll(id="picker-detail-scroll"):
                    yield Static("", id="picker-detail")
            yield Static(self._render_filters(), id="picker-filters")
            yield Static("", id="picker-status")

    def on_mount(self) -> None:
        self._rebuild_list()
        list_view = self.query_one("#picker-list", ListView)
        if self._current:
            for i, e in enumerate(self._filtered):
                if e.display_id == self._current:
                    list_view.index = i
                    break
        list_view.focus()
        self._update_detail()
        self._update_status()

    # ------------------------------------------------------------------
    # search + filter
    # ------------------------------------------------------------------

    @on(Input.Changed, "#picker-search")
    def _on_search_change(self, event: Input.Changed) -> None:
        self._rebuild_list(query=event.value)

    def _rebuild_list(self, query: str = "") -> None:
        items = [e for e in self._all if self._passes_filters(e)]

        q = query.lower().strip()
        if q:
            # 1) Substring matches first (anywhere in haystack) — these are
            #    the user's "I know what I want" hits and must always show.
            substring_hits = [e for e in items if q in e.search_haystack]
            substring_ids = {e.display_id for e in substring_hits}

            # 2) Try fuzzy on the rest, more permissive threshold
            fuzzy_hits: list[_Entry] = []
            try:
                from rapidfuzz import fuzz
                for e in items:
                    if e.display_id in substring_ids:
                        continue
                    # token_set_ratio handles word-order and partial tokens
                    score = max(
                        fuzz.partial_ratio(q, e.search_haystack),
                        fuzz.token_set_ratio(q, e.search_haystack),
                    )
                    e.score = score
                    if score >= 65:
                        fuzzy_hits.append(e)
                fuzzy_hits.sort(key=lambda e: e.score, reverse=True)
            except ImportError:
                pass

            # 3) Boost: id starts with query > display starts with query > rest
            def _rank(e: _Entry) -> tuple[int, int, str]:
                m = e.resolved.model
                qid = e.display_id.lower()
                disp = (m.display or "").lower()
                if qid.startswith(q):
                    pri = 0
                elif m.id.lower().startswith(q):
                    pri = 1
                elif disp.startswith(q):
                    pri = 2
                else:
                    pri = 3
                return (pri, -len(qid), qid)

            substring_hits.sort(key=_rank)
            items = substring_hits + fuzzy_hits
        else:
            items.sort(key=lambda e: (e.resolved.provider.id, e.resolved.model.id))

        self._filtered = items
        list_view = self.query_one("#picker-list", ListView)
        # Remove all children synchronously to avoid DuplicateIds
        for child in list(list_view.children):
            child.remove()
        self._rebuild_seq = getattr(self, "_rebuild_seq", 0) + 1
        seq = self._rebuild_seq
        for i, e in enumerate(items):
            active_keys = e.resolved.provider.pool.active_count
            row = _render_row(e, active_keys=active_keys, query=q)
            list_view.mount(ListItem(Static(row), id=f"item-{seq}-{i}"))
        if items:
            list_view.index = 0
        self._update_detail()
        self._update_status()

    def _passes_filters(self, entry: _Entry) -> bool:
        if not self._enabled_filters:
            return True
        m = entry.resolved.model
        for f in self._enabled_filters:
            if f == "free":
                if m.cost_input > 0 or m.cost_output > 0:
                    return False
            elif f == "<128k":
                if m.context_window >= 128_000:
                    return False
            else:
                cap = Capability.__members__.get(f)
                if cap is None or cap not in m.capabilities:
                    return False
        return True

    def _render_filters(self) -> Text:
        out = Text()
        for i, (label, key) in enumerate(_FILTERS):
            on = key in self._enabled_filters
            if i == self._active_filter_idx:
                out.append(f"[{label}]", style=f"bold {GOLD_HI}" if on else GOLD)
            else:
                out.append(f" {label} ", style=BRONZE if on else TXT_DIM)
            out.append("  ")
        out.append("    ctrl+t toggle · tab cycle", style=MUTED)
        return out

    def _refresh_filter_bar(self) -> None:
        self.query_one("#picker-filters", Static).update(self._render_filters())

    def action_cycle_filter(self) -> None:
        self._active_filter_idx = (self._active_filter_idx + 1) % len(_FILTERS)
        self._refresh_filter_bar()

    def action_cycle_filter_back(self) -> None:
        self._active_filter_idx = (self._active_filter_idx - 1) % len(_FILTERS)
        self._refresh_filter_bar()

    def action_toggle_filter(self) -> None:
        _, key = _FILTERS[self._active_filter_idx]
        if key in self._enabled_filters:
            self._enabled_filters.remove(key)
        else:
            self._enabled_filters.add(key)
        self._refresh_filter_bar()
        # re-apply with current search input
        q = self.query_one("#picker-search", Input).value
        self._rebuild_list(query=q)

    # ------------------------------------------------------------------
    # selection / detail
    # ------------------------------------------------------------------

    @on(ListView.Highlighted, "#picker-list")
    def _on_highlight(self, event: ListView.Highlighted) -> None:
        self._update_detail()

    @on(ListView.Selected, "#picker-list")
    def _on_selected(self, event: ListView.Selected) -> None:
        self.action_select()

    def _current_entry(self) -> _Entry | None:
        list_view = self.query_one("#picker-list", ListView)
        i = list_view.index
        if i is None or not (0 <= i < len(self._filtered)):
            return None
        return self._filtered[i]

    def _update_detail(self) -> None:
        e = self._current_entry()
        det = self.query_one("#picker-detail", Static)
        if e is None:
            det.update(Text("no models match", style=MUTED))
            return
        m = e.resolved.model
        p = e.resolved.provider
        body = Text()
        body.append(m.display or m.id, style=f"bold {GOLD_HI}")
        body.append("\n")
        body.append(e.display_id, style=GOLD_DIM)
        body.append("\n\n")

        def kv(k: str, v: str, accent: str = TXT) -> None:
            body.append(f"{k:<14}", style=GOLD_DIM)
            body.append(f"{v}\n", style=accent)

        kv("provider", f"{p.name} ({p.config.format})")
        kv("base url", p.config.base_url, TXT_DIM)
        kv("context", _human_k(m.context_window))
        kv("max output", _human_k(m.max_output_tokens))
        caps = ", ".join(m.capabilities.to_strings()) or "—"
        kv("capabilities", caps, BRONZE)

        if m.aliases:
            kv("aliases", ", ".join(m.aliases), TXT_DIM)

        if m.cost_input or m.cost_output:
            kv(
                "cost / 1M",
                f"in ${m.cost_input:.2f}  out ${m.cost_output:.2f}",
                BRONZE,
            )
        else:
            kv("cost", "free / unknown", OK)

        # key health
        body.append("\n")
        body.append("keys\n", style=GOLD)
        snap = p.pool.snapshot()
        if not snap:
            body.append("  (none)\n", style=MUTED)
        else:
            for s in snap:
                style = TXT
                if s["status"] == "rate_limited":
                    style = COPPER
                elif s["status"] == "auth_error":
                    style = RUST
                body.append(
                    f"  {s['id']:<14} {s['status']:<14} req={s['total_requests']} tok={s['total_tokens']}\n",
                    style=style,
                )
        det.update(body)

    def _update_status(self) -> None:
        bar = self.query_one("#picker-status", Static)
        n = len(self._filtered)
        total = len(self._all)
        bar.update(Text(f"{n} / {total} models   ·   ↑↓ nav · / search · enter pick · esc close", style=MUTED))

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------

    def action_select(self) -> None:
        e = self._current_entry()
        if e is None:
            self.dismiss(None)
            return
        self.dismiss(e.resolved)

    def action_cancel(self) -> None:
        self.dismiss(None)

    # Keep typing routed to the Input even when the list is focused.
    def on_key(self, event: Key) -> None:
        list_view = self.query_one("#picker-list", ListView)
        search = self.query_one("#picker-search", Input)
        if event.key == "/" and self.focused is list_view:
            search.focus()
            event.stop()
            return
        # printable single character → focus search and forward
        if (
            self.focused is list_view
            and event.is_printable
            and len(event.character or "") == 1
            and event.key not in ("space", "enter")
        ):
            search.focus()
            search.value = (search.value or "") + (event.character or "")
            search.cursor_position = len(search.value)
            event.stop()


__all__ = ["ModelPicker"]
