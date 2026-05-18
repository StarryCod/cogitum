"""Tests for cogitum.core.legion — recursive sub-agent orchestrator.

The runtime is isolated from any LLM provider via a worker callable
plugged in at test setup. We exercise:

  * L1 dispatch (single + multiple cogitators in parallel)
  * L2 spawning from inside an L1 worker
  * MAX_DEPTH enforcement (L2 cannot recurse)
  * Sibling roster rendering
  * Async messaging (point-to-point + broadcast)
  * Inbox drain semantics
  * Aggregation / summary shape
  * Cancellation cascade
  * Error propagation (failed L2 doesn't kill L1 silently)
"""
from __future__ import annotations

import asyncio
import pytest


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────


def _fresh_legion():
    """Build a *new* Legion (not the singleton) for test isolation.

    The module-level `get_legion()` is process-wide; tests need their
    own instance so worker registration in one test doesn't leak.
    """
    from cogitum.core.legion import Legion
    return Legion()


# ─────────────────────────────────────────────────────────────────────────
# Worker stubs — drop-in replacements for the LLM-backed cogitator
# ─────────────────────────────────────────────────────────────────────────


def make_echo_worker():
    """Worker that just returns 'echo: <goal>'. Trivial, for L1-only tests."""
    async def worker(node, run, send_message, spawn_l2):
        await asyncio.sleep(0)  # one event-loop tick — keeps it concurrent
        return f"echo: {node.goal}"
    return worker


def make_recursive_worker(l2_tasks_per_l1: list[list[dict]]):
    """L1 nodes spawn N L2 nodes each based on the supplied plan.

    plan[i] is the list of L2 task-dicts for L1 number i (in dispatch
    order). After children return, L1 returns a string concatenating
    its own goal and the children outputs.
    """
    counter = {"i": 0}

    async def worker(node, run, send_message, spawn_l2):
        if node.depth == 1:
            i = counter["i"]
            counter["i"] += 1
            tasks = l2_tasks_per_l1[i] if i < len(l2_tasks_per_l1) else []
            if tasks and spawn_l2 is not None:
                child_outputs = await spawn_l2(tasks)
                joined = " | ".join(child_outputs)
                return f"L1[{node.goal}] children={joined}"
            return f"L1[{node.goal}] no-children"
        else:
            await asyncio.sleep(0)
            return f"L2[{node.goal}]"
    return worker


# ─────────────────────────────────────────────────────────────────────────
# L1 dispatch
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_single_l1():
    legion = _fresh_legion()
    legion.register_worker(make_echo_worker())
    run = await legion.start_run(
        root_goal="test",
        tasks=[{"goal": "alpha"}],
    )
    from cogitum.core.legion import NodeStatus

    assert len(run.nodes) == 1
    n = next(iter(run.nodes.values()))
    assert n.depth == 1
    assert n.status == NodeStatus.DONE
    assert n.output == "echo: alpha"
    assert run.summary
    assert "alpha" in run.summary
    assert run.is_complete()


@pytest.mark.asyncio
async def test_dispatch_multiple_l1_in_parallel():
    legion = _fresh_legion()
    legion.register_worker(make_echo_worker())
    run = await legion.start_run(
        root_goal="multi",
        tasks=[
            {"id": "A", "goal": "alpha"},
            {"id": "B", "goal": "beta"},
            {"id": "C", "goal": "gamma"},
        ],
    )
    from cogitum.core.legion import NodeStatus

    assert len(run.nodes) == 3
    ids = [n.id for n in run.l1_nodes()]
    assert set(ids) == {"A", "B", "C"}
    for n in run.l1_nodes():
        assert n.status == NodeStatus.DONE


@pytest.mark.asyncio
async def test_l1_count_limit_enforced():
    from cogitum.core.legion import MAX_L1_SIBLINGS
    legion = _fresh_legion()
    legion.register_worker(make_echo_worker())
    too_many = [{"goal": f"t{i}"} for i in range(MAX_L1_SIBLINGS + 1)]
    with pytest.raises(ValueError, match="max"):
        await legion.start_run(root_goal="x", tasks=too_many)


@pytest.mark.asyncio
async def test_empty_tasks_rejected():
    legion = _fresh_legion()
    legion.register_worker(make_echo_worker())
    with pytest.raises(ValueError, match="empty"):
        await legion.start_run(root_goal="x", tasks=[])


