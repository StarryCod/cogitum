"""
cogitum.update_flow
~~~~~~~~~~~~~~~~~~~

`cog update` — single-card, full-screen self-update flow.

Layout (one card spanning the entire viewport):

  ┌──────────────── ⚔  COGITUM  UPDATE ────────────────┐
  │                                                     │
  │           current  0.3.0   →   latest  0.4.0        │
  │                                                     │
  │   ████████████████████░░░░░░░░░░░░░░░░░░░  56%      │
  │                                                     │
  │   running ›  Resolving deltas: 100% (4/4), done.    │
  │                                                     │
  │                    [   Close   ]                    │
  └─────────────────────────────────────────────────────┘

The single bottom output line replaces itself on every new line
from `git pull` — feels like a live ticker, no log spam.

Phases the same card walks through:

  1. checking      — probe origin/master, progress 5 %
  2. up-to-date    — green status, single Close button, exit 0
  3. ready         — newer found, [Upgrade now] [Cancel]
  4. upgrading     — progress bar advances on each git stage,
                     ticker line shows current git output
  5. success       — "✓ Restart Cogitum to apply", Close
  6. error         — "✗ <reason>", Close, exit 1

Why one card and one ticker line: a single self-replacing line
forces us to surface only the informative output, and reads
dramatically calmer than a scrolling pane of git noise.

Why standalone Textual app: `cog update` runs in its own process
(no main Cogitum app to mount a modal into).
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical
from textual.widgets import Button, ProgressBar, Static

from .core.update_check import (
    UpdateInfo, _fetch_latest_version, detect_install_method, is_newer,
)
from .design import (
    BG, BG_SOFT, BRONZE, COPPER, GOLD, GOLD_DIM, GOLD_HI,
    MUTED, OK, RUST, TXT, TXT_DIM,
)


# ─────────────────────────────────────────────────────────────────────────
# Update root resolution
# ─────────────────────────────────────────────────────────────────────────


def _find_update_root() -> Path | None:
    """Return the .git checkout root that should be `git pull`-ed."""
    env_home = os.environ.get("COGITUM_HOME")
    if env_home:
        p = Path(env_home)
        if (p / ".git").is_dir():
            return p
    try:
        import cogitum
        start = Path(cogitum.__file__).resolve().parent
    except Exception:
        return None
    for cand in (start, *start.parents):
        if (cand / ".git").is_dir():
            return cand
    return None


# ─────────────────────────────────────────────────────────────────────────
# git output → progress hint
# ─────────────────────────────────────────────────────────────────────────
#
# git porcelain doesn't emit machine-readable progress, but its
# stdout/stderr lines have predictable shapes. We map common stages to
# coarse percentages so the bar advances meaningfully on each line.

_GIT_STAGE_PROGRESS: list[tuple[re.Pattern, int]] = [
    (re.compile(r"^remote:\s*Counting objects"), 30),
    (re.compile(r"^remote:\s*Compressing objects"), 45),
    (re.compile(r"^Receiving objects"), 65),
    (re.compile(r"^Resolving deltas"), 80),
    (re.compile(r"^Updating "), 90),
    (re.compile(r"^Fast-forward"), 95),
    (re.compile(r"\bdone\b\s*$"), 95),
]


def _progress_for_line(line: str) -> int | None:
    for pat, pct in _GIT_STAGE_PROGRESS:
        if pat.search(line):
            return pct
    return None


# ─────────────────────────────────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────────────────────────────────


class _UpdateApp(App[int]):
    """One-shot Textual app for `cog update`. Returns exit code."""

    CSS = """
    Screen { background: #0E0E11; }

    /* Outer wrapper: center one card horizontally, fill height. */
    #shell {
        align: center middle;
        padding: 2 4;
        height: 100%;
        width: 100%;
    }

    /* The single card — fills almost the entire viewport. */
    #card {
        width: 100%;
        height: 100%;
        max-width: 120;
        max-height: 32;
        padding: 2 4;
        background: #161618;
        border: round #A8732D;
        color: #E6E1CF;
        layout: vertical;
    }

    #title       { color: #F5C24A; text-style: bold; height: 1; text-align: center; }
    #subtitle    { color: #9C957D; height: 1; padding-top: 1; text-align: center; }
    #version-row { height: auto; padding-top: 2; padding-bottom: 1; text-align: center; color: #E6E1CF; }
    #status      { height: auto; padding-top: 1; text-align: center; }

    /* Progress bar lane — give the bar full width, center the % readout. */
    #progress-lane {
        height: 3;
        padding-top: 2;
        align: center middle;
    }
    ProgressBar { width: 100%; }
    ProgressBar > Bar { width: 1fr; }
    ProgressBar > PercentageStatus { color: #F5C24A; }

    #ticker {
        height: 2;
        padding-top: 2;
        text-align: center;
        color: #C8C2A8;
    }

    /* Action row pinned to the bottom of the card. */
    #actions {
        dock: bottom;
        height: 5;
        align: center middle;
        padding: 1 0;
    }
    #actions Button {
        min-width: 20;
        height: 3;
        content-align: center middle;
        margin: 0 1;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "abort", "close"),
        Binding("enter", "primary", "confirm"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._exit_code = 0
        self._info: UpdateInfo | None = None
        self._update_root: Path | None = None
        self._busy = False

    # --- compose -------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="shell"):
            with Vertical(id="card"):
                yield Static("⚔  COGITUM  UPDATE", id="title")
                yield Static("checking origin/master…", id="subtitle")
                yield Static("", id="version-row")
                yield Static("", id="status")
                with Center(id="progress-lane"):
                    yield ProgressBar(total=100, show_eta=False, id="progress")
                yield Static("", id="ticker")
                with Horizontal(id="actions"):
                    pass

    async def on_mount(self) -> None:
        self._set_progress(5)
        self.run_worker(self._check_for_updates(), exclusive=True)

    # --- check phase ---------------------------------------------------

    async def _check_for_updates(self) -> None:
        from . import __version__ as installed
        self._set_ticker("contacting raw.githubusercontent…")
        latest = await _fetch_latest_version()
        if latest is None:
            self._show_error(
                "Could not reach origin/master. Check your network "
                "and try `cog update` again."
            )
            return

        self._set_progress(15)
        info = UpdateInfo(
            current=installed,
            latest=latest,
            newer=is_newer(latest, installed),
            install_method=detect_install_method(),
        )
        self._info = info
        self._set_version_row(info)

        if not info.newer:
            self._set_progress(100)
            self._set_subtitle("Cogitum is up to date.", style=OK, bold=True)
            self._set_ticker(f"installed = {installed}    ·    master = {latest}")
            self._mount_close_button()
            return

        # Newer version found → check we have a viable .git root.
        self._update_root = _find_update_root()
        if self._update_root is None:
            self._show_error(
                "Found a newer version, but couldn't determine where "
                "Cogitum is installed (no .git in the package's parent "
                "tree). Run `npm install -g cogitum` manually."
            )
            return

        self._set_progress(20)
        self._set_subtitle("A newer version is available.", style=GOLD_HI, bold=True)
        self._set_status(
            "Press [b]Upgrade now[/b] to apply, or Esc to cancel.",
            style=GOLD_HI,
        )
        self._set_ticker(f"will run  git pull --ff-only  in  {self._update_root}")
        self._mount_upgrade_buttons()

    # --- upgrade phase -------------------------------------------------

    async def _run_upgrade(self) -> None:
        assert self._update_root is not None
        self._busy = True
        self._clear_actions()
        self._set_subtitle("upgrading…", style=GOLD_HI, bold=True)
        self._set_status("", style=TXT)
        self._set_progress(25)
        self._set_ticker("starting git pull --ff-only…")

        # Force progress output even when stdout isn't a TTY (it's a
        # pipe here) so we get useful ticker lines instead of git's
        # "quiet by default for non-tty" behaviour. Without this the
        # bar sat at 25% staring at silence for the entire pull.
        env = {
            **os.environ,
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PROGRESS_DELAY": "0",
        }

        # Heartbeat task: bumps the ticker every 1.5s while we wait
        # for git output, so the UI never looks dead. Stops as soon
        # as the pull finishes.
        heartbeat_running = True
        heartbeat_dots = ["·", "··", "···", "····"]
        heartbeat_idx = 0
        last_real_line = ""

        async def heartbeat() -> None:
            nonlocal heartbeat_idx
            while heartbeat_running:
                await asyncio.sleep(1.5)
                if not heartbeat_running:
                    break
                tail = heartbeat_dots[heartbeat_idx % len(heartbeat_dots)]
                heartbeat_idx += 1
                self._set_ticker(
                    f"{last_real_line or 'fetching from origin'}  {tail}"
                )

        heartbeat_task = asyncio.create_task(heartbeat())

        async def _do_pull() -> tuple[int, str]:
            """Run git pull and stream progress; return (rc, last_line).

            Pulled out so we can wrap the whole thing in
            ``asyncio.wait_for(..., timeout=120)`` and clean up the
            subprocess group on timeout.
            """
            nonlocal last_real_line
            proc = await asyncio.create_subprocess_exec(
                "git", "pull", "--ff-only", "--progress",
                "origin", "master",
                cwd=str(self._update_root),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            try:
                # git --progress writes status updates with \r (carriage
                # return), not \n — splitting by '\n' would buffer the
                # whole pull into one giant string. Read raw chunks and
                # split on either separator so the ticker advances on
                # every progress update.
                assert proc.stdout is not None
                buf = b""
                while True:
                    chunk = await proc.stdout.read(256)
                    if not chunk:
                        break
                    buf += chunk
                    # Split on either \r or \n; keep the partial last
                    # piece for the next iteration.
                    while True:
                        nl = -1
                        for sep in (b"\r", b"\n"):
                            i = buf.find(sep)
                            if i != -1 and (nl == -1 or i < nl):
                                nl = i
                        if nl == -1:
                            break
                        line = buf[:nl].decode("utf-8", "replace").rstrip()
                        buf = buf[nl + 1:]
                        if not line:
                            continue
                        last_real_line = line
                        self._set_ticker(line)
                        hint = _progress_for_line(line)
                        if hint is not None:
                            self._set_progress(hint)

                # Drain any tail in buf.
                tail_line = buf.decode("utf-8", "replace").rstrip()
                if tail_line:
                    last_real_line = tail_line
                    self._set_ticker(tail_line)

                rc = await proc.wait()
                return rc, last_real_line
            except asyncio.CancelledError:
                # Caller (wait_for timeout) is unwinding us. Nuke the
                # whole git process group so a hung child doesn't
                # outlive the upgrade dialog. Mirrors the headless
                # path's TimeoutExpired handling.
                try:
                    if proc.returncode is None:
                        try:
                            os.killpg(os.getpgid(proc.pid), 9)
                        except (AttributeError, ProcessLookupError, OSError):
                            try:
                                proc.kill()
                            except ProcessLookupError:
                                pass
                except Exception:
                    pass
                raise

        try:
            try:
                # 120s ceiling matches the headless CLI path. Anything
                # longer is a hung auth prompt or dead network — kill it
                # rather than letting the dialog stare at a frozen ticker.
                rc, last_real_line = await asyncio.wait_for(
                    _do_pull(), timeout=120,
                )
            except asyncio.TimeoutError:
                self._show_error(
                    "Upgrade timed out",
                    detail="git pull > 120s — check network/auth and retry",
                )
                self._exit_code = 1
                rc = 1

            if rc == 0:
                self._set_progress(100)
                self._set_subtitle(
                    "✓ pulled. Restart Cogitum to apply.", style=OK, bold=True)
                self._set_ticker(
                    f"updated to {self._info.latest if self._info else '?'}")
                self._exit_code = 0
            else:
                self._show_error(
                    f"git pull failed (exit {rc})",
                    detail=last_real_line or "(no output)")
                self._exit_code = 1
        except Exception as e:
            self._show_error("upgrade crashed", detail=str(e))
            self._exit_code = 1
        finally:
            heartbeat_running = False
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass

        self._busy = False
        self._mount_close_button()

    # --- helpers ------------------------------------------------------

    def _show_error(self, headline: str, detail: str = "") -> None:
        self._set_progress(100)
        self._set_subtitle(headline, style=RUST, bold=True)
        if detail:
            self._set_ticker(detail)
        self._exit_code = 1
        self._mount_close_button()

    def _set_subtitle(self, text: str, *, style: str = TXT_DIM, bold: bool = False) -> None:
        node = self.query_one("#subtitle", Static)
        s = f"bold {style}" if bold else style
        node.update(Text(text, style=s))

    def _set_version_row(self, info: UpdateInfo) -> None:
        node = self.query_one("#version-row", Static)
        out = Text()
        out.append("current  ", style=TXT_DIM)
        out.append(info.current, style=TXT)
        out.append("    →    ", style=GOLD_DIM)
        out.append("latest  ", style=TXT_DIM)
        out.append(info.latest or "?", style=f"bold {OK if info.newer else TXT}")
        node.update(out)

    def _set_status(self, text: str, *, style: str = TXT) -> None:
        node = self.query_one("#status", Static)
        node.update(Text.from_markup(text, style=style))

    def _set_ticker(self, line: str) -> None:
        node = self.query_one("#ticker", Static)
        # Trim very long lines so the ticker row stays single-line.
        if len(line) > 200:
            line = line[:197] + "…"
        out = Text()
        out.append("›  ", style=GOLD_DIM)
        out.append(line, style=TXT_DIM)
        node.update(out)

    def _set_progress(self, pct: int) -> None:
        try:
            bar = self.query_one("#progress", ProgressBar)
            bar.update(progress=max(0, min(100, pct)))
        except Exception:
            pass

    def _clear_actions(self) -> None:
        actions = self.query_one("#actions", Horizontal)
        for child in list(actions.children):
            child.remove()

    def _mount_upgrade_buttons(self) -> None:
        self._clear_actions()
        actions = self.query_one("#actions", Horizontal)
        actions.mount(Button("Upgrade now", id="btn-upgrade", variant="primary"))
        actions.mount(Button("Cancel", id="btn-cancel"))

    def _mount_close_button(self) -> None:
        self._clear_actions()
        actions = self.query_one("#actions", Horizontal)
        actions.mount(Button("Close", id="btn-close", variant="primary"))

    # --- button + key bindings -----------------------------------------

    @on(Button.Pressed, "#btn-upgrade")
    def _on_upgrade(self) -> None:
        self.run_worker(self._run_upgrade(), exclusive=True)

    @on(Button.Pressed, "#btn-cancel")
    @on(Button.Pressed, "#btn-close")
    def _on_close(self) -> None:
        self.exit(self._exit_code)

    def action_abort(self) -> None:
        if self._busy:
            return  # don't yank the rug while git is running
        self.exit(self._exit_code)

    def action_primary(self) -> None:
        try:
            actions = self.query_one("#actions", Horizontal)
            for ch in actions.children:
                if isinstance(ch, Button) and ch.id == "btn-upgrade":
                    self._on_upgrade()
                    return
            self.exit(self._exit_code)
        except Exception:
            self.exit(self._exit_code)


# ─────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────


def run() -> int:
    """Launch the update Textual app, return its exit code.

    Falls back to a plain-text path if Textual fails to start (no
    terminal, dumb pipe, missing capabilities). Plain-text path runs
    the same logic and prints linearly so `cog update` stays usable
    from CI / scripts.
    """
    if not _can_use_textual():
        return _run_headless()
    try:
        return int(_UpdateApp().run() or 0)
    except Exception as e:
        print(f"⚠ TUI failed: {e}\nFalling back to headless mode.")
        return _run_headless()


def _can_use_textual() -> bool:
    import sys
    if not sys.stdout.isatty():
        return False
    if os.environ.get("TERM", "").lower() in ("", "dumb"):
        return False
    return True


def _run_headless() -> int:
    from . import __version__ as installed
    print(f"⚔ Cogitum update — current: {installed}")
    print("checking origin/master …", flush=True)
    latest = asyncio.run(_fetch_latest_version())
    if latest is None:
        print("✗ could not reach origin/master")
        return 1
    print(f"latest: {latest}")
    if not is_newer(latest, installed):
        print("✓ Cogitum is up to date")
        return 0
    root = _find_update_root()
    if root is None:
        print("✗ could not find .git checkout — run `npm install -g cogitum` manually")
        return 1
    print(f"running: git pull --ff-only  (cwd={root})")
    env = {**os.environ, "GIT_PAGER": "cat", "PAGER": "cat",
           "GIT_TERMINAL_PROMPT": "0"}
    # 120s is generous for a fast-forward pull on any reasonable
    # repo/network. If we're stuck longer than that we're probably
    # blocked on auth or a hung connection (GIT_TERMINAL_PROMPT=0 should
    # have killed prompts already, but belt-and-braces). Use ``run``
    # with check=False so we can log the returncode ourselves rather
    # than catching CalledProcessError; ``call`` had no timeout kwarg
    # before Python 3.3+ added it transitively, but we want explicit
    # error paths for both timeout and nonzero exits.
    try:
        cp = subprocess.run(
            ["git", "pull", "--ff-only", "origin", "master"],
            cwd=str(root), env=env, check=False, timeout=120,
        )
        rc = cp.returncode
    except subprocess.TimeoutExpired:
        print("✗ git pull timed out after 120s — check network/auth and retry")
        return 1
    if rc == 0:
        print("✓ pulled. Restart Cogitum to apply.")
        return 0
    print(f"✗ git pull failed (exit {rc})")
    return 1


__all__ = ["run"]
