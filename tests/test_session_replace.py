"""Tests for SessionStore.replace_messages — the atomic-rewrite path
used by /compact and the TG gateway's post-agent persistence.

Regression target: when agent.run() returns a shorter history (because
auto-compaction shrunk it mid-run), the TG gateway used to call
``append_messages`` which left the long pre-compaction tail on disk.
``/resume`` would then load (long old) + (short new) = duplicated and
contradictory history. ``replace_messages`` is the fix; this test
locks in its contract.
"""

from __future__ import annotations

from cogitum.core.events import Message, TextPart
from cogitum.core.sessions import SessionStore


def _user(text: str) -> Message:
    return Message(role="user", parts=[TextPart(text=text)])


def test_replace_messages_atomic_rewrite(tmp_path) -> None:
    store = SessionStore(base_dir=tmp_path)
    meta = store.create_session(session_id="s1", model="m1")

    # Seed: write 5 messages
    seed = [_user(f"old-{i}") for i in range(5)]
    store.append_messages(meta.id, seed)
    assert len(store.load_session(meta.id)) == 5

    # Now compact down to 2 messages and rewrite.
    compacted = [_user("briefing"), _user("recent")]
    store.replace_messages(meta.id, compacted)

    # Disk has exactly the new list, nothing more.
    on_disk = store.load_session(meta.id)
    assert len(on_disk) == 2
    assert on_disk[0].text == "briefing"
    assert on_disk[1].text == "recent"

    # Index counter resynced (was 5, now 2). Without this,
    # /resume would re-append starting from the wrong offset.
    fresh_meta = store.get_meta(meta.id)
    assert fresh_meta is not None
    assert fresh_meta.count == 2


def test_replace_messages_no_duplicate_after_compaction(tmp_path) -> None:
    """The exact bug pattern from the TG gateway: agent emits a long
    history, gets auto-compacted to a short one, gateway persists.
    Append would double-write; replace must not."""
    store = SessionStore(base_dir=tmp_path)
    meta = store.create_session(session_id="s1", model="m1")

    long_history = [_user(f"turn-{i}") for i in range(20)]
    store.append_messages(meta.id, long_history)

    # Simulate compaction: result is a single briefing + last few turns.
    compacted = [
        _user("briefing-summary"),
        *long_history[-3:],
    ]
    store.replace_messages(meta.id, compacted)

    on_disk = store.load_session(meta.id)
    assert len(on_disk) == 4
    assert on_disk[0].text == "briefing-summary"
    # No leakage of pre-compaction messages
    early = {m.text for m in long_history[:-3]}
    on_disk_texts = {m.text for m in on_disk}
    assert early.isdisjoint(on_disk_texts), (
        f"pre-compaction messages leaked into compacted history: "
        f"{early & on_disk_texts}"
    )


def test_replace_messages_preserves_index_for_other_sessions(tmp_path) -> None:
    """Replacing messages in one session must not touch another."""
    store = SessionStore(base_dir=tmp_path)
    s1 = store.create_session(session_id="s1", model="m1")
    s2 = store.create_session(session_id="s2", model="m2")

    store.append_messages(s1.id, [_user("a"), _user("b")])
    store.append_messages(s2.id, [_user("x"), _user("y"), _user("z")])

    store.replace_messages(s1.id, [_user("compacted")])

    assert len(store.load_session(s1.id)) == 1
    assert len(store.load_session(s2.id)) == 3
    assert store.get_meta(s1.id).count == 1
    assert store.get_meta(s2.id).count == 3
