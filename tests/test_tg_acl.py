"""Tier-3 ACL: lock /yolo, /godmode, /model, /resume, /reload, /models,
/compact and the inline `model:` / `resume:` / `approve:` / `reject:`
callbacks to the operator (allowed_user_id) so a group member can't
escalate by mutating shared Agent / session-store / mesh state.

These tests drive _handle_command / _handle_callback directly with a stub
bot constructed via __new__ + manual attrs (same isolation pattern as
test_tool_execution_correctness.py and test_godmode_persona_lock.py — the
real CogitumBot.__init__ binds an asyncio.Semaphore to the running loop
and opens a poll-offset file, both of which leak between tests).
"""
from __future__ import annotations

import collections
from unittest.mock import AsyncMock, MagicMock

import pytest

from cogitum.gateway.telegram import OPERATOR_ONLY_MSG
from cogitum.gateway.tg_formatter import escape_md

# Production sends the operator-only string through escape_md before
# MarkdownV2 parse — so on .sent inspection we look for the escaped
# form. Callbacks ship raw via answer_callback.
_ESCAPED_OPERATOR_ONLY = escape_md(OPERATOR_ONLY_MSG)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

class _FakeAPI:
    def __init__(self) -> None:
        self.sent: list[tuple] = []
        self.answered: list[tuple] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))

    async def answer_callback(self, cb_id, text=""):
        self.answered.append((cb_id, text))

    async def edit_message(self, chat_id, message_id, text, **kw):
        self.sent.append((chat_id, text, {"edit": True, **kw}))


class _FakeCfg:
    def __init__(self) -> None:
        self.system = "BASE"
        self.model = "anthropic/claude-sonnet-4.5"
        self.yolo_mode = False


class _FakeAgent:
    def __init__(self) -> None:
        self.cfg = _FakeCfg()
        self.submit_approval = MagicMock(return_value=True)
        # /compact path may call agent.compact_now — make it a no-op
        # AsyncMock so the operator-positive test (if added later) doesn't
        # explode. Non-operator must bounce before this gets called.
        self.compact_now = AsyncMock(return_value=([], 0, 0))


class _FakeConfig:
    """Stand-in for TelegramConfig.

    Real TelegramConfig.can_respond returns True for messages from
    allowed_chat_ids regardless of user_id — that's exactly the
    privilege-escalation surface we're protecting. Mirror that here so
    a test message from a random group user passes auth but should
    still be rejected by _is_operator.
    """

    def __init__(
        self,
        *,
        allowed_user_id: int = 1,
        allowed_chat_ids: list[int] | None = None,
    ) -> None:
        self.allowed_user_id = allowed_user_id
        self.allowed_chat_ids = list(allowed_chat_ids or [])

    def can_respond(self, *, user_id, chat_id):
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
        self.history: list = []
        self.session_id: str | None = None
        self.agent_task = None

    @property
    def is_busy(self) -> bool:
        # Real ChatSession reports busy iff agent_task is set and not
        # done. Mirror that so /stop's busy-check fires on operator-side
        # tests where we plant a fake unfinished task.
        return self.agent_task is not None and not self.agent_task.done()


def _make_bot(
    *,
    allowed_user_id: int = 1,
    allowed_chat_ids: list[int] | None = None,
):
    from cogitum.gateway.telegram import CogitumBot

    bot = CogitumBot.__new__(CogitumBot)
    bot.config = _FakeConfig(  # type: ignore
        allowed_user_id=allowed_user_id,
        allowed_chat_ids=allowed_chat_ids,
    )
    bot.api = _FakeAPI()  # type: ignore
    bot.agent = _FakeAgent()  # type: ignore
    bot.sessions = {}
    bot.mesh = MagicMock()
    bot.mesh.resolve = MagicMock(return_value=[])
    bot._pre_godmode_system = None
    bot._approval_token_to_call_id = collections.OrderedDict()
    bot._approval_token_max = 64
    bot._seen_callbacks = collections.OrderedDict()
    bot._seen_callbacks_max = 256
    return bot


