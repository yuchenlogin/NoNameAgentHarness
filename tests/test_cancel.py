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


# --- 对抗性审查发现的回归 ---

def test_cancel_after_finished_cancels_next_run_not_ignored(tmp_path):
    """A cancel written after a finished run must cancel the NEXT run (not be vetoed)."""
    store, _ = make_store(tmp_path)
    try:
        # First run completes.
        loop1 = AgentLoop(store=store, session_id="s", driver=SlowDriver(rounds=1))
        assert loop1.run("task")["final_state"] == "COMPLETED"
        # A NEW cancel after that finish belongs to the next run.
        store.append_event("s", "loop.cancel_requested", {"reason": "停止下一次"})
        # The next run honours it (it is not vetoed by the first run's finish).
        loop2 = AgentLoop(store=store, session_id="s", driver=SlowDriver(rounds=10), max_rounds=20)
        summary = loop2.run("task2")
        assert summary["final_state"] == "CANCELLED"
        assert summary["stop_reason"] == "停止下一次"
    finally:
        store.close()


def test_cancel_works_across_store_connections(tmp_path):
    """A cancel from a SEPARATE HarnessStore connection (true cross-actor) works."""
    root = tmp_path / "project"
    root.mkdir()
    db = root / ".noname" / "harness.db"
    with HarnessStore(db) as store:
        store.initialize_project(root, "x")
        loop = AgentLoop(store=store, session_id="s", driver=SlowDriver(rounds=10), max_rounds=20)
        # A separate connection (another actor/process) writes the cancel.
        with HarnessStore(db) as other:
            other.append_event("s", "loop.cancel_requested", {"reason": "跨连接中断"})
        summary = loop.run("task")
        assert summary["final_state"] == "CANCELLED"
        assert summary["stop_reason"] == "跨连接中断"


def test_cancellation_check_is_o1_not_history_scan(tmp_path):
    """The cancel check must not decode the whole event history per round."""
    store, _ = make_store(tmp_path)
    try:
        for i in range(100):
            store.append_event("s", "note", {"text": f"event {i}"})
        loop = AgentLoop(store=store, session_id="s", driver=SlowDriver(rounds=2), max_rounds=5)
        # Instrument list_events to detect any full-history scan.
        calls = []
        original = store.list_events
        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            return original(*args, **kwargs)
        store.list_events = spy
        loop.run("task")
        # _cancellation_requested uses targeted MAX(seq) queries, not list_events scans.
        scan_calls = [c for c in calls if c[1].get("limit", 0) >= 1000]
        assert scan_calls == []
    finally:
        store.close()


def test_cli_cancel_validates_reason(tmp_path, capsys):
    from noname_harness.cli import main
    root = tmp_path / "project"
    root.mkdir()
    db = root / ".noname" / "harness.db"
    assert main(["init", "--db", str(db), "--root", str(root)]) == 0
    capsys.readouterr()
    # A blank reason is rejected (same validation as AgentLoop.cancel).
    assert main(["cancel", "--db", str(db), "--session", "s", "--reason", "   "]) == 2
    capsys.readouterr()
