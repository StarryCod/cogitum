"""Cogitum — sovereign agentic CLI. Entry point."""
from __future__ import annotations

from .app import CogitumApp


def main() -> None:
    CogitumApp().run()


if __name__ == "__main__":
    main()
