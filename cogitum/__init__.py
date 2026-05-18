"""Cogitum — sovereign agentic CLI. Entry point."""
from __future__ import annotations

# Single source of truth for the Python project version. The npm
# wrapper has its own version (npm/package.json) that's allowed to
# diverge — it's a separate artifact with its own release cadence.
__version__ = "0.3.0"

from .app import CogitumApp


def main() -> None:
    CogitumApp().run()


if __name__ == "__main__":
    main()

