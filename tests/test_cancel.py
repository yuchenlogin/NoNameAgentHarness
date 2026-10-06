"""Cancel API: event-driven, ledger-recorded, honoured at the round boundary."""

from __future__ import annotations

import pytest

from noname_harness.agent_loop import AgentLoop, AgentLoopError, LoopResult
from noname_harness.store import HarnessStore


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "cancel project")
    return store, root


class SlowDriver:
    def __init__(self, rounds=10):
        self._rounds = rounds

    def act(self, context, last_tool_result=None):
        self._rounds -= 1
        if self._rounds <= 0:
            return LoopResult(task_complete=True)
        return LoopResult(output="working")


def test_cancel_stops_loop_at_round_boundary(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        driver = SlowDriver(rounds=10)
        loop = AgentLoop(store=store, session_id="s", driver=driver, max_rounds=20)
        # Cancel before the run starts: the loop honours it at the first boundary.
        loop.cancel("user 要求停止")
        summary = loop.run("task")
        assert summary["final_state"] == "CANCELLED"
        assert summary["stop_reason"] == "user 要求停止"
        assert summary["rounds"] <= 1  # stopped at the first boundary
    finally:
        store.close()


def test_cancel_is_recorded_as_append_only_event(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        loop = AgentLoop(store=store, session_id="s", driver=SlowDriver())
        loop.cancel("user 要求停止")
        loop.run("task")
        types = [e.event_type for e in store.list_events("s", limit=50)]
        assert "loop.cancel_requested" in types
        # The cancellation is replayable from the event stream.
        event = next(e for e in store.list_events("s", limit=50) if e.event_type == "loop.cancel_requested")
        assert event.payload["reason"] == "user 要求停止"
    finally:
        store.close()


def test_external_actor_cancels_a_running_loop(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        # An external actor (another agent/CLI) cancels by writing the event,
        # without holding the loop object.
        loop = AgentLoop(store=store, session_id="s", driver=SlowDriver(rounds=10), max_rounds=20)
        store.append_event("s", "loop.cancel_requested", {"reason": "外部中断", "requested_at_round": 0})
        summary = loop.run("task")
        assert summary["final_state"] == "CANCELLED"
        assert summary["stop_reason"] == "外部中断"
    finally:
        store.close()


def test_cancel_reason_validation(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        loop = AgentLoop(store=store, session_id="s", driver=SlowDriver())
        with pytest.raises(ValueError):
            loop.cancel("   ")
    finally:
        store.close()


def test_terminal_state_does_not_respond_to_new_cancel(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        loop = AgentLoop(store=store, session_id="s", driver=SlowDriver(rounds=1))
        summary1 = loop.run("task")
        assert summary1["final_state"] == "COMPLETED"
        # After finishing, a cancel request does not change the recorded outcome.
        loop.cancel("太迟了")
        events = [e for e in store.list_events("s", limit=50)]
        finished = next(e for e in events if e.event_type == "loop.finished")
        assert finished.payload["final_state"] == "COMPLETED"
    finally:
        store.close()


def test_reconstruct_sees_cancelled_terminal_state(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        loop = AgentLoop(store=store, session_id="s", driver=SlowDriver())
        loop.cancel("中断")
        loop.run("task")
        recovered = AgentLoop.reconstruct(store, "s", SlowDriver())
        assert recovered.state == "CANCELLED"
    finally:
        store.close()
