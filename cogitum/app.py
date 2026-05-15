"""Cogitum TUI app — bubbles + specialised tool cards + model picker."""
from __future__ import annotations

import asyncio

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.widgets import Static

from .core.agent import Agent, AgentConfig, AgentDone, AgentError, AgentText, AgentThinking, AgentToolCall, AgentToolResult
from .core.builtin_tools import *  # noqa: F401,F403 — registers tools into REGISTRY
from .core.llm.loader import load_mesh, load_settings, write_settings
from .core.llm.mesh import Mesh, ResolvedModel
from .core.tools import REGISTRY
from .setup_flow import SetupScreen
from .widgets.banner import Banner, BannerTags
from .widgets.composer import Composer
from .widgets.cards import EditCard, RunCard, SearchCard, ReadCard, FetchCard
from .widgets.feed import AgentBlock, Feed, ThinkingBlock, ToolCallCard, WaitingIndicator
from .widgets.inspector import Inspector, InspectorState
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
            with VerticalScroll(id="inspector-pane"):
                yield Inspector(id="inspector-widget")
        yield StatusBar(id="statusbar")
        yield Composer(id="composer")

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
            # Update inspector with real data
            inspector = self.query_one("#inspector-widget", Inspector)
            model_name = self.current_model or "—"
            provider_name = "—"
            context_window = 200_000
            if self.current_model:
                resolved = self.mesh.resolve(self.current_model)
                if resolved:
                    provider_name = resolved[0].provider.id
                    context_window = resolved[0].model.context_window or 200_000
            inspector.update_state(
                model=model_name,
                provider=provider_name,
                context_window=context_window,
                tools=REGISTRY.names(),
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
        self._update_inspector_model(resolved)
        # Update agent config
        if self._agent:
            self._agent.cfg.model = self.current_model
        self.query_one("#feed-pane", Feed).append_system(
            f"model = {self.current_model}", "switched"
        )

    def _update_inspector_model(self, resolved: ResolvedModel) -> None:
        try:
            inspector = self.query_one("#inspector-widget", Inspector)
            inspector.update_state(
                model=resolved.model.display or resolved.model.id,
                provider=resolved.provider.id,
                context_window=resolved.model.context_window or 200_000,
            )
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # composer
    # ------------------------------------------------------------------

    @on(Composer.Submitted)
    def _on_composer_submitted(self, event: Composer.Submitted) -> None:
        feed = self.query_one("#feed-pane", Feed)
        text = event.value.strip()
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
            self.query_one("#composer", Composer).set_enabled(enabled)
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
        waiting: WaitingIndicator | None = feed.append_waiting()
        # Map call_id → ToolCallCard for result updates
        tool_cards: dict[str, ToolCallCard] = {}
        # Map call_id → AgentToolCall event for rich card creation
        tool_calls_data: dict[str, AgentToolCall] = {}

        async def drain_queue() -> None:
            nonlocal agent_block, thinking_block, waiting
            while True:
                try:
                    event = queue.get_nowait()
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.02)
                    continue

                if isinstance(event, AgentText):
                    # Stop waiting animation on first content
                    if waiting is not None:
                        waiting.stop()
                        waiting = None
                    if agent_block is None:
                        agent_block = feed.append_agent()
                    agent_block.append_delta(event.delta)

                elif isinstance(event, AgentThinking):
                    # Stop waiting animation on first thinking
                    if waiting is not None:
                        waiting.stop()
                        waiting = None
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
                    # Show a pending card (will be replaced with proper card on result)
                    card = feed.append_tool_call(
                        event.tool_name, event.arguments, event.call_id
                    )
                    tool_cards[event.call_id] = card
                    tool_calls_data[event.call_id] = event

                elif isinstance(event, AgentToolResult):
                    card = tool_cards.get(event.call_id)
                    # Replace generic card with a beautiful typed card
                    tool_event = tool_calls_data.get(event.call_id)
                    if card and tool_event:
                        rich_card = self._make_rich_card(
                            tool_event.tool_name,
                            tool_event.arguments,
                            event.result,
                            event.error,
                        )
                        if rich_card:
                            card.remove()
                            feed.append_card(rich_card)
                        else:
                            card.set_result(event.result, error=event.error)
                    elif card:
                        card.set_result(event.result, error=event.error)
                    # New agent block for next response
                    agent_block = None

                elif isinstance(event, AgentDone):
                    if thinking_block is not None:
                        thinking_block.finish()
                    if agent_block is not None:
                        agent_block.finish_streaming()
                    usage = event.usage
                    if usage:
                        feed.append_system(
                            f"done · {event.turns} turn(s) · "
                            f"in={usage.input_tokens} out={usage.output_tokens}",
                            "usage",
                        )
                        # Update inspector
                        try:
                            inspector = self.query_one("#inspector-widget", Inspector)
                            inspector.update_state(
                                tokens_in=inspector.state.tokens_in + usage.input_tokens,
                                tokens_out=inspector.state.tokens_out + usage.output_tokens,
                                tokens_used=inspector.state.tokens_used + usage.input_tokens + usage.output_tokens,
                                turns=inspector.state.turns + event.turns,
                                messages=len(self._history),
                            )
                        except Exception:
                            pass
                    self._set_composer_enabled(True)
                    return

                elif isinstance(event, AgentError):
                    if waiting is not None:
                        waiting.stop()
                        waiting = None
                    feed.append_error(event.message, meta="agent")
                    try:
                        inspector = self.query_one("#inspector-widget", Inspector)
                        inspector.update_state(last_error=event.message)
                    except Exception:
                        pass
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

            # Check for errors — show in feed, don't propagate traceback
            for t in done:
                if t.exception():
                    exc = t.exception()
                    feed.append_error(str(exc), meta="agent")
                    self._set_composer_enabled(True)
                    return

            # Update history with new messages
            if not agent_fut.cancelled():
                self._history = agent_fut.result()

        except asyncio.CancelledError:
            agent_fut.cancel()
            drain_fut.cancel()
        except Exception as exc:  # noqa: BLE001
            feed.append_error(str(exc), meta="agent")
            self._set_composer_enabled(True)

    # ------------------------------------------------------------------
    # rich tool cards
    # ------------------------------------------------------------------

    def _make_rich_card(self, tool_name: str, arguments: dict, result: str, error: bool):
        """Create a beautiful typed card based on tool name, or None for generic."""
        if tool_name == "terminal":
            cmd = arguments.get("command", "")
            lines = result.splitlines()
            # Parse exit code from result
            exit_code = 0
            output = result
            if lines and lines[0].startswith("[exit "):
                try:
                    exit_code = int(lines[0].split()[1].rstrip("]"))
                    output = "\n".join(lines[1:])
                except (ValueError, IndexError):
                    pass
            # Truncate long output
            out_lines = output.splitlines()
            if len(out_lines) > 15:
                output = "\n".join(out_lines[:12]) + f"\n… +{len(out_lines) - 12} more lines"
            return RunCard(cmd=cmd, output=output, exit_code=exit_code)

        elif tool_name == "read_file":
            path = arguments.get("path", "")
            lines_count = result.count("\n") + 1
            size = f"{len(result)} chars"
            return ReadCard(path=path, lines=lines_count, size=size)

        elif tool_name == "write_file":
            path = arguments.get("path", "")
            content = arguments.get("content", "")
            # Fake a simple diff: all lines are additions
            diff_lines = [("+", line) for line in content.splitlines()[:10]]
            if len(content.splitlines()) > 10:
                diff_lines.append(("", f"… +{len(content.splitlines()) - 10} more lines"))
            return EditCard(path=path, diff=diff_lines, plus=len(content.splitlines()), minus=0)

        elif tool_name == "search_files":
            pattern = arguments.get("pattern", "")
            hits = [l for l in result.splitlines() if l.strip()][:8]
            total = len(result.splitlines())
            return SearchCard(pattern=pattern, hits=hits, total=total)

        elif tool_name == "fetch_url":
            url = arguments.get("url", "")
            status = 200 if not error else 500
            size = f"{len(result)} chars"
            return FetchCard(url=url, status=status, size=size)

        return None  # generic ToolCallCard stays

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
