"""Inspector pane — neutral tech terminology, warm palette.

Right-hand column. Sections, top to bottom:
  · MODEL    — model name, provider, context use
  · AGENTS   — active subagents / running tasks
  · SKILLS   — loaded skills
  · PROJECT  — workdir, git branch, dirty files
  · USAGE    — token + cost meter, elapsed time
"""
from __future__ import annotations

from dataclasses import dataclass, field

from rich.console import Group
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from textual.widget import Widget

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
    TXT,
    TXT_DIM,
)


@dataclass
class InspectorState:
    model: str = "claude-opus-4.7"
    provider: str = "custom · kr"
    context_used_pct: float = 0.18
    agents: list[tuple[str, str]] = field(
        default_factory=lambda: [
            ("scribe",   "running"),
            ("analyst",  "idle"),
            ("redteam",  "idle"),
            ("auditor",  "idle"),
        ]
    )
    skills: list[str] = field(
        default_factory=lambda: [
            "writing-plans",
            "subagent-driven-development",
            "humanity-mode",
            "godmode",
        ]
    )
    workdir: str = "~/Cogitum"
    branch: str = "main"
    dirty: int = 6
    tokens_in: int = 14_812
    tokens_out: int = 2_476
    elapsed: str = "00:04:31"


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


class Inspector(Widget):
    def __init__(self, state: InspectorState | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = state or InspectorState()

    def render(self):
        st = self.state
        rows: list = []

        # ── MODEL ────────────────────────────────────────────────────────
        rows.append(_h("MODEL"))
        rows.append(Text(""))
        rows.append(_kv("name",     st.model,    f"bold {GOLD_HI}"))
        rows.append(_kv("provider", st.provider, TXT_DIM))
        ctx = Table.grid(expand=True, padding=(0, 0))
        ctx.add_column(ratio=1); ctx.add_column(ratio=2, justify="right")
        ctx.add_row(Text("context", style=GOLD_DIM), _bar(st.context_used_pct))
        rows.append(ctx)
        rows.append(
            Text.assemble(
                ("            ", ""),
                (f"{int(st.context_used_pct*100)} %", TXT_DIM),
            )
        )
        rows.append(Rule(style=RULE))

        # ── AGENTS ───────────────────────────────────────────────────────
        rows.append(_h("AGENTS"))
        rows.append(Text(""))
        ag = Table.grid(expand=True, padding=(0, 0))
        ag.add_column(ratio=2); ag.add_column(ratio=1, justify="right")
        for name, status in st.agents:
            if status == "running":
                left = Text()
                left.append("● ", style=GOLD_HI)
                left.append(name, style=TXT)
                right = Text("running", style=GOLD_HI)
            else:
                left = Text()
                left.append("○ ", style=OLIVE)
                left.append(name, style=TXT_DIM)
                right = Text("idle", style=MUTED)
            ag.add_row(left, right)
        rows.append(ag)
        rows.append(Rule(style=RULE))

        # ── SKILLS ───────────────────────────────────────────────────────
        rows.append(_h("SKILLS"))
        rows.append(Text(""))
        for s in st.skills:
            line = Text()
            line.append(f"  {GLYPH_BULLET} ", style=GOLD_DIM)
            line.append(s, style=TXT)
            rows.append(line)
        rows.append(Text(f"  {len(st.skills)} loaded", style=GOLD_DIM))
        rows.append(Rule(style=RULE))

        # ── PROJECT ──────────────────────────────────────────────────────
        rows.append(_h("PROJECT"))
        rows.append(Text(""))
        rows.append(_kv("path",   st.workdir, TXT))
        rows.append(_kv("branch", st.branch,  GOLD))
        rows.append(_kv(
            "dirty",
            f"{st.dirty} files",
            value_style=GOLD_HI if st.dirty else OK,
        ))
        rows.append(Rule(style=RULE))

        # ── USAGE ────────────────────────────────────────────────────────
        rows.append(_h("USAGE"))
        rows.append(Text(""))
        rows.append(_kv("tokens in",  f"{st.tokens_in:,}",  TXT))
        rows.append(_kv("tokens out", f"{st.tokens_out:,}", TXT))
        rows.append(_kv("elapsed",    st.elapsed,           TXT))

        return Group(*rows)
