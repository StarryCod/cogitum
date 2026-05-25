"""Tier-3 hardening: /godmode must keep persona_lock at the bottom of the
system prompt while godmode is on. Without it, swapping in a preset would
blow away the integrity guard that the gateway installs at startup —
leaving the bot vulnerable to 'ignore previous instructions' / forged
<system> injections coming through user messages.

These tests drive _handle_command directly with a stub bot constructed
via __new__ + manual attrs (same isolation pattern as
test_tool_execution_correctness.py)."""
from __future__ import annotations

import collections

import pytest


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

class _FakeAPI:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))


class _FakeCfg:
    def __init__(self, system: str, model: str = "anthropic/claude-sonnet-4.5"):
        self.system = system
        self.model = model
        self.yolo_mode = False


class _FakeAgent:
    def __init__(self, system: str) -> None:
        self.cfg = _FakeCfg(system=system)


class _FakeConfig:
    """Mirror real TelegramConfig surface so ACL tests don't drift.

    Production: allowed_chat_ids is list[int] and can_respond is kw-only
    (signature `def can_respond(self, *, user_id, chat_id)`). Old fake
    used set + positional, both silent footguns.
    """

    def __init__(
        self,
        *,
        allowed_user_id: int = 1,
        allowed_chat_ids: list[int] | None = None,
    ) -> None:
        self.allowed_user_id = allowed_user_id
        self.allowed_chat_ids: list[int] = list(allowed_chat_ids or [])

    def can_respond(self, *, user_id: int, chat_id: int) -> bool:
        if self.allowed_chat_ids and chat_id in self.allowed_chat_ids:
            return True
        if self.allowed_user_id and user_id == self.allowed_user_id:
            return True
        if not self.allowed_user_id and not self.allowed_chat_ids:
            return chat_id == user_id
        return False


class _Session:
    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id


def _make_bot(starting_system: str = "BASE_PERSONA"):
    """Construct a CogitumBot without running __init__.

    We bypass __init__ on purpose — the real one wires up an asyncio
    Semaphore bound to the running loop and opens a poll-offset file.
    For these unit tests we only need the surface _handle_command
    touches in the godmode branch.
    """
    from cogitum.gateway.telegram import CogitumBot

    bot = CogitumBot.__new__(CogitumBot)
    bot.config = _FakeConfig()  # type: ignore
    bot.api = _FakeAPI()  # type: ignore
    bot.agent = _FakeAgent(system=starting_system)  # type: ignore
    bot.sessions = {}
    bot.mesh = None
    bot._pre_godmode_system = None
    bot._approval_token_to_call_id = collections.OrderedDict()
    bot._approval_token_max = 64
    bot._seen_callbacks = collections.OrderedDict()
    bot._seen_callbacks_max = 256
    return bot


def _msg(chat_id: int = 1, user_id: int = 1) -> dict:
    return {"chat": {"id": chat_id}, "from": {"id": user_id}}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

LOCK_MARKER = "INSTRUCTION INTEGRITY LOCK"


@pytest.mark.asyncio
async def test_godmode_explicit_preset_keeps_persona_lock():
    """/godmode plinian → cfg.system contains preset text AND lock."""
    from cogitum.core.godmode import get_preset

    bot = _make_bot(starting_system="BASE_PERSONA")
    session = _Session(chat_id=1)

    await bot._handle_command("/godmode plinian", session, _msg())

    sys_prompt = bot.agent.cfg.system
    plinian_text = get_preset("plinian")
    assert plinian_text and plinian_text.strip() in sys_prompt, (
        "preset body must be present in the active system prompt"
    )
    assert LOCK_MARKER in sys_prompt, (
        "persona_lock marker must remain after /godmode swap"
    )
    # Lock sits at the bottom — overrides everything above it.
    assert sys_prompt.rstrip().endswith(
        sys_prompt[sys_prompt.rfind(LOCK_MARKER):].rstrip()
    )


