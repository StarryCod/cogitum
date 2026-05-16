"""Cogitum TUI app — bubbles + specialised tool cards + model picker."""
from __future__ import annotations

import asyncio

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.widgets import Static

from .core.agent import Agent, AgentApprovalRequest, AgentConfig, AgentDone, AgentError, AgentRetry, AgentText, AgentThinking, AgentToolCall, AgentToolResult
from .core.builtin_tools import *  # noqa: F401,F403 — registers tools into REGISTRY
from .widgets.approval import ApprovalWidget
from .core.llm.loader import load_mesh, load_settings, write_settings
from .core.llm.mesh import Mesh, ResolvedModel
from .core.sessions import get_store, SessionStore
from .core.events import _id
from .core.tools import REGISTRY
from .setup_flow import SetupScreen
from .widgets.banner import Banner, BannerTags
from .widgets.composer import Composer, ComposerArea
from .widgets.cards import EditCard, WriteCard, RunCard, SearchCard, ReadCard, FetchCard
from .widgets.feed import AgentBlock, Feed, ThinkingBlock, ToolCallCard, WaitingIndicator
from .widgets.inspector import Inspector, InspectorState
from .widgets.model_picker import ModelPicker
from .widgets.queue_bar import QueueBar
from .widgets.session_picker import SessionPicker
from .widgets.statusbar import StatusBar


