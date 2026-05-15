"""Cogitum TUI app — bubbles + specialised tool cards + model picker."""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.widgets import Input, Static

from .core.llm.loader import load_mesh, load_settings, write_settings
from .core.llm.mesh import Mesh, ResolvedModel
from .setup_flow import SetupScreen
from .widgets.banner import Banner, BannerTags
from .widgets.feed import Feed
from .widgets.cards import EditCard, RunCard, SearchCard, SwarmCard, ReadCard
from .widgets.inspector import Inspector
from .widgets.model_picker import ModelPicker
from .widgets.statusbar import StatusBar


def _seed(feed: Feed) -> None:
    feed.append_system("Session ready  ·  type / for commands  ·  Ctrl+M for models", "init")
    feed.append_agent(
        "Cogitum is online. Pick a model with /models and start with a task.",
        meta="ready",
    )


class HRule(Static):
    """Full-width horizontal divider that always reaches the screen edges."""
    def __init__(self, classes: str = "hrule", **kw) -> None:
        super().__init__("", classes=classes, **kw)


class CogitumApp(App):
    CSS_PATH = "cogitum.tcss"
    TITLE = "COGITUM"
    SUB_TITLE = "forge mark vii"

    BINDINGS = [
        Binding("ctrl+q", "quit", "quit"),
        Binding("ctrl+m", "open_models", "models"),
        Binding("ctrl+comma", "open_setup", "setup"),
        Binding("ctrl+r", "noop", "rewind", show=False),
        Binding("ctrl+s", "noop", "verify", show=False),
        Binding("ctrl+k", "noop", "swarm", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.mesh: Mesh | None = None
        self.settings: dict = {}
        self.current_model: str | None = None

    # ------------------------------------------------------------------
    # compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Banner(id="banner")
        yield BannerTags(id="banner-tags")
        yield HRule(id="hrule-top")
        with Container(id="main"):
            yield Feed(id="feed-pane")
            yield Inspector(id="inspector-pane")
        yield StatusBar(id="statusbar")
        yield HRule(id="hrule-bottom")
        with Horizontal(id="composer-shell"):
            yield Static("▶", id="composer-prefix")
            yield Input(placeholder="type your task or /command…", id="composer")

    # ------------------------------------------------------------------
    # mount
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        feed = self.query_one("#feed-pane", Feed)
        _seed(feed)
        self._load_mesh_async()

    def _load_mesh_async(self) -> None:
        feed = self.query_one("#feed-pane", Feed)
        try:
            self.mesh = load_mesh()
            self.settings = load_settings()
        except Exception as e:  # noqa: BLE001
            feed.append_error(f"mesh load failed: {e}", meta="config")
            return

        if not self.mesh.providers:
            feed.append_error(
                "No providers configured. Run `cog setup` outside the TUI to "
                "add API keys or connect a subscription.",
                meta="empty mesh",
            )
            self._update_statusbar("—")
            return

        default = self.settings.get("default_model")
        if default and self.mesh.resolve(default):
            self.current_model = default
        else:
            # First available.
            pairs = self.mesh.list_resolved()
            self.current_model = pairs[0].qualified_id if pairs else None

        self._update_statusbar(self.current_model or "—")
        feed.append_system(
            f"mesh ready · {len(self.mesh.providers)} providers · {len(self.mesh.list_resolved())} models",
            "loaded",
        )

    def _update_statusbar(self, model: str) -> None:
        try:
            self.query_one("#statusbar", StatusBar).set_model(model)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------

    def action_noop(self) -> None:
        pass

    def action_open_models(self) -> None:
        if self.mesh is None or not self.mesh.providers:
            self.query_one("#feed-pane", Feed).append_error(
                "No mesh available — configure providers first (Ctrl+, or /setup)."
            )
            return
        picker = ModelPicker(self.mesh, current=self.current_model)
        self.push_screen(picker, self._on_model_picked)

    def action_open_setup(self) -> None:
        self.push_screen(SetupScreen(), self._on_setup_close)

    def _on_setup_close(self, _result: object) -> None:
        # Re-load mesh and settings after setup.
        self._load_mesh_async()

    def _on_model_picked(self, resolved: ResolvedModel | None) -> None:
        if resolved is None:
            return
        self.current_model = resolved.qualified_id
        self.settings["default_model"] = self.current_model
        try:
            write_settings(self.settings)
        except Exception:  # noqa: BLE001
            pass
        self._update_statusbar(self.current_model)
        self.query_one("#feed-pane", Feed).append_system(
            f"model = {self.current_model}", "switched"
        )

    # ------------------------------------------------------------------
    # composer
    # ------------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        feed = self.query_one("#feed-pane", Feed)
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return

        if text.startswith("/"):
            self._handle_command(text, feed)
            return

        feed.append_user(text)
        if self.current_model is None:
            feed.append_error("No model selected. /models to pick one.")
            return
        feed.append_agent(
            f"(stub) would stream from {self.current_model}. Agent loop wires next.",
            meta="staged",
        )

    def _handle_command(self, raw: str, feed: Feed) -> None:
        parts = raw[1:].split(maxsplit=1)
        cmd = (parts[0] if parts else "").lower()
        rest = parts[1] if len(parts) > 1 else ""

        if cmd in ("setup", "settings", "config"):
            self.action_open_setup()
            return

        if cmd in ("models", "model"):
            if rest:
                # /model kimi -> direct switch, fuzzy resolve
                if self.mesh is None:
                    feed.append_error("mesh not ready")
                    return
                candidates = self.mesh.resolve(rest)
                if not candidates:
                    feed.append_error(f"no model matches: {rest!r}")
                    return
                if len(candidates) > 1:
                    self.action_open_models()
                    return
                self._on_model_picked(candidates[0])
                return
            self.action_open_models()
            return

        if cmd == "help":
            feed.append_system(
                "/setup — provider/auth wizard · /models — pick model · "
                "/model <id|alias> — direct switch · /clear — clear feed · "
                "/quit — exit",
                "commands",
            )
            return

        if cmd == "clear":
            feed.clear()
            return

        if cmd in ("quit", "exit", "q"):
            self.exit()
            return

        feed.append_error(f"unknown command: /{cmd}  (try /help)")


def main() -> None:
    CogitumApp().run()


if __name__ == "__main__":
    main()