@pytest.mark.asyncio
async def test_godmode_auto_branch_keeps_persona_lock():
    """/godmode (no arg) and /godmode on / auto take the auto-pick
    branch. All three must wrap the picked preset with the lock."""
    from cogitum.core.godmode import auto_pick_preset, get_preset

    for sub in ("", "on", "auto"):
        bot = _make_bot(starting_system="BASE_PERSONA")
        session = _Session(chat_id=1)

        cmd = "/godmode" if not sub else f"/godmode {sub}"
        await bot._handle_command(cmd, session, _msg())

        picked = auto_pick_preset(bot.agent.cfg.model)
        body = get_preset(picked) or ""
        sys_prompt = bot.agent.cfg.system
        assert body.strip() in sys_prompt, f"sub={sub!r} missing preset body"
        assert LOCK_MARKER in sys_prompt, f"sub={sub!r} lost persona_lock"


@pytest.mark.asyncio
async def test_godmode_off_restores_pre_godmode_system_with_lock():
    """/godmode off must restore exactly the value captured before the
    first swap. M5 false-positive fix: previous version started with
    a stub string that already contained the LOCK_MARKER, so the post-
    disarm `LOCK_MARKER in cfg.system` assertion was free. Now we
    start with wrap_system_prompt(base) — production-realistic — so
    the marker is genuinely from the lock wrap, not from input."""
    from cogitum.gateway.persona_lock import wrap_system_prompt

    starting = wrap_system_prompt("BASE_PERSONA")
    # Sanity: production wrap really did add the marker.
    assert LOCK_MARKER in starting

    bot = _make_bot(starting_system=starting)
    session = _Session(chat_id=1)

    # Arm
    await bot._handle_command("/godmode plinian", session, _msg())
    assert bot.agent.cfg.system != starting

    # Disarm
    await bot._handle_command("/godmode off", session, _msg())
    # Strict equality — the captured value is restored verbatim.
    assert bot.agent.cfg.system == starting
    assert bot._pre_godmode_system is None
    # Marker присутствует именно из-за prod-realistic wrap, не из stub.
    assert LOCK_MARKER in bot.agent.cfg.system


@pytest.mark.asyncio
async def test_consecutive_godmode_swaps_do_not_double_stack_lock():
    """Two /godmode <preset> calls in a row: the second must read from
    the raw preset, not from cfg.system. So the lock must appear
    exactly once in the final system prompt."""
    bot = _make_bot(starting_system="BASE_PERSONA")
    session = _Session(chat_id=1)

    await bot._handle_command("/godmode plinian", session, _msg())
    await bot._handle_command("/godmode subtle", session, _msg())

    sys_prompt = bot.agent.cfg.system
    occurrences = sys_prompt.count(LOCK_MARKER)
    assert occurrences == 1, (
        f"expected exactly one persona_lock, found {occurrences}"
    )

    # _pre_godmode_system must still hold the ORIGINAL pre-godmode value,
    # not the first-swap result. Otherwise /godmode off after a chain of
    # swaps would restore the wrong baseline.
    assert bot._pre_godmode_system == "BASE_PERSONA"


def test_wrap_system_prompt_idempotent_for_godmode_use():
    """Sanity: wrap_system_prompt(preset) is what /godmode now applies.
    Confirm the function appends the lock and is stable for our call
    pattern (we never feed it an already-wrapped string — every swap
    starts from a raw preset)."""
    from cogitum.gateway.persona_lock import wrap_system_prompt
    from cogitum.core.godmode import get_preset

    raw = get_preset("plinian") or ""
    wrapped = wrap_system_prompt(raw)
    assert raw.strip() in wrapped
    assert LOCK_MARKER in wrapped
    # Empty / None still produces a lock-only prompt — agents without a
    # configured persona still get the integrity guard.
    assert LOCK_MARKER in wrap_system_prompt("")
    assert LOCK_MARKER in wrap_system_prompt(None)
