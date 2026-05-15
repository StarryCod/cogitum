"""Specialised tool-call cards.

Each card is a UI block, not a log line. Common chrome:

  ┌─ TYPE  primary identifier                            footer ─┐
  │ body…                                                         │
  └────────────────────────────────────────────────────────────────┘

Type-specific bodies:

  EditCard    — colourised diff (+ green, − rust)
  RunCard     — command + truncated output + exit code in footer
  SearchCard  — pattern in header + truncated path list
  SwarmCard   — subagent goal + bullet trace
  ReadCard    — compact one-line: path + size/lines
  FetchCard   — compact one-line: url + status

Everything in warm palette only.
"""
from __future__ import annotations

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text
from textual.widgets import Static

from ..design import (
    BRONZE,
    COPPER,
    GOLD,
    GOLD_DIM,
    GOLD_HI,
    MUTED,
    OK,
    OLIVE,
    RUST,
    SURFACE,
    TXT,
    TXT_DIM,
)


# ── header / footer plumbing ────────────────────────────────────────────────

def _title(kind: str, primary: str, footer: str = "", footer_style: str = OK) -> Text:
    """Compose '┄ KIND  primary' + right-side footer.

    Rich Panel handles the actual border drawing; we just supply text.
    """
    t = Text()
    t.append(f" {kind} ", style=f"bold {COPPER} on default")
    t.append("  ")
    t.append(primary, style=f"bold {TXT}")
    if footer:
        # spacer pushed to the right at render time via title_align/subtitle
        pass
    return t


def _panel(title: Text, body: RenderableType, subtitle: Text | None = None,
           border_style: str = COPPER) -> Panel:
    return Panel(
        body,
        title=title,
        title_align="left",
        subtitle=subtitle,
        subtitle_align="right",
        border_style=border_style,
        padding=(0, 1),
        expand=True,
    )


# ── EDIT (write_file / patch) ───────────────────────────────────────────────

class EditCard(Static):
    DEFAULT_CLASSES = "card card-edit"

    def __init__(self, path: str, diff: list[tuple[str, str]],
                 plus: int = 0, minus: int = 0, **kw) -> None:
        super().__init__(self._build(path, diff, plus, minus), **kw)

    @staticmethod
    def _build(path: str, diff: list[tuple[str, str]], plus: int, minus: int) -> Panel:
        body = Text()
        for sigil, line in diff:
            if sigil == "+":
                body.append("+ ", style=f"bold {OK}")
                body.append(line + "\n", style=OK)
            elif sigil == "-":
                body.append("- ", style=f"bold {RUST}")
                body.append(line + "\n", style=RUST)
            else:
                body.append("  " + line + "\n", style=TXT_DIM)
        if body.plain.endswith("\n"):
            body.right_crop(1)
        title = _title("EDIT", path)
        sub = Text()
        sub.append(f"+{plus} ", style=OK)
        sub.append(f"−{minus}", style=RUST)
        return _panel(title, body, sub, border_style=BRONZE)




# ── WRITE (write_file) ─────────────────────────────────────────────────────

class WriteCard(Static):
    DEFAULT_CLASSES = "card card-write"

    def __init__(self, path: str, lines: int, size: str = "", **kw) -> None:
        super().__init__(self._build(path, lines, size), **kw)

    @staticmethod
    def _build(path: str, lines: int, size: str) -> Panel:
        body = Text()
        body.append("  ", style=GOLD_DIM)
        body.append(path, style=TXT)
        title = _title("WRITE", path.split("/")[-1] if "/" in path else path)
        sub = Text(f"{lines} lines  ·  {size}" if size else f"{lines} lines",
                   style=GOLD_DIM)
        return _panel(title, body, sub, border_style=OK)

# ── RUN (terminal) ──────────────────────────────────────────────────────────

