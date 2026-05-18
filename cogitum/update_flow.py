"""
cogitum.update_flow
~~~~~~~~~~~~~~~~~~~

`cog update` — self-update flow with a small Textual UI.

What it does, in order:

  1. Show a card "checking origin/master…" while it probes the
     remote ``pyproject.toml`` (4s timeout, no cache so the user's
     deliberate `cog update` always sees fresh data).
  2. If installed == latest → "Cogitum is up to date" and exit.
  3. If newer is available, show the version diff and ask to
     proceed (Enter / [Y] = yes, Esc / [N] = no).
  4. On confirm: detect the install root, run the right upgrade
     command (`git pull --ff-only` for npm-style installs that
     keep the repo at ``%LOCALAPPDATA%\\cogitum`` or
     ``~/.local/share/cogitum``; `git pull --ff-only` for source
     clones too — both layouts are .git checkouts, the difference
     is just where the dir sits).
  5. Stream stdout/stderr from the upgrade process line-by-line
     into a scrolling log pane.
  6. On success, prompt "Restart Cogitum to apply" and exit 0.
     On failure, surface the error and exit 1.

Why a separate flow / Textual screen rather than printing to
stdout? Two reasons:

  * The user explicitly asked for a "красивая менюшка с прогрессом
    которая сверяет последний коммит и локальную версию и делает
    обновление и потом просит перезапустить Cogitum". Cogitum's
    aesthetic is the TUI; a plain stdout output would feel out of
    register.
  * Streaming subprocess output through the agent's existing
    rendering primitives (round-bordered card, gold text, status
    glyphs) keeps the look unified.

Why standalone Textual app instead of a modal inside the main
Cogitum app? `cog update` runs from a fresh process — there's no
main app to mount a modal *into*. A standalone `App` is the right
shape for one-shot CLI screens.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Static

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
    """Return the .git checkout root that should be `git pull`-ed.

    Order:
      1. ``COGITUM_HOME`` env var (set by the npm wrapper).
      2. The directory containing this Python package's ``__file__``,
         walking up until we find a ``.git`` directory or run out of
         parents. Covers source clones (``~/Cogitum``) AND the npm
         wrapper's clone (``~/.local/share/cogitum``,
         ``%LOCALAPPDATA%\\cogitum``, …).

    Returns ``None`` if no .git root is found — in that case the
    update flow surfaces a "can't determine install location" error
    and asks the user to upgrade manually.
    """
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
# UI
# ─────────────────────────────────────────────────────────────────────────


class _UpdateApp(App[int]):
    """One-shot Textual app for `cog update`. Returns exit code."""

    DEFAULT_CSS = """
    Screen { background: #0E0E11; }

    #shell {
        align: center top;
        padding: 2 4;
        height: 100%;
    }

    #card {
        width: 80;
        max-width: 100%;
        padding: 1 2;
        background: #161618;
        border: round #A8732D;
        color: #E6E1CF;
    }
    #title  { color: #F5C24A; text-style: bold; height: 1; }
    #status { color: #C8C2A8; height: auto; padding-top: 1; }

    #version-row {
        height: auto; padding-top: 1; padding-bottom: 1;
    }

    #log-card {
        width: 80;
        max-width: 100%;
        height: 14;
        margin-top: 1;
        background: #1A1A1D;
        border: round #2A2620;
        color: #C8C2A8;
        padding: 1 2;
    }
    #log-title { color: #F5C24A; text-style: bold; height: 1; }
    #log-body { padding-top: 1; }

    #actions {
        height: 3;
        padding-top: 1;
        align: center middle;
    }

    #foot { padding-top: 1; height: 1; color: #7A5A1A; text-align: center; }
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
                yield Static("⚔ COGITUM UPDATE", id="title")
                yield Static("Checking origin/master…", id="status")
                yield Static("", id="version-row")
                with Horizontal(id="actions"):
                    pass  # buttons mounted dynamically once we know state
            with VerticalScroll(id="log-card"):
                yield Static("── Output ──", id="log-title")
                yield Static("", id="log-body")
            yield Static("Esc close", id="foot")

    async def on_mount(self) -> None:
        self.run_worker(self._check_for_updates(), exclusive=True)

    # --- check phase ---------------------------------------------------

    async def _check_for_updates(self) -> None:
        from . import __version__ as installed
        latest = await _fetch_latest_version()
        if latest is None:
            self._set_status(
                "Could not reach origin/master.\n"
                "Check your network and try again.", error=True)
            self._exit_code = 1
            self._mount_close_button()
            return

        info = UpdateInfo(
            current=installed,
            latest=latest,
            newer=is_newer(latest, installed),
            install_method=detect_install_method(),
        )
        self._info = info

        if not info.newer:
            self._set_status(
                f"Cogitum is up to date.  ({installed})", ok=True)
            row = self.query_one("#version-row", Static)
            row.update(self._render_version_row(info))
            self._mount_close_button()
            return

        self._update_root = _find_update_root()
        if self._update_root is None:
            self._set_status(
                "Found a newer version, but could not determine where "
                "Cogitum is installed (no .git in the package's parent "
                "tree). Run `npm install -g cogitum` manually.",
                error=True)
            row = self.query_one("#version-row", Static)
            row.update(self._render_version_row(info))
            self._exit_code = 1
            self._mount_close_button()
            return

        # Found new version + viable .git root → offer to upgrade.
        self._set_status(
            f"A newer version is available.\n"
            f"Will run: git pull --ff-only  in  {self._update_root}",
            ok=False)
        row = self.query_one("#version-row", Static)
        row.update(self._render_version_row(info))
        self._mount_upgrade_button()

    def _render_version_row(self, info: UpdateInfo) -> Text:
        out = Text()
        out.append("current  ", style=TXT_DIM)
        out.append(info.current, style=TXT)
        out.append("    →    ", style=GOLD_DIM)
        out.append("latest  ", style=TXT_DIM)
        if info.newer:
            out.append(info.latest or "?", style=f"bold {OK}")
        else:
            out.append(info.latest or "?", style=TXT)
        return out

    # --- upgrade phase -------------------------------------------------

    async def _run_upgrade(self) -> None:
        assert self._update_root is not None
        self._busy = True
        self._set_status("Pulling latest from origin/master…")
        self._clear_actions()

        # Disable git pager just in case the user's global config has
        # one — pagers hang when run with no tty.
        env = {**os.environ, "GIT_PAGER": "cat", "PAGER": "cat"}

        proc = await asyncio.create_subprocess_exec(
            "git", "pull", "--ff-only", "origin", "master",
            cwd=str(self._update_root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        log = self.query_one("#log-body", Static)
        accumulated: list[str] = []

        async def _drain() -> None:
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                accumulated.append(line)
                # Keep the log pane to the last 200 lines so a noisy
                # pull can't blow up the widget.
                if len(accumulated) > 200:
                    del accumulated[: len(accumulated) - 200]
                log.update("\n".join(accumulated))

        await _drain()
        rc = await proc.wait()

        if rc == 0:
            self._set_status(
                "✓ Pull succeeded.\n"
                "Restart Cogitum to apply the new version.", ok=True)
            self._exit_code = 0
        else:
            self._set_status(
                f"✗ git pull failed (exit {rc}). See output above.",
                error=True)
            self._exit_code = 1

        self._busy = False
        self._mount_close_button()

    # --- action helpers ------------------------------------------------

    def _set_status(self, text: str, *, ok: bool = False, error: bool = False) -> None:
        node = self.query_one("#status", Static)
        if error:
            node.update(Text(text, style=RUST))
        elif ok:
            node.update(Text(text, style=OK))
        else:
            node.update(Text(text, style=TXT))

    def _clear_actions(self) -> None:
        actions = self.query_one("#actions", Horizontal)
        for child in list(actions.children):
            child.remove()

    def _mount_upgrade_button(self) -> None:
        self._clear_actions()
        actions = self.query_one("#actions", Horizontal)
        actions.mount(Button("Upgrade now", id="btn-upgrade", variant="primary"))
        actions.mount(Button("Cancel", id="btn-cancel"))

    def _mount_close_button(self) -> None:
        self._clear_actions()
        actions = self.query_one("#actions", Horizontal)
        actions.mount(Button("Close", id="btn-close", variant="primary"))

    # --- button handlers ----------------------------------------------

    @on(Button.Pressed, "#btn-upgrade")
    def _on_upgrade(self) -> None:
        self.run_worker(self._run_upgrade(), exclusive=True)

    @on(Button.Pressed, "#btn-cancel")
    @on(Button.Pressed, "#btn-close")
    def _on_close(self) -> None:
        self.exit(self._exit_code)

    # --- key bindings -------------------------------------------------

    def action_abort(self) -> None:
        if self._busy:
            return  # don't yank the rug while git is running
        self.exit(self._exit_code)

    def action_primary(self) -> None:
        # Enter behaves as the primary action for whichever phase
        # we're in: confirm upgrade if the upgrade button is mounted,
        # close otherwise.
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

    Falls back to a plain-text path if Textual fails to start (e.g.
    no terminal, dumb pipe, missing capabilities). Plain-text path
    just runs the same logic and prints linearly — keeps `cog update`
    usable from CI / scripts.
    """
    if not _can_use_textual():
        return _run_headless()

    try:
        return int(_UpdateApp().run() or 0)
    except Exception as e:
        print(f"⚠ TUI failed: {e}\nFalling back to headless mode.")
        return _run_headless()


def _can_use_textual() -> bool:
    """Heuristic: stdout is a TTY and we have a non-dumb terminal."""
    import sys
    if not sys.stdout.isatty():
        return False
    if os.environ.get("TERM", "").lower() in ("", "dumb"):
        return False
    return True


def _run_headless() -> int:
    """No-TUI fallback. Same logic, plain prints."""
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
    env = {**os.environ, "GIT_PAGER": "cat", "PAGER": "cat"}
    rc = subprocess.call(
        ["git", "pull", "--ff-only", "origin", "master"],
        cwd=str(root), env=env,
    )
    if rc == 0:
        print("✓ pulled. Restart Cogitum to apply.")
        return 0
    print(f"✗ git pull failed (exit {rc})")
    return 1


__all__ = ["run"]