def _msg(chat_id: int, user_id: int) -> dict:
    return {"chat": {"id": chat_id}, "from": {"id": user_id}, "message_id": 1}


def _cb(chat_id: int, user_id: int, data: str, cb_id: str = "cb-1") -> dict:
    return {
        "id": cb_id,
        "data": data,
        "from": {"id": user_id},
        "message": {"chat": {"id": chat_id}, "message_id": 99},
    }


# ---------------------------------------------------------------------------
# /yolo
# ---------------------------------------------------------------------------

OPERATOR = 1
GROUP_CHAT = -100123
GROUP_USER = 555


@pytest.mark.asyncio
async def test_operator_in_private_can_yolo_on():
    bot = _make_bot(allowed_user_id=OPERATOR, allowed_chat_ids=[GROUP_CHAT])
    session = _Session(chat_id=OPERATOR)

    await bot._handle_command("/yolo on", session, _msg(OPERATOR, OPERATOR))

    assert bot.agent.cfg.yolo_mode is True


@pytest.mark.asyncio
async def test_group_member_cannot_yolo_on():
    """Non-operator user in an allowed group must not flip yolo."""
    bot = _make_bot(allowed_user_id=OPERATOR, allowed_chat_ids=[GROUP_CHAT])
    session = _Session(chat_id=GROUP_CHAT)

    await bot._handle_command(
        "/yolo on", session, _msg(GROUP_CHAT, GROUP_USER),
    )

    assert bot.agent.cfg.yolo_mode is False, "yolo must NOT change for non-operator"
    # Reject message uses the unified OPERATOR_ONLY_MSG constant.
    sent = bot.api.sent
    assert any(_ESCAPED_OPERATOR_ONLY in t for _, t, _ in sent), (
        f"expected _ESCAPED_OPERATOR_ONLY, got {sent!r}"
    )


@pytest.mark.asyncio
async def test_group_member_cannot_yolo_off_either():
    """Even disarming yolo from a group is escalation — mirror image
    of the on-flip. The operator might have armed it intentionally."""
    bot = _make_bot(allowed_user_id=OPERATOR, allowed_chat_ids=[GROUP_CHAT])
    bot.agent.cfg.yolo_mode = True
    session = _Session(chat_id=GROUP_CHAT)

    await bot._handle_command(
        "/yolo off", session, _msg(GROUP_CHAT, GROUP_USER),
    )

    assert bot.agent.cfg.yolo_mode is True


# ---------------------------------------------------------------------------
# /godmode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_member_cannot_godmode():
    bot = _make_bot(allowed_user_id=OPERATOR, allowed_chat_ids=[GROUP_CHAT])
    original_system = bot.agent.cfg.system
    session = _Session(chat_id=GROUP_CHAT)

    await bot._handle_command(
        "/godmode plinian", session, _msg(GROUP_CHAT, GROUP_USER),
    )

    assert bot.agent.cfg.system == original_system, "system prompt must not change"
    assert bot._pre_godmode_system is None
    assert any(_ESCAPED_OPERATOR_ONLY in t for _, t, _ in bot.api.sent)


@pytest.mark.asyncio
async def test_operator_can_godmode():
    bot = _make_bot(allowed_user_id=OPERATOR, allowed_chat_ids=[GROUP_CHAT])
    session = _Session(chat_id=OPERATOR)

    await bot._handle_command(
        "/godmode plinian", session, _msg(OPERATOR, OPERATOR),
    )

    # Either godmode armed (system swapped) or rejected with reason —
    # we want the swap to actually happen for operator.
    assert bot._pre_godmode_system == "BASE"
    assert bot.agent.cfg.system != "BASE"