@pytest.mark.asyncio
async def test_worker_required():
    legion = _fresh_legion()
    with pytest.raises(RuntimeError, match="worker"):
        await legion.start_run(root_goal="x", tasks=[{"goal": "a"}])


# ─────────────────────────────────────────────────────────────────────────
# L2 spawning + depth limit
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_l1_spawns_l2():
    from cogitum.core.legion import NodeStatus

    legion = _fresh_legion()
    legion.register_worker(make_recursive_worker([
        [{"goal": "sub-a"}, {"goal": "sub-b"}],
    ]))
    run = await legion.start_run(
        root_goal="root",
        tasks=[{"id": "main", "goal": "do-stuff"}],
    )

    # main + 2 children
    assert len(run.nodes) == 3
    main = run.nodes["main"]
    assert main.depth == 1
    assert main.status == NodeStatus.DONE
    assert len(main.children) == 2

    children = run.children_of("main")
    assert {c.depth for c in children} == {2}
    for c in children:
        assert c.status == NodeStatus.DONE
        assert c.output.startswith("L2[")

    # main's output should reflect the children
    assert "L2[sub-a]" in main.output
    assert "L2[sub-b]" in main.output


@pytest.mark.asyncio
async def test_l2_cannot_spawn_l3():
    """spawn_l2 callable is None for L2 nodes — they have no way to
    recurse further. We assert the runtime never hands them a spawner."""
    spawner_seen_at_depths = []

    async def worker(node, run, send_message, spawn_l2):
        spawner_seen_at_depths.append((node.depth, spawn_l2 is not None))
        if node.depth == 1 and spawn_l2 is not None:
            await spawn_l2([{"goal": "leaf"}])
        return "ok"

    legion = _fresh_legion()
    legion.register_worker(worker)
    await legion.start_run(root_goal="r", tasks=[{"id": "p", "goal": "p"}])

    # L1 should have a spawner, L2 should not.
    by_depth = {d: has for d, has in spawner_seen_at_depths}
    assert by_depth[1] is True
    assert by_depth[2] is False


@pytest.mark.asyncio
async def test_l2_count_limit_enforced():
    from cogitum.core.legion import MAX_L2_SIBLINGS

    async def worker(node, run, send_message, spawn_l2):
        if node.depth == 1 and spawn_l2 is not None:
            tasks = [{"goal": f"sub-{i}"} for i in range(MAX_L2_SIBLINGS + 1)]
            await spawn_l2(tasks)
        return "ok"

    legion = _fresh_legion()
    legion.register_worker(worker)
    run = await legion.start_run(root_goal="r", tasks=[{"id": "p", "goal": "p"}])
    # Worker should have raised ValueError inside spawn_l2; the
    # runtime catches it and marks the L1 node FAILED.
    from cogitum.core.legion import NodeStatus
    p = run.nodes["p"]
    assert p.status == NodeStatus.FAILED
    assert "max" in p.error.lower()


# ─────────────────────────────────────────────────────────────────────────
# Sibling roster rendering
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_roster_shows_self_parent_siblings():
    from cogitum.core.legion import render_roster_for, NodeStatus

    seen_rosters: dict[str, str] = {}

    async def worker(node, run, send_message, spawn_l2):
        seen_rosters[node.id] = render_roster_for(run, node.id)
        return "done"

    legion = _fresh_legion()
    legion.register_worker(worker)
    await legion.start_run(
        root_goal="ROOT",
        tasks=[
            {"id": "alpha", "goal": "do alpha"},
            {"id": "beta", "goal": "do beta"},
            {"id": "gamma", "goal": "do gamma"},
        ],
    )

    # alpha's roster must mention itself, parent (L0/ROOT), siblings (beta/gamma).
    a = seen_rosters["alpha"]
    assert "[alpha]" in a
    assert "[L0]" in a
    assert "ROOT" in a
    assert "[beta]" in a
    assert "[gamma]" in a
    # alpha must be marked YOU.
    assert "YOU" in a