def _seed(feed: Feed) -> None:
    feed.append_system("Session ready  ·  type / for commands  ·  Ctrl+P for models", "init")
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
        Binding("ctrl+c", "copy_selection", "copy", priority=True),
        Binding("ctrl+q", "quit", "quit"),
        Binding("ctrl+p", "open_models", "models", priority=True),
        Binding("ctrl+s", "open_setup", "setup", priority=True),
        Binding("escape", "cancel_agent", "stop"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.mesh: Mesh | None = None
        self.settings: dict = {}
        self.current_model: str | None = None
        self._agent: Agent | None = None
        self._agent_task: asyncio.Task | None = None
        self._history: list = []   # list[Message] — persists across turns
        self._pending_messages: list[str] = []  # queued while agent is running
        self._inject_queue: asyncio.Queue[str] = asyncio.Queue()  # fed to agent between iterations
        # Session persistence
        self._session_id: str | None = None
        self._session_msg_count: int = 0  # messages already saved to disk

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
        yield QueueBar(id="queue-bar")
        yield Composer(id="composer")

    # ------------------------------------------------------------------
    # mount
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        feed = self.query_one("#feed-pane", Feed)
        _seed(feed)
        self._load_mesh_async()

    async def on_unmount(self) -> None:
        """Clean up resources on exit."""
        mesh = getattr(self, "mesh", None)
        if mesh is not None:
            await mesh.aclose()

    def _load_mesh_async(self) -> None:
        feed = self.query_one("#feed-pane", Feed)
        # Close old mesh if reloading (prevents httpx client leaks)
        old_mesh = getattr(self, "mesh", None)
        if old_mesh is not None:
            asyncio.ensure_future(old_mesh.aclose())
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

    def action_copy_selection(self) -> None:
        """Copy selected text to clipboard, or cancel agent if nothing selected."""
        selected = self.screen.get_selected_text()
        if selected:
            self.copy_to_clipboard(selected)
            self.screen.clear_selection()
            self.notify("Copied!", timeout=1.5)
        else:
            # No selection — act as cancel (Ctrl+C default behavior)
            self.action_cancel_agent()

    def action_cancel_agent(self) -> None:
        # Only handle Esc if agent is actually running — otherwise let it
        # propagate to modals (ModelPicker, SetupScreen) for their own dismiss
        if not self._agent_task or self._agent_task.done():
            return
        self._agent_task.cancel()
        # Cancel any running tool tasks via agent
        if self._agent and self._agent._active_tool_tasks:
            for t in self._agent._active_tool_tasks:
                if not t.done():
                    t.cancel()
            self._agent._active_tool_tasks = []
        feed = self.query_one("#feed-pane", Feed)
        # Remove any active WaitingIndicator
        for w in feed.query("WaitingIndicator"):
            w.stop()
        # Show stopped message
        feed.append_system("⏹ stopped by user", "esc")
        # Rebuild inject_queue from remaining pending (some may have been consumed)
        self._rebuild_inject_queue()
        # Process next queued message if any
        if self._pending_messages:
            next_msg = self._pending_messages.pop(0)
            self.query_one("#queue-bar", QueueBar).pop_first()
            self._rebuild_inject_queue()
            feed.append_user(next_msg)
            self._agent_task = asyncio.create_task(
                self._run_agent(next_msg, feed)
            )

    def action_open_models(self) -> None:
        # Always reload mesh from disk so newly-added providers/models
        # appear immediately (user may have edited providers.toml or
        # used /setup since last picker open).
        self._load_mesh_async()
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
        # Reload mesh + settings + agent so providers/models/default_model
        # changes from the wizard take effect immediately.
        self._load_mesh_async()
        self.query_one("#feed-pane", Feed).append_system(
            f"config reloaded — {len(self.mesh.list_resolved()) if self.mesh else 0} models available",
            "setup closed",
        )

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
    # ── Approval handler ───────────────────────────────────────────────────

    @on(ApprovalWidget.Decided)
    def _on_approval_widget_decided(self, event: ApprovalWidget.Decided) -> None:
        """Handle approval decision from TUI widget."""
        if self._approval_queue:
            self._approval_queue.put_nowait(event.decision)

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

        # If agent is running, queue the message for next turn
        if self._agent_task and not self._agent_task.done():
            self._pending_messages.append(text)
            self.query_one("#queue-bar", QueueBar).add(text)
            # Also push to inject_queue so agent picks it up between tool iterations
            self._inject_queue.put_nowait(text)
            return

        # Normal flow continues below
        if False:
            pass  # placeholder to keep elif chain valid
            return

        # Don't allow concurrent runs
        if self._agent_task and not self._agent_task.done():
            feed.append_system("agent is busy — press Esc to cancel", "busy")
            return

        feed.append_user(text)

        if self._agent is None or self.mesh is None:
            feed.append_error("No model selected. /models to pick one.")
            return

        self._agent_task = asyncio.create_task(
            self._run_agent(text, feed)
        )

    @on(ComposerArea.HistoryRequest)
    def _on_history_for_queue(self, event: ComposerArea.HistoryRequest) -> None:
        """Arrow up with empty input while agent running → pop last queued message back to input."""
        if event.direction != -1:
            return
        # Only intercept if agent is running and there are queued messages
        if not self._pending_messages:
            return
        if not self._agent_task or self._agent_task.done():
            return
        area = self.query_one("#composer-area", ComposerArea)
        if area.text.strip():
            return  # don't override if user is typing something
        # Pop last queued message back into composer
        text = self._pending_messages.pop()
        self.query_one("#queue-bar", QueueBar).pop_last()
        # Rebuild inject_queue without the popped message
        self._rebuild_inject_queue()
        area.load_text(text)
        event.stop()

    def _rebuild_inject_queue(self) -> None:
        """Rebuild inject_queue from current _pending_messages state."""
        # Drain old queue
        while not self._inject_queue.empty():
            try:
                self._inject_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        # Re-add current pending messages
        for msg in self._pending_messages:
            self._inject_queue.put_nowait(msg)

    # ------------------------------------------------------------------
    # agent worker
    # ------------------------------------------------------------------

    async def _run_agent(self, user_message: str, feed: Feed) -> None:
        """Run one agent turn, streaming events into the feed."""
        queue: asyncio.Queue = asyncio.Queue()
        approval_queue: asyncio.Queue = asyncio.Queue()
        self._approval_queue = approval_queue

        # Current agent block and thinking block (updated live)
        agent_block: AgentBlock | None = None
        thinking_block: ThinkingBlock | None = None
        waiting: WaitingIndicator | None = feed.append_waiting()
        # Map call_id → ToolCallCard for result updates
        tool_cards: dict[str, ToolCallCard] = {}
        # Map call_id → AgentToolCall event for rich card creation
        tool_calls_data: dict[str, AgentToolCall] = {}
        # Track streamed text for approximate token counting
        self._streamed_text = ""

        # Update inspector: new message sent, streaming starts
        try:
            inspector = self.query_one("#inspector-widget", Inspector)
            inspector.update_state(
                messages=len(self._history) + 1,
                is_streaming=True,
            )
        except Exception:
            pass

        async def drain_queue() -> None:
            nonlocal agent_block, thinking_block, waiting
            while True:
                event = await queue.get()

                if isinstance(event, AgentText):
                    # Stop waiting animation on first content
                    if waiting is not None:
                        waiting.stop()
                        waiting = None
                    if agent_block is None:
                        agent_block = feed.append_agent()
                    agent_block.append_delta(event.delta)
                    self._streamed_text += event.delta
                    # Realtime inspector update
                    try:
                        self.query_one("#inspector-widget", Inspector).stream_delta(len(event.delta))
                    except Exception:
                        pass

                elif isinstance(event, AgentThinking):
                    # Stop waiting animation on first thinking
                    if waiting is not None:
                        waiting.stop()
                        waiting = None
                    if thinking_block is None:
                        thinking_block = feed.append_thinking()
                    thinking_block.append_delta(event.delta)
                    # Realtime inspector update (thinking counts too)
                    try:
                        self.query_one("#inspector-widget", Inspector).stream_delta(len(event.delta))
                    except Exception:
                        pass

                elif isinstance(event, AgentRetry):
                    # Show/restore friendly waiting indicator during retry
                    # Pick a rotating label based on attempt number
                    labels = WaitingIndicator._RETRY_LABELS
                    label = labels[event.attempt % len(labels)]
                    if waiting is None:
                        waiting = feed.append_waiting()
                    waiting.set_status(label)

                elif isinstance(event, AgentToolCall):
                    # Stop waiting on first tool call
                    if waiting is not None:
                        waiting.stop()
                        waiting = None
                    # Finish thinking block if open
                    if thinking_block is not None:
                        thinking_block.finish()
                        thinking_block = None
                    # New turn → new agent block next time
                    agent_block = None

                    if getattr(event, "preliminary", False):
                        # Show "preparing..." card immediately
                        card = feed.append_tool_call(
                            event.tool_name, {}, event.call_id, preparing=True
                        )
                        tool_cards[event.call_id] = card
                    else:
                        # Full tool call — update existing card or create new
                        existing = tool_cards.get(event.call_id)
                        if existing:
                            existing.set_arguments(event.arguments)
                        else:
                            card = feed.append_tool_call(
                                event.tool_name, event.arguments, event.call_id
                            )
                            tool_cards[event.call_id] = card
                        tool_calls_data[event.call_id] = event

                elif isinstance(event, AgentApprovalRequest):
                    # Show approval widget and wait for user decision
                    from .widgets.approval import ApprovalWidget
                    approval_widget = ApprovalWidget(
                        tool_name=event.tool_name,
                        arguments=event.arguments,
                        call_id=event.call_id,
                        danger_level=event.danger_level,
                    )
                    feed.mount(approval_widget)
                    approval_widget.focus()

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
                    # Show waiting indicator while agent processes results
                    if waiting is None:
                        waiting = feed.append_waiting()
                        waiting.set_status("thinking…")

                elif isinstance(event, AgentDone):
                    if thinking_block is not None:
                        thinking_block.finish()
                    if agent_block is not None:
                        agent_block.finish_streaming()
                    usage = event.usage
                    # Approximate token count from streamed text if no usage reported
                    approx_out = len(self._streamed_text) // 4 if not usage else 0
                    approx_in = len(user_message) // 4 if not usage else 0

                    in_tokens = usage.input_tokens if usage else approx_in
                    out_tokens = usage.output_tokens if usage else approx_out
                    cache_read = usage.cache_read_tokens if usage else 0
                    cache_write = usage.cache_write_tokens if usage else 0

                    feed.append_system(
                        f"done · {event.turns} turn(s) · "
                        f"in≈{in_tokens} out≈{out_tokens}",
                        "usage",
                    )
                    # Update inspector with final counts + end streaming
                    try:
                        inspector = self.query_one("#inspector-widget", Inspector)
                        inspector.stream_end()
                        inspector.update_state(
                            tokens_in=inspector.state.tokens_in + in_tokens,
                            tokens_out=inspector.state.tokens_out + out_tokens,
                            tokens_used=inspector.state.tokens_used + in_tokens + out_tokens,
                            cache_read=inspector.state.cache_read + cache_read,
                            cache_write=inspector.state.cache_write + cache_write,
                            turns=inspector.state.turns + event.turns,
                            messages=len(self._history),
                        )
                    except Exception:
                        pass
                    return

                elif isinstance(event, AgentError):
                    if waiting is not None:
                        waiting.stop()
                        waiting = None
                    feed.append_error(event.message, meta="agent")
                    try:
                        inspector = self.query_one("#inspector-widget", Inspector)
                        inspector.stream_end()
                        inspector.update_state(last_error=event.message)
                    except Exception:
                        pass
                    return

        # Run agent + drain concurrently
        agent_coro = self._agent.run(
            user_message=user_message,
            history=self._history,
            queue=queue,
            inject_queue=self._inject_queue,
            approval_queue=approval_queue,
        )

        try:
            agent_fut = asyncio.create_task(agent_coro)
            drain_fut = asyncio.create_task(drain_queue())

            # Wait for agent to finish first
            await asyncio.wait([agent_fut], return_when=asyncio.FIRST_COMPLETED)

            # If agent crashed without sending AgentDone/AgentError, push error to queue
            if agent_fut.done() and agent_fut.exception():
                exc = agent_fut.exception()
                await queue.put(AgentError(message=str(exc), exc=exc))
            elif agent_fut.done() and not agent_fut.cancelled():
                # Agent finished normally but drain might still be waiting —
                # ensure it gets a terminal event if one wasn't sent
                # (safety net: put a sentinel AgentDone if queue is empty after agent)
                pass

            # Now wait for drain to process remaining events (including the error we just pushed)
            try:
                await asyncio.wait_for(drain_fut, timeout=10.0)
            except asyncio.TimeoutError:
                drain_fut.cancel()
                # Clean up any lingering waiting indicators
                for w in feed.query("WaitingIndicator"):
                    w.stop()
                # Mark any tool cards still in "running" state as timed out
                for cid, card in tool_cards.items():
                    if card._result is None:
                        card.set_result("(timed out — no result received)", error=True)
                # drain didn't finish — show error directly
                if agent_fut.done() and agent_fut.exception():
                    feed.append_error(str(agent_fut.exception()), meta="agent")

            # Update history with new messages (only on success)
            if agent_fut.done() and not agent_fut.cancelled() and not agent_fut.exception():
                self._history = agent_fut.result()
                # Persist new messages to disk
                self._save_session_delta()

        except asyncio.CancelledError:
            agent_fut.cancel()
            drain_fut.cancel()
            # DON'T clear pending_messages — user cancelled current turn,
            # but queued messages should still be processed on next submit.
            return
        except Exception as exc:  # noqa: BLE001
            feed.append_error(str(exc), meta="agent")

        # Sync: remove from _pending_messages any items that were already
        # injected by the agent mid-turn (they were consumed from inject_queue).
        # Whatever remains in _pending_messages but NOT in inject_queue was consumed.
        remaining_in_inject = []
        while not self._inject_queue.empty():
            try:
                remaining_in_inject.append(self._inject_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        # Only messages still in inject_queue were NOT consumed by agent
        # (e.g. agent finished without tool calls after they were queued).
        # Those are the ones we need to process as separate turns.
        self._pending_messages = remaining_in_inject
        # Update QueueBar to reflect actual remaining
        queue_bar = self.query_one("#queue-bar", QueueBar)
        queue_bar.clear()
        for msg in self._pending_messages:
            queue_bar.add(msg)

        # Process remaining queued messages as next turn
        if self._pending_messages:
            next_msg = self._pending_messages.pop(0)
            queue_bar.pop_first()
            feed.append_user(next_msg)
            self._agent_task = asyncio.create_task(
                self._run_agent(next_msg, feed)
            )

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
            lines_count = len(content.splitlines())
            size = f"{len(content)} chars"
            return WriteCard(path=path, lines=lines_count, size=size)

        elif tool_name == "edit_file":
            path = arguments.get("path", "")
            old_str = arguments.get("old_string", "")
            new_str = arguments.get("new_string", "")
            # Build real git-style diff
            diff_lines: list[tuple[str, str]] = []
            for line in old_str.splitlines()[:8]:
                diff_lines.append(("-", line))
            if len(old_str.splitlines()) > 8:
                diff_lines.append(("", f"… -{len(old_str.splitlines()) - 8} more"))
            for line in new_str.splitlines()[:8]:
                diff_lines.append(("+", line))
            if len(new_str.splitlines()) > 8:
                diff_lines.append(("", f"… +{len(new_str.splitlines()) - 8} more"))
            minus = len(old_str.splitlines())
            plus = len(new_str.splitlines())
            return EditCard(path=path, diff=diff_lines, plus=plus, minus=minus)

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

        elif tool_name == "web_search":
            query = arguments.get("query", "")
            hits = [l for l in result.splitlines() if l.strip() and not l.startswith("Search results")][:8]
            total = len([l for l in result.splitlines() if l.strip() and l[0:1].isdigit()])
            return SearchCard(pattern=query, hits=hits, total=total)

        elif tool_name == "browser":
            url = arguments.get("url", "") or "(active page)"
            action = arguments.get("action", "")
            status = 200 if not error else 500
            size = f"{action}: {len(result)} chars"
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
            self._start_new_session()
            self._history = []
            feed.clear()
            feed.append_system("new session — history cleared", "new")
            return

        if cmd == "title":
            if not rest:
                feed.append_system("usage: /title <name>", "help")
                return
            if self._session_id:
                get_store().set_title(self._session_id, rest)
                feed.append_system(f"session title: {rest}", "title")
            else:
                feed.append_system("no active session yet — send a message first", "warn")
            return

        if cmd == "resume":
            self._show_resume_modal()
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

        if cmd == "godmode":
            from .core.godmode import get_preset, list_presets, DEFAULT_PRESET
            if not rest or rest == "on":
                preset_name = DEFAULT_PRESET
                preset = get_preset(preset_name)
                self._agent.cfg.system = preset
                feed.append_system(f"godmode: {preset_name} — enabled", "godmode")
            elif rest == "off":
                from .core.agent import AgentConfig
                self._agent.cfg.system = AgentConfig.system
                feed.append_system("godmode: disabled — normal mode", "godmode")
            elif rest == "list":
                names = ", ".join(list_presets())
                feed.append_system(f"godmode presets: {names}", "godmode")
            else:
                preset = get_preset(rest)
                if preset:
                    self._agent.cfg.system = preset
                    feed.append_system(f"godmode: {rest} — enabled", "godmode")
                else:
                    feed.append_error(f"unknown preset: {rest} (try /godmode list)")
            return

        if cmd in ("quit", "exit", "q"):
            self.exit()
            return

        feed.append_error(f"unknown command: /{cmd}  (try /help)")

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------

    def _ensure_session(self) -> str:
        """Ensure a session exists, create one if needed. Returns session_id."""
        if self._session_id:
            return self._session_id
        store = get_store()
        meta = store.create_session(session_id=_id(), model=self.current_model or "")
        self._session_id = meta.id
        self._session_msg_count = 0
        return self._session_id

    def _save_session_delta(self) -> None:
        """Save only NEW messages (since last save) to disk."""
        if not self._history:
            return
        session_id = self._ensure_session()
        store = get_store()
        new_messages = self._history[self._session_msg_count:]
        if new_messages:
            store.append_messages(session_id, new_messages)
            self._session_msg_count = len(self._history)
            # Auto-title from first user message
            if self._session_msg_count <= 3:
                self._auto_title(session_id)
            # Update model in meta
            if self.current_model:
                store.set_model(session_id, self.current_model)

    def _auto_title(self, session_id: str) -> None:
        """Set session title from first user message (truncated to 50 chars)."""
        for msg in self._history:
            if msg.role == "user" and msg.text:
                title = msg.text.strip().replace("\n", " ")
                if len(title) > 50:
                    title = title[:47] + "..."
                get_store().set_title(session_id, title)
                return

    def _start_new_session(self) -> None:
        """Reset session state for /new command."""
        self._session_id = None
        self._session_msg_count = 0

    def _resume_session(self, session_id: str) -> None:
        """Load a session from disk."""
        store = get_store()
        messages = store.load_session(session_id)
        meta = store.get_meta(session_id)
        self._history = messages
        self._session_id = session_id
        self._session_msg_count = len(messages)
        # Rebuild feed with loaded messages
        feed = self.query_one("#feed-pane", Feed)
        feed.clear()
        for msg in messages:
            if msg.role == "user":
                feed.append_user(msg.text)
            elif msg.role == "assistant":
                if msg.text:
                    feed.append_agent(msg.text, meta="restored")
            elif msg.role == "tool":
                pass  # skip tool results in restored view
        title = meta.title if meta else session_id
        feed.append_system(f"resumed: {title} ({len(messages)} messages)", "resume")

    def _show_resume_modal(self) -> None:
        """Open the session picker modal."""
        def on_dismiss(session_id: str | None) -> None:
            if session_id:
                self._resume_session(session_id)
        self.push_screen(SessionPicker(), callback=on_dismiss)


def main() -> None:
    CogitumApp().run()


if __name__ == "__main__":
    main()
