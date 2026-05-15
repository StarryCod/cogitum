"""Cogitum TUI app — bubbles + specialised tool cards + model picker."""
from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.widgets import Input, Static

from .core.agent import Agent, AgentConfig, AgentDone, AgentError, AgentText, AgentThinking, AgentToolCall, AgentToolResult
from .core.builtin_tools import *  # noqa: F401,F403 — registers tools into REGISTRY
from .core.llm.loader import load_mesh, load_settings, write_settings
from .core.llm.mesh import Mesh, ResolvedModel
from .core.tools import REGISTRY
from .setup_flow import SetupScreen
from .widgets.banner import Banner, BannerTags
from .widgets.feed import AgentBlock, Feed, ThinkingBlock, ToolCallCard
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
    """Full-width horizontal divider."""
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
        Binding("escape", "cancel_agent", "cancel", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.mesh: Mesh | None = None
        self.settings: dict = {}
        self.current_model: str | None = None
        self._agent: Agent | None = None
        self._agent_task: asyncio.Task | None = None
        self._history: list = []   # list[Message] — persists across turns

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
                "No providers configured. Run `cog setup` to add API keys.",
                meta="empty mesh",
            )
            self._update_statusbar("—")
            return

        default = self.settings.get("default_model")
        if default and self.mesh.resolve(default):
            self.current_model = default
        else:
            pairs = self.mesh.list_resolved()
            self.current_model = pairs[0].qualified_id if pairs else None

        self._update_statusbar(self.current_model or "—")
        feed.append_system(
            f"mesh ready · {len(self.mesh.providers)} providers · {len(self.mesh.list_resolved())} models",
            "loaded",
        )

        # Build agent
        if self.mesh:
            self._agent = Agent(
                mesh=self.mesh,
                registry=REGISTRY,
                config=AgentConfig(model=self.current_model),
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

    def action_cancel_agent(self) -> None:
        if self._agent_task and not self._agent_task.done():
            self._agent_task.cancel()
            feed = self.query_one("#feed-pane", Feed)
            feed.append_system("agent cancelled", "esc")
            self._set_composer_enabled(True)

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
        # Update agent config
        if self._agent:
            self._agent.cfg.model = self.current_model
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

        # Don't allow concurrent runs
        if self._agent_task and not self._agent_task.done():
            feed.append_system("agent is busy — press Esc to cancel", "busy")
            return

        feed.append_user(text)

        if self._agent is None or self.mesh is None:
            feed.append_error("No model selected. /models to pick one.")
            return

        self._set_composer_enabled(False)
        self._agent_task = asyncio.create_task(
            self._run_agent(text, feed)
        )

    def _set_composer_enabled(self, enabled: bool) -> None:
        try:
            inp = self.query_one("#composer", Input)
            inp.disabled = not enabled
            prefix = self.query_one("#composer-prefix", Static)
            prefix.update("▶" if enabled else "⏳")
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # agent worker
    # ------------------------------------------------------------------

    async def _run_agent(self, user_message: str, feed: Feed) -> None:
        """Run one agent turn, streaming events into the feed."""
        queue: asyncio.Queue = asyncio.Queue()

        # Current agent block and thinking block (updated live)
        agent_block: AgentBlock | None = None
        thinking_block: ThinkingBlock | None = None
        # Map call_id → ToolCallCard for result updates
        tool_cards: dict[str, ToolCallCard] = {}

        async def drain_queue() -> None:
            nonlocal agent_block, thinking_block
            while True:
                try:
                    event = queue.get_nowait()
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.02)
                    continue

                if isinstance(event, AgentText):
                    if agent_block is None:
                        agent_block = feed.append_agent()
                    self.call_from_thread(agent_block.append_delta, event.delta) if False else agent_block.append_delta(event.delta)

                elif isinstance(event, AgentThinking):
                    if thinking_block is None:
                        thinking_block = feed.append_thinking()
                    thinking_block.append_delta(event.delta)

                elif isinstance(event, AgentToolCall):
                    # Finish thinking block if open
                    if thinking_block is not None:
                        thinking_block.finish()
                        thinking_block = None
                    # New turn → new agent block next time
                    agent_block = None
                    card = feed.append_tool_call(
                        event.tool_name, event.arguments, event.call_id
                    )
                    tool_cards[event.call_id] = card

                elif isinstance(event, AgentToolResult):
                    card = tool_cards.get(event.call_id)
                    if card:
                        card.set_result(event.result, error=event.error)
                    # New agent block for next response
                    agent_block = None

                elif isinstance(event, AgentDone):
                    if thinking_block is not None:
                        thinking_block.finish()
                    usage = event.usage
                    if usage:
                        feed.append_system(
                            f"done · {event.turns} turn(s) · "
                            f"in={usage.input_tokens} out={usage.output_tokens}",
                            "usage",
                        )
                    self._set_composer_enabled(True)
                    return

                elif isinstance(event, AgentError):
                    feed.append_error(event.message, meta="agent")
                    self._set_composer_enabled(True)
                    return

        # Run agent + drain concurrently
        agent_coro = self._agent.run(
            user_message=user_message,
            history=self._history,
            queue=queue,
        )

        try:
            agent_fut = asyncio.create_task(agent_coro)
            drain_fut = asyncio.create_task(drain_queue())

            done, pending = await asyncio.wait(
                [agent_fut, drain_fut],
                return_when=asyncio.ALL_COMPLETED,
            )

            # Propagate exceptions
            for t in done:
                if t.exception():
                    raise t.exception()  # type: ignore[misc]

            # Update history with new messages
            if not agent_fut.cancelled():
                self._history = agent_fut.result()

        except asyncio.CancelledError:
            agent_fut.cancel()
            drain_fut.cancel()
            raise
        except Exception as exc:  # noqa: BLE001
            feed.append_error(str(exc), meta="agent")
            self._set_composer_enabled(True)

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    def _handle_command(self, raw: str, feed: Feed) -> None:
        parts = raw[1:].split(maxsplit=1)
        cmd = (parts[0] if parts else "").lower()
        rest = parts[1] if len(parts) > 1 else ""

        if cmd in ("setup", "settings", "config"):
            self.action_open_setup()
            return

        if cmd in ("models", "model"):
            if rest:
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

        if cmd == "new":
            self._history = []
            feed.clear()
            feed.append_system("new session — history cleared", "new")
            return

        if cmd == "tools":
            names = REGISTRY.names()
            feed.append_system(f"{len(names)} tools: {', '.join(names)}", "tools")
            return

        if cmd == "help":
            feed.append_system(
                "/setup — provider wizard · /models — pick model · "
                "/model <id> — direct switch · /new — clear history · "
                "/tools — list tools · /clear — clear feed · /quit — exit",
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