@pytest.mark.asyncio
async def test_roster_for_l2_shows_l1_parent_and_l2_siblings():
    from cogitum.core.legion import render_roster_for

    rosters: dict[str, str] = {}

    async def worker(node, run, send_message, spawn_l2):
        if node.depth == 1 and spawn_l2 is not None:
            await spawn_l2([
                {"id": "x", "goal": "do x"},
                {"id": "y", "goal": "do y"},
            ])
        else:
            rosters[node.id] = render_roster_for(run, node.id)
        return "ok"

    legion = _fresh_legion()
    legion.register_worker(worker)
    await legion.start_run(
        root_goal="r",
        tasks=[{"id": "p", "goal": "parent task"}],
    )

    # x's roster should reference p as parent and y as sibling.
    rx = rosters["p.x"]
    assert "[p]" in rx
    assert "parent task" in rx
    assert "[p.y]" in rx


# ─────────────────────────────────────────────────────────────────────────
# Async messaging
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_message_delivered_to_specific_recipient():
    received: dict[str, list[str]] = {}

    async def worker(node, run, send_message, spawn_l2):
        from cogitum.core.legion import render_inbox_for
        # Each node logs its inbox AT START of run. To make the test
        # deterministic, alpha sends to beta, then beta runs after a
        # short delay (we sleep) to guarantee the message lands.
        if node.id == "alpha":
            send_message("beta", "hello from alpha")
            await asyncio.sleep(0.05)
            return "alpha-done"
        if node.id == "beta":
            await asyncio.sleep(0.02)  # let alpha's send_message land
            received["beta"] = [m for m in [render_inbox_for(node)] if m]
            return "beta-done"
        return "?"

    legion = _fresh_legion()
    legion.register_worker(worker)
    await legion.start_run(
        root_goal="r",
        tasks=[{"id": "alpha", "goal": "a"}, {"id": "beta", "goal": "b"}],
    )

    assert "beta" in received
    assert received["beta"]
    assert "hello from alpha" in received["beta"][0]


@pytest.mark.asyncio
async def test_message_broadcast_to_all_except_sender():
    """Broadcast (recipient='*') reaches every other node, never the sender."""
    inboxes: dict[str, list[str]] = {n: [] for n in ("a", "b", "c")}

    async def worker(node, run, send_message, spawn_l2):
        from cogitum.core.legion import render_inbox_for
        if node.id == "a":
            send_message("*", "broadcast")
            await asyncio.sleep(0.05)
            inboxes["a"].append(render_inbox_for(node))
            return "a"
        await asyncio.sleep(0.02)
        inboxes[node.id].append(render_inbox_for(node))
        return node.id

    legion = _fresh_legion()
    legion.register_worker(worker)
    await legion.start_run(
        root_goal="r",
        tasks=[{"id": "a", "goal": "a"}, {"id": "b", "goal": "b"},
               {"id": "c", "goal": "c"}],
    )

    # b and c each got the broadcast; a did not (sender excluded).
    assert any("broadcast" in m for m in inboxes["b"])
    assert any("broadcast" in m for m in inboxes["c"])
    assert not any("broadcast" in m for m in inboxes["a"] if m)


@pytest.mark.asyncio
async def test_inbox_drained_on_render():
    from cogitum.core.legion import render_inbox_for, LegionNode, NodeStatus

    n = LegionNode(id="x", parent_id=None, depth=1, goal="g")
    from cogitum.core.legion import LegionMessage
    n.inbox.append(LegionMessage(sender_id="y", recipient_id="x", body="hi"))
    out1 = render_inbox_for(n)
    assert "hi" in out1
    # Second render should return empty — inbox was drained.
    out2 = render_inbox_for(n)
    assert out2 == ""


@pytest.mark.asyncio
async def test_message_to_unknown_id_is_dropped_silently():
    """Sending to a non-existent recipient must not crash the sender."""
    async def worker(node, run, send_message, spawn_l2):
        send_message("ghost", "anyone there?")
        return "ok"

    legion = _fresh_legion()
    legion.register_worker(worker)
    run = await legion.start_run(root_goal="r", tasks=[{"id": "alone", "goal": "a"}])
    from cogitum.core.legion import NodeStatus
    assert run.nodes["alone"].status == NodeStatus.DONE