# ---------------------------------------------------------------------------
# /model
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_member_cannot_switch_model():
    bot = _make_bot(allowed_user_id=OPERATOR, allowed_chat_ids=[GROUP_CHAT])
    bot.mesh.resolve = MagicMock(return_value=[
        MagicMock(qualified_id="anthropic/claude-haiku-4")
    ])
    original_model = bot.agent.cfg.model
    session = _Session(chat_id=GROUP_CHAT)

    await bot._handle_command(
        "/model haiku", session, _msg(GROUP_CHAT, GROUP_USER),
    )

    assert bot.agent.cfg.model == original_model
    assert any(_ESCAPED_OPERATOR_ONLY in t for _, t, _ in bot.api.sent)


# ---------------------------------------------------------------------------
# Inline `model:` callback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_member_cannot_use_model_callback():
    bot = _make_bot(allowed_user_id=OPERATOR, allowed_chat_ids=[GROUP_CHAT])
    original_model = bot.agent.cfg.model

    await bot._handle_callback(
        _cb(GROUP_CHAT, GROUP_USER, "model:anthropic/claude-haiku-4"),
    )

    assert bot.agent.cfg.model == original_model
    assert any(text == OPERATOR_ONLY_MSG for _, text in bot.api.answered)


# ---------------------------------------------------------------------------
# `approve:` / `reject:` callbacks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_member_cannot_approve_pending_call():
    """A pending tool-approval token belongs to whichever chat triggered it
    (in practice always the operator's private session — group members
    can't even reach approval prompts because of can_respond + tool routing).
    Even so: a non-operator click must NOT resolve the operator's pending
    future. submit_approval must never be called."""
    bot = _make_bot(allowed_user_id=OPERATOR, allowed_chat_ids=[GROUP_CHAT])
    bot._approval_token_to_call_id = collections.OrderedDict({"abc12345": "call_real_id"})

    await bot._handle_callback(
        _cb(GROUP_CHAT, GROUP_USER, "approve:abc12345"),
    )

    bot.agent.submit_approval.assert_not_called()
    # Token must NOT be popped — operator can still click their own.
    assert "abc12345" in bot._approval_token_to_call_id
    assert any(text == OPERATOR_ONLY_MSG for _, text in bot.api.answered)


@pytest.mark.asyncio
async def test_group_member_cannot_reject_pending_call():
    bot = _make_bot(allowed_user_id=OPERATOR, allowed_chat_ids=[GROUP_CHAT])
    bot._approval_token_to_call_id = collections.OrderedDict({"deadbeef": "call_real_id"})

    await bot._handle_callback(
        _cb(GROUP_CHAT, GROUP_USER, "reject:deadbeef"),
    )

    bot.agent.submit_approval.assert_not_called()
    assert "deadbeef" in bot._approval_token_to_call_id


@pytest.mark.asyncio
async def test_operator_can_approve_in_private():
    bot = _make_bot(allowed_user_id=OPERATOR, allowed_chat_ids=[GROUP_CHAT])
    bot._approval_token_to_call_id = collections.OrderedDict({"abc12345": "call_real_id"})

    await bot._handle_callback(
        _cb(OPERATOR, OPERATOR, "approve:abc12345"),
    )

    bot.agent.submit_approval.assert_called_once()
    args, kwargs = bot.agent.submit_approval.call_args
    # Accept either positional or keyword; the contract is (call_id, decision).
    call_id = kwargs.get("call_id", args[0] if args else None)
    decision = kwargs.get("decision", args[1] if len(args) > 1 else None)
    assert call_id == "call_real_id"
    assert decision == "approve"
    # Token consumed.
    assert "abc12345" not in bot._approval_token_to_call_id


