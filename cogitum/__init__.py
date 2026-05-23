"""Cogitum — sovereign agentic CLI. Entry point."""
from __future__ import annotations

# Single source of truth for the Python project version. Keep this
# in lockstep with ``pyproject.toml`` (the version field) — both are
# read by different tooling. The update wizard pulls master's
# pyproject.toml and compares it to ``__version__`` here, so a stale
# ``__version__`` makes ``cog update`` look like it succeeded but
# the next launch still reports the old version.
#
# The npm wrapper has its own version (``npm/package.json``) that's
# allowed to diverge — it's a separate artifact with its own release
# cadence.
__version__ = "0.6.0"

from .app import CogitumApp


def main() -> None:
    CogitumApp().run()


if __name__ == "__main__":
    main()

