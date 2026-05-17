"""Generate aesthetic SVG screenshots for README / docs.

Uses Textual's headless mode + export_screenshot().
Run: python scripts/screenshots.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cogitum.app import CogitumApp
from cogitum.setup_flow import SetupScreen
from cogitum.widgets.model_picker import ModelPicker
from cogitum.widgets.session_picker import SessionPicker
from cogitum.core.llm.loader import seed_default_config, _PROVIDERS_PATH
from cogitum.core.sessions import get_store
from cogitum.core.events import Message, TextPart

ASSETS = Path(__file__).resolve().parent.parent / "assets"


async def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)

    # Ensure default config exists so mesh loads
    seed_default_config(_PROVIDERS_PATH)

    # Seed a dummy session so session picker isn't empty
    store = get_store()
    if not store.list_sessions(limit=1):
        sid = store.create_session("demo-session", title="Demo: Auth refactor", model="kimi-k2.6")
        store.append_message(sid, Message(role="user", parts=[TextPart(text="Refactor auth to use OAuth2")]))
        store.append_message(sid, Message(role="assistant", parts=[TextPart(text="Done. Split into registry, storage, and pkce modules.")]))
        sid2 = store.create_session("demo-session-2", title="Debug: queue race", model="claude-sonnet-4-5")
        store.append_message(sid2, Message(role="user", parts=[TextPart(text="Fix the queue race condition")]))

    # Screen size tuned for 1920×1080 aspect ratio (16:9).
    # Textual cells are ~12.3×25.5 px in SVG export.
    # cols/rows ≈ 3.68 gives a 16:9 viewBox.
    _COLS, _ROWS = 158, 43

    app = CogitumApp()

    # --- 1. Main window --------------------------------------------------
    async with app.run_test(size=(_COLS, _ROWS)) as pilot:
        await pilot.pause()
        await pilot.pause()
        svg = app.export_screenshot(title="COGITUM — Mk.V")
        (ASSETS / "main.svg").write_text(svg, encoding="utf-8")
        print(f"wrote {ASSETS / 'main.svg'}")

    # --- 2. Model picker -------------------------------------------------
    app2 = CogitumApp()
    async with app2.run_test(size=(_COLS, _ROWS)) as pilot:
        await pilot.pause()
        app2.push_screen(ModelPicker(app2.mesh, current=app2.current_model))
        await pilot.pause()
        await pilot.pause()
        svg = app2.export_screenshot(title="COGITUM — Models")
        (ASSETS / "models.svg").write_text(svg, encoding="utf-8")
        print(f"wrote {ASSETS / 'models.svg'}")

    # --- 3. Setup wizard -------------------------------------------------
    app3 = CogitumApp()
    async with app3.run_test(size=(_COLS, _ROWS)) as pilot:
        await pilot.pause()
        app3.push_screen(SetupScreen())
        await pilot.pause()
        await pilot.pause()
        svg = app3.export_screenshot(title="COGITUM — Setup")
        (ASSETS / "setup.svg").write_text(svg, encoding="utf-8")
        print(f"wrote {ASSETS / 'setup.svg'}")

    # --- 4. Session picker -----------------------------------------------
    app4 = CogitumApp()
    async with app4.run_test(size=(_COLS, _ROWS)) as pilot:
        await pilot.pause()
        app4.push_screen(SessionPicker())
        await pilot.pause()
        await pilot.pause()
        svg = app4.export_screenshot(title="COGITUM — Sessions")
        (ASSETS / "sessions.svg").write_text(svg, encoding="utf-8")
        print(f"wrote {ASSETS / 'sessions.svg'}")


if __name__ == "__main__":
    asyncio.run(main())