# ---------------------------------------------------------------------------
# /resume — CRIT from adversarial review (cross-chat session theft)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_member_cannot_resume_session(monkeypatch):
    """Non-operator /resume must NOT touch the global session store.

    Без ACL: hostile группа жмёт /resume → store.list_sessions(limit=20)
    отдаёт операторские приватные сессии в виде кнопок. Click → загрузка
    операторской истории в групповую сессию + последующая запись через
    replace_messages.
    """
    bot = _make_bot(allowed_user_id=OPERATOR, allowed_chat_ids=[GROUP_CHAT])

    fake_store = MagicMock()
    fake_store.list_sessions = MagicMock(return_value=[
        MagicMock(id="op-secret-1", title="Operator's API keys")
    ])
    monkeypatch.setattr(
        "cogitum.gateway.telegram.get_store", lambda: fake_store
    )

    session = _Session(chat_id=GROUP_CHAT)
    await bot._handle_command(
        "/resume", session, _msg(GROUP_CHAT, GROUP_USER),
    )

    # Critical: store не должен быть прочитан non-operator-ом.
    fake_store.list_sessions.assert_not_called()
    assert any(_ESCAPED_OPERATOR_ONLY in t for _, t, _ in bot.api.sent)


@pytest.mark.asyncio
async def test_group_member_cannot_resume_callback(monkeypatch):
    """resume:<id> click must NOT load session history."""
    bot = _make_bot(allowed_user_id=OPERATOR, allowed_chat_ids=[GROUP_CHAT])

    fake_store = MagicMock()
    fake_store.load_session = MagicMock(return_value=[])
    fake_store.get_meta = MagicMock(return_value=None)
    monkeypatch.setattr(
        "cogitum.gateway.telegram.get_store", lambda: fake_store
    )

    await bot._handle_callback(
        _cb(GROUP_CHAT, GROUP_USER, "resume:op-secret-1"),
    )

    fake_store.load_session.assert_not_called()
    # Group session не получил операторскую историю.
    assert GROUP_CHAT not in bot.sessions or bot.sessions[GROUP_CHAT].history == []
    assert any(text == OPERATOR_ONLY_MSG for _, text in bot.api.answered)


# ---------------------------------------------------------------------------
# /reload, /models, /compact — HIGH from adversarial review
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_member_cannot_trigger_mesh_reload():
    """/reload вызывает _reload_mesh — провайдеры refresh, mesh swap,
    Agent.cfg.model swap. Non-operator не должен трогать."""
    bot = _make_bot(allowed_user_id=OPERATOR, allowed_chat_ids=[GROUP_CHAT])
    bot._reload_mesh = AsyncMock()
    session = _Session(chat_id=GROUP_CHAT)

    await bot._handle_command(
        "/reload", session, _msg(GROUP_CHAT, GROUP_USER),
    )

    bot._reload_mesh.assert_not_called()
    assert any(_ESCAPED_OPERATOR_ONLY in t for _, t, _ in bot.api.sent)


@pytest.mark.asyncio
async def test_group_member_cannot_open_models_picker():
    """/models тоже зовёт _reload_mesh(silent=True)."""
    bot = _make_bot(allowed_user_id=OPERATOR, allowed_chat_ids=[GROUP_CHAT])
    bot._reload_mesh = AsyncMock()
    session = _Session(chat_id=GROUP_CHAT)

    await bot._handle_command(
        "/models", session, _msg(GROUP_CHAT, GROUP_USER),
    )

    bot._reload_mesh.assert_not_called()
    assert any(_ESCAPED_OPERATOR_ONLY in t for _, t, _ in bot.api.sent)


@pytest.mark.asyncio
async def test_group_member_cannot_compact():
    """/compact запускает LLM-турн на shared agent (operator's keys)."""
    bot = _make_bot(allowed_user_id=OPERATOR, allowed_chat_ids=[GROUP_CHAT])
    session = _Session(chat_id=GROUP_CHAT)
    session.history = [{"role": "user", "content": "hi"}]

    await bot._handle_command(
        "/compact", session, _msg(GROUP_CHAT, GROUP_USER),
    )

    bot.agent.compact_now.assert_not_called()
    assert any(_ESCAPED_OPERATOR_ONLY in t for _, t, _ in bot.api.sent)


