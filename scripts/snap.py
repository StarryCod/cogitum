"""Render Cogitum TUI to SVG without a real terminal.

Uses Textual's built-in App.run_test() + App.export_screenshot() to drive
the app headlessly and write out an SVG of the rendered screen.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# add src
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cogitum.app import CogitumApp


OUT = Path(__file__).resolve().parent.parent / "assets" / "tui-preview.svg"


async def render():
    app = CogitumApp()
    async with app.run_test(size=(140, 44)) as pilot:
        # let mount + initial layout settle
        await pilot.pause()
        await pilot.pause()
        svg = app.export_screenshot(title="COGITUM — Mk.IV.7")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(svg, encoding="utf-8")
    print(f"wrote {OUT}  ({len(svg):,} bytes)")


if __name__ == "__main__":
    asyncio.run(render())
