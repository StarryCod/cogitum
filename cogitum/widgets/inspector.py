"""Inspector pane — live system state, warm palette.

Right-hand column. Sections, top to bottom:
  · MODEL    — model name, provider, context use
  · TOOLS    — available tools count + list
  · SESSION  — messages count, turns
  · USAGE    — token meter, elapsed time
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from rich.console import Group
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from textual.widgets import Static
from ..design import (
    BRONZE,
    COPPER,
    GLYPH_BAR_EMPTY,
    GLYPH_BAR_FULL,
    GLYPH_BULLET,
    GOLD,
    GOLD_DIM,
    GOLD_HI,
    MUTED,
    OK,
    OLIVE,
    RULE,
    RUST,
    TXT,
    TXT_DIM,
)


@dataclass
class InspectorState:
    model: str = "—"
    provider: str = "—"
    context_window: int = 200_000
    tokens_used: int = 0
    tools: list[str] = field(default_factory=list)
    messages: int = 0
    turns: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    started_at: float = field(default_factory=time.time)
    last_error: str = ""


def _h(name: str) -> Text:
    return Text(name, style=f"bold {GOLD}")


def _kv(label: str, value: str, value_style: str = TXT) -> Table:
    tbl = Table.grid(expand=True, padding=(0, 0))
    tbl.add_column(ratio=1, justify="left")
    tbl.add_column(ratio=2, justify="right")
    tbl.add_row(
        Text(label, style=GOLD_DIM),
        Text(value, style=value_style),
    )
    return tbl


def _bar(pct: float, width: int = 18, full: str = GOLD, empty: str = MUTED) -> Text:
    n = max(0, min(width, int(round(pct * width))))
    t = Text()
    t.append(GLYPH_BAR_FULL * n, style=full)
    t.append(GLYPH_BAR_EMPTY * (width - n), style=empty)
    return t


class Inspector(Static):
    def __init__(self, state: InspectorState | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = state or InspectorState()

    def update_state(self, **kwargs) -> None:
        """Update state fields and refresh."""
        for k, v in kwargs.items():
            if hasattr(self.state, k):
                setattr(self.state, k, v)
        self.refresh()

    def render(self):
        st = self.state
        rows: list = []

        # ── MODEL ────────────────────────────────────────────────────────
        rows.append(_h("MODEL"))
        rows.append(Text(""))
        rows.append(_kv("name",     st.model,    f"bold {GOLD_HI}"))
        rows.append(_kv("provider", st.provider, TXT_DIM))
        pct = st.tokens_used / max(st.context_window, 1)
        ctx = Table.grid(expand=True, padding=(0, 0))
        ctx.add_column(ratio=1); ctx.add_column(ratio=2, justify="right")
        ctx.add_row(Text("context", style=GOLD_DIM), _bar(pct))
        rows.append(ctx)
        rows.append(
            Text.assemble(
                ("            ", ""),
                (f"{int(pct*100)}% of {st.context_window//1000}k", TXT_DIM),
            )
        )
        rows.append(Rule(style=RULE))

        # ── TOOLS ──────────────────────────────────────────────────────
        rows.append(_h("TOOLS"))
        rows.append(Text(""))
        if st.tools:
            for t in st.tools[:8]:
                line = Text()
                line.append(f"  {GLYPH_BULLET} ", style=GOLD_DIM)
                line.append(t, style=TXT)
                rows.append(line)
            if len(st.tools) > 8:
                rows.append(Text(f"  … +{len(st.tools) - 8} more", style=MUTED))
            rows.append(Text(f"  {len(st.tools)} available", style=GOLD_DIM))
        else:
            rows.append(Text("  none loaded", style=MUTED))
        rows.append(Rule(style=RULE))

        # ── SESSION ────────────────────────────────────────────────────
        rows.append(_h("SESSION"))
        rows.append(Text(""))
        rows.append(_kv("messages", str(st.messages), TXT))
        rows.append(_kv("turns",    str(st.turns),    TXT))
        elapsed = time.time() - st.started_at
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        rows.append(_kv("elapsed",  f"{mins:02d}:{secs:02d}", TXT))
        rows.append(Rule(style=RULE))

        # ── USAGE ──────────────────────────────────────────────────────
        rows.append(_h("USAGE"))
        rows.append(Text(""))
        rows.append(_kv("tokens in",  f"{st.tokens_in:,}",  TXT))
        rows.append(_kv("tokens out", f"{st.tokens_out:,}", TXT))
        total = st.tokens_in + st.tokens_out
        rows.append(_kv("total",      f"{total:,}",         f"bold {GOLD_HI}"))

        # ── ERROR ──────────────────────────────────────────────────────
        if st.last_error:
            rows.append(Rule(style=RULE))
            rows.append(_h("LAST ERROR"))
            rows.append(Text(""))
            err_text = st.last_error[:80]
            rows.append(Text(f"  {err_text}", style=RUST))

        return Group(*rows)