# ---------------------------------------------------------------------------
# Open mode (no allowlists)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_open_mode_only_private_user_is_operator():
    """No allowed_user_id, no allowed_chat_ids — only the 1:1 user
    (chat_id == user_id) qualifies as operator. Group context never does."""
    bot = _make_bot(allowed_user_id=0, allowed_chat_ids=[])

    # Group: rejected.
    bot_group_session = _Session(chat_id=GROUP_CHAT)
    await bot._handle_command(
        "/yolo on", bot_group_session, _msg(GROUP_CHAT, GROUP_USER),
    )
    assert bot.agent.cfg.yolo_mode is False

    # Private 1:1 (chat_id == user_id): allowed.
    pm_session = _Session(chat_id=42)
    await bot._handle_command(
        "/yolo on", pm_session, _msg(42, 42),
    )
    assert bot.agent.cfg.yolo_mode is True


@pytest.mark.asyncio
async def test_allowlisted_chat_without_operator_id_blocks_all_mutations():
    """allowed_chat_ids set but allowed_user_id=0 — refuse all mutations.
    There's no designated operator, so no-one can mutate shared Agent
    state safely. Group conversation continues to work (can_respond
    returns True) but mutating commands all bounce."""
    bot = _make_bot(allowed_user_id=0, allowed_chat_ids=[GROUP_CHAT])
    session = _Session(chat_id=GROUP_CHAT)

    await bot._handle_command(
        "/yolo on", session, _msg(GROUP_CHAT, GROUP_USER),
    )

    assert bot.agent.cfg.yolo_mode is False


# ---------------------------------------------------------------------------
# _is_operator parametrized table (M5)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "allowed_user,allowed_chats,user,chat,expected",
    [
        # Locked deploy.
        (1, [], 1, 1, True),                # operator PM
        (1, [], 2, 2, False),               # other user PM
        (1, [-100], 1, -100, True),         # operator in allowed group
        (1, [-100], 2, -100, False),        # group member in allowed group
        (1, [-100], 1, 1, True),            # operator's own private
        # Open mode (no locks).
        (0, [], 5, 5, True),                # private 1:1
        (0, [], 5, -100, False),            # group context never operator
        # Chat-only deploy (allowlist + no operator id).
        (0, [-100], 5, -100, False),        # no designated operator → refuse
        (0, [-100], 7, 7, False),           # private outside allowlist
        # Anonymous user / channel post.
        (0, [], 0, 0, False),
        (1, [-100], 0, -100, False),        # anon user in group, locked
    ],
)
def test_is_operator_table(allowed_user, allowed_chats, user, chat, expected):
    bot = _make_bot(
        allowed_user_id=allowed_user,
        allowed_chat_ids=list(allowed_chats),
    )
    assert bot._is_operator(user, chat) is expected



# ---------------------------------------------------------------------------
# Tier-4 R2: behavioural ACL coverage for /title /stop /new
# (the missing tests the R2 fix wave claimed but didn't actually add).
# Each pair: operator-from-private succeeds, group-member-non-operator
# is rejected and produces no state mutation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_member_cannot_title_session():
    """/title writes get_store().set_title(session_id, ...) — that's a
    write into the SHARED sqlite session store. A group member could
    rename the operator's saved private session via the same chat's
    session_id."""
    from unittest.mock import MagicMock, patch

    bot = _make_bot(allowed_user_id=OPERATOR, allowed_chat_ids=[GROUP_CHAT])
    session = _Session(chat_id=GROUP_CHAT)
    session.session_id = "private-session-id"

    fake_store = MagicMock()
    with patch("cogitum.gateway.telegram.get_store", return_value=fake_store):
        await bot._handle_command(
            "/title hijacked", session, _msg(GROUP_CHAT, GROUP_USER),
        )

    fake_store.set_title.assert_not_called()
    assert any(_ESCAPED_OPERATOR_ONLY in t for _, t, _ in bot.api.sent), (
        f"expected operator-only rejection, got {bot.api.sent!r}"
    )