class RunCard(Static):
    DEFAULT_CLASSES = "card card-run"

    def __init__(self, cmd: str, output: str = "", exit_code: int = 0,
                 duration: str = "", **kw) -> None:
        super().__init__(self._build(cmd, output, exit_code, duration), **kw)

    @staticmethod
    def _build(cmd: str, output: str, exit_code: int, duration: str) -> Panel:
        body = Text()
        body.append("$ ", style=GOLD_DIM)
        body.append(cmd + "\n", style=f"bold {GOLD_HI}")
        body.append("\n")
        for line in (output or "").rstrip("\n").splitlines():
            body.append(line + "\n", style=TXT_DIM)
        if body.plain.endswith("\n"):
            body.right_crop(1)
        title = _title("RUN", cmd.split()[0] if cmd else "shell")
        sub = Text()
        if duration:
            sub.append(duration, style=GOLD_DIM)
            sub.append("   ")
        if exit_code == 0:
            sub.append("exit 0", style=OK)
        else:
            sub.append(f"exit {exit_code}", style=RUST)
        return _panel(title, body, sub, border_style=BRONZE)


# ── SEARCH ──────────────────────────────────────────────────────────────────

class SearchCard(Static):
    DEFAULT_CLASSES = "card card-search"

    def __init__(self, pattern: str, hits: list[str], total: int, **kw) -> None:
        super().__init__(self._build(pattern, hits, total), **kw)

    @staticmethod
    def _build(pattern: str, hits: list[str], total: int) -> Panel:
        body = Text()
        for h in hits[:5]:
            body.append("  ", style=GOLD_DIM)
            body.append(h + "\n", style=TXT)
        if total > len(hits[:5]):
            body.append(f"  · {total - 5} more\n", style=GOLD_DIM)
        if body.plain.endswith("\n"):
            body.right_crop(1)
        title = _title("SEARCH", f'"{pattern}"')
        sub = Text(f"{total} hits", style=GOLD)
        return _panel(title, body, sub, border_style=COPPER)


# ── SWARM (delegate_task) ───────────────────────────────────────────────────

class SwarmCard(Static):
    DEFAULT_CLASSES = "card card-swarm"

    def __init__(self, agent: str, goal: str, trace: list[str],
                 status: str = "running", **kw) -> None:
        super().__init__(self._build(agent, goal, trace, status), **kw)

    @staticmethod
    def _build(agent: str, goal: str, trace: list[str], status: str) -> Panel:
        body = Text()
        body.append("goal: ", style=GOLD_DIM)
        body.append(goal + "\n", style=TXT)
        body.append("\n")
        for t in trace:
            body.append("  · ", style=GOLD_DIM)
            body.append(t + "\n", style=TXT_DIM)
        if body.plain.endswith("\n"):
            body.right_crop(1)
        title = _title("SWARM", agent)
        col = OK if status == "done" else (GOLD_HI if status == "running" else MUTED)
        sub = Text(status, style=col)
        return _panel(title, body, sub, border_style=BRONZE)


# ── READ / FETCH compact ────────────────────────────────────────────────────

class ReadCard(Static):
    DEFAULT_CLASSES = "card card-read"

    def __init__(self, path: str, lines: int, size: str = "", **kw) -> None:
        super().__init__(self._build(path, lines, size), **kw)

    @staticmethod
    def _build(path: str, lines: int, size: str) -> Panel:
        body = Text()
        body.append("  ", style=GOLD_DIM)
        body.append(path, style=TXT)
        title = _title("READ", "")
        sub = Text(f"{lines} lines  ·  {size}" if size else f"{lines} lines",
                   style=GOLD_DIM)
        return _panel(title, body, sub, border_style=COPPER)


class FetchCard(Static):
    DEFAULT_CLASSES = "card card-fetch"

    def __init__(self, url: str, status: int, size: str = "", **kw) -> None:
        super().__init__(self._build(url, status, size), **kw)

    @staticmethod
    def _build(url: str, status: int, size: str) -> Panel:
        body = Text()
        body.append("  ", style=GOLD_DIM)
        body.append(url, style=TXT)
        title = _title("FETCH", "")
        sub_color = OK if 200 <= status < 300 else RUST
        sub = Text(f"{status}  ·  {size}" if size else str(status), style=sub_color)
        return _panel(title, body, sub, border_style=COPPER)