# ─────────────────────────────────────────────────────────────────────────
# Aggregation / summary
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_summary_lists_each_l1_with_status_and_output():
    legion = _fresh_legion()
    legion.register_worker(make_echo_worker())
    run = await legion.start_run(
        root_goal="r",
        tasks=[{"id": "a", "goal": "alpha"}, {"id": "b", "goal": "beta"}],
    )

    s = run.summary
    assert "[a]" in s and "alpha" in s
    assert "[b]" in s and "beta" in s
    assert "status: done" in s
    assert "output: echo: alpha" in s
    assert "output: echo: beta" in s


@pytest.mark.asyncio
async def test_summary_includes_failure_reason():
    async def worker(node, run, send_message, spawn_l2):
        if node.goal == "boom":
            raise RuntimeError("kaboom")
        return "ok"

    legion = _fresh_legion()
    legion.register_worker(worker)
    run = await legion.start_run(
        root_goal="r",
        tasks=[{"id": "good", "goal": "ok"}, {"id": "bad", "goal": "boom"}],
    )

    from cogitum.core.legion import NodeStatus
    assert run.nodes["good"].status == NodeStatus.DONE
    assert run.nodes["bad"].status == NodeStatus.FAILED
    s = run.summary
    assert "status: done" in s
    assert "status: failed" in s
    assert "kaboom" in s


# ─────────────────────────────────────────────────────────────────────────
# Cancellation
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_run_marks_active_nodes_cancelled():
    """cancel_run on a paused-by-sleep node should still mark it cancelled
    once the run completes."""
    from cogitum.core.legion import NodeStatus

    started = asyncio.Event()

    async def worker(node, run, send_message, spawn_l2):
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise
        return "should-not-reach"

    legion = _fresh_legion()
    legion.register_worker(worker)

    async def cancel_soon():
        await started.wait()
        await legion.cancel_run("run-?")  # bogus id — fine, no crash
        for rid in list(legion._runs):
            await legion.cancel_run(rid)

    # Race the run with a cancellation kick.
    cancel_task = asyncio.create_task(cancel_soon())
    # We can't await start_run to completion because it sleeps 10s.
    # Instead: kick off start_run, wait briefly, assert state.
    run_task = asyncio.create_task(
        legion.start_run(root_goal="r", tasks=[{"id": "n", "goal": "g"}])
    )
    await started.wait()
    await cancel_task
    # Worker is still in its sleep — but cancel_run doesn't wake it.
    # That's intentional: cancel marks state, worker will drop on next yield.
    # For this test we just verify the flag and state were set.
    run_id = next(iter(legion._runs))
    run = legion._runs[run_id]
    assert run.cancelled is True
    assert run.nodes["n"].status == NodeStatus.CANCELLED

    run_task.cancel()
    try:
        await run_task
    except (asyncio.CancelledError, Exception):
        pass


# ─────────────────────────────────────────────────────────────────────────
# Event stream
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_events_emitted_for_node_lifecycle():
    legion = _fresh_legion()
    legion.register_worker(make_echo_worker())

    # Start the run as a background task so we can subscribe before it ends.
    run_task = asyncio.create_task(
        legion.start_run(root_goal="r", tasks=[{"id": "z", "goal": "g"}])
    )
    # Give it a tick to register the run.
    await asyncio.sleep(0.01)
    runs = legion.active_runs()
    if not runs:
        # Already done — fall through to checking the produced events.
        run = await run_task
    else:
        run = runs[0]
        await run_task

    # Drain events from the queue (run is already complete by now).
    seen_kinds = []
    while not run.events.empty():
        ev = run.events.get_nowait()
        seen_kinds.append(ev.kind)

    assert "node_added" in seen_kinds
    assert "node_status" in seen_kinds
    assert "run_done" in seen_kinds


# ─────────────────────────────────────────────────────────────────────────
# Misc
# ─────────────────────────────────────────────────────────────────────────


def test_get_legion_returns_singleton():
    from cogitum.core.legion import get_legion
    a = get_legion()
    b = get_legion()
    assert a is b


def test_id_coercion_sanitises_special_chars():
    legion = _fresh_legion()
    # Direct call to the private helper — covers the sanitiser.
    assert legion._coerce_id("foo bar/baz", depth=1, default_n=0) == "foo_bar_baz"
    assert legion._coerce_id("", depth=1, default_n=3) == "L1-3"
    assert legion._coerce_id(None, depth=2, default_n=1) == "L2-1"
    assert legion._coerce_id("alpha-1.beta_2", depth=1, default_n=0) == "alpha-1.beta_2"