@pytest.mark.asyncio
async def test_operator_can_title_in_private():
    from unittest.mock import MagicMock, patch

    bot = _make_bot(allowed_user_id=OPERATOR, allowed_chat_ids=[GROUP_CHAT])
    session = _Session(chat_id=OPERATOR)
    session.session_id = "abc-123"

    fake_store = MagicMock()
    with patch("cogitum.gateway.telegram.get_store", return_value=fake_store):
        await bot._handle_command(
            "/title my-session", session, _msg(OPERATOR, OPERATOR),
        )

    fake_store.set_title.assert_called_once_with("abc-123", "my-session")


@pytest.mark.asyncio
async def test_group_member_cannot_stop_running_agent():
    """/stop calls session.cancel() which kills the operator's running
    agent turn. From a group chat that's an escalation surface (DoS)."""
    bot = _make_bot(allowed_user_id=OPERATOR, allowed_chat_ids=[GROUP_CHAT])
    session = _Session(chat_id=GROUP_CHAT)

    # Mark the session as busy so /stop has something to cancel —
    # otherwise the no-op branch fires and we can't tell if the gate
    # short-circuited or the body short-circuited on emptiness.
    busy_task = MagicMock()
    busy_task.done = MagicMock(return_value=False)
    session.agent_task = busy_task
    cancelled = {"flag": False}
    def _mark_cancel():
        cancelled["flag"] = True
    session.cancel = _mark_cancel  # type: ignore[assignment]

    await bot._handle_command(
        "/stop", session, _msg(GROUP_CHAT, GROUP_USER),
    )

    assert cancelled["flag"] is False, "non-operator must not cancel agent turn"
    assert any(_ESCAPED_OPERATOR_ONLY in t for _, t, _ in bot.api.sent)


@pytest.mark.asyncio
async def test_operator_can_stop_in_private():
    bot = _make_bot(allowed_user_id=OPERATOR, allowed_chat_ids=[GROUP_CHAT])
    session = _Session(chat_id=OPERATOR)

    busy_task = MagicMock()
    busy_task.done = MagicMock(return_value=False)
    session.agent_task = busy_task
    cancelled = {"flag": False}
    def _mark_cancel():
        cancelled["flag"] = True
    session.cancel = _mark_cancel  # type: ignore[assignment]

    await bot._handle_command(
        "/stop", session, _msg(OPERATOR, OPERATOR),
    )

    assert cancelled["flag"] is True


@pytest.mark.asyncio
async def test_group_member_cannot_new_session():
    """/new wipes session.history + session.session_id. A non-operator
    group member could nuke the operator's running conversation."""
    bot = _make_bot(allowed_user_id=OPERATOR, allowed_chat_ids=[GROUP_CHAT])
    session = _Session(chat_id=GROUP_CHAT)
    session.history = [{"role": "user", "content": "operator's data"}]
    session.session_id = "operator-session-id"

    await bot._handle_command(
        "/new", session, _msg(GROUP_CHAT, GROUP_USER),
    )

    assert session.history == [{"role": "user", "content": "operator's data"}]
    assert session.session_id == "operator-session-id"
    assert any(_ESCAPED_OPERATOR_ONLY in t for _, t, _ in bot.api.sent)


@pytest.mark.asyncio
async def test_operator_can_new_session_in_private():
    bot = _make_bot(allowed_user_id=OPERATOR, allowed_chat_ids=[GROUP_CHAT])
    session = _Session(chat_id=OPERATOR)
    session.history = [{"role": "user", "content": "old"}]
    session.session_id = "old-id"

    await bot._handle_command(
        "/new", session, _msg(OPERATOR, OPERATOR),
    )

    assert session.history == []
    assert session.session_id is None
