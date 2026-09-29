"""Agent loop: explicit state machine, logged transitions, stream recovery."""

from __future__ import annotations

import pytest

from noname_harness.agent_loop import AgentLoop, AgentLoopError, LoopResult
from noname_harness.store import HarnessStore


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "loop project")
    return store, root


class ScriptedDriver:
    """A deterministic driver that replays a queue of LoopResults."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    def act(self, context, last_tool_result=None):
        self.calls += 1
        if not self._results:
            return LoopResult(task_complete=True)
        return self._results.pop(0)


def test_happy_path_completes_and_logs_transitions(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        driver = ScriptedDriver([LoopResult(task_complete=True)])
        loop = AgentLoop(store=store, session_id="s", driver=driver)
        summary = loop.run("do the thing")
        assert summary["final_state"] == "COMPLETED"
        assert summary["stop_reason"] == "task_complete"
        assert summary["rounds"] == 1
        # The full path is in the ledger.
        states = [
            e.payload["to"] for e in store.list_events("s", limit=100)
            if e.event_type == "loop.transition"
        ]
        # newest-first; reverse for chronological
        assert list(reversed(states))[:4] == [
            "ASSEMBLING_CONTEXT", "SELECTING_MODEL", "CALLING_MODEL", "APPLYING_RESULT",
        ]
        assert any(e.event_type == "loop.finished" for e in store.list_events("s", limit=100))
    finally:
        store.close()


def test_tool_call_turn_goes_through_waiting_tool(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        driver = ScriptedDriver([
            LoopResult(tool_call={"name": "search", "result": ["hit"]}),
            LoopResult(task_complete=True),
        ])
        loop = AgentLoop(store=store, session_id="s", driver=driver)
        summary = loop.run("research")
        assert summary["final_state"] == "COMPLETED"
        assert summary["rounds"] == 2
        states = [
            e.payload["to"] for e in reversed(
                [e for e in store.list_events("s", limit=100) if e.event_type == "loop.transition"]
            )
        ]
        assert "WAITING_TOOL" in states
    finally:
        store.close()


def test_round_limit_stops_the_loop(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        # Driver never completes; loop must stop at max_rounds.
        driver = ScriptedDriver([LoopResult(output="again") for _ in range(50)])
        loop = AgentLoop(store=store, session_id="s", driver=driver, max_rounds=3)
        summary = loop.run("loop forever")
        assert summary["final_state"] == "FAILED"
        assert summary["stop_reason"] == "round_limit"
        assert summary["rounds"] == 3
    finally:
        store.close()


def test_budget_limit_stops_before_round_limit(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        driver = ScriptedDriver([LoopResult(output="x") for _ in range(50)])
        loop = AgentLoop(store=store, session_id="s", driver=driver, max_rounds=10, budget_rounds=2)
        summary = loop.run("budgeted")
        assert summary["stop_reason"] == "budget_limit"
        assert summary["rounds"] == 2
    finally:
        store.close()


def test_driver_exception_becomes_failed_not_a_crash(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        class BoomDriver:
            def act(self, context, last_tool_result=None):
                raise RuntimeError("driver exploded")
        loop = AgentLoop(store=store, session_id="s", driver=BoomDriver())
        summary = loop.run("boom")
        assert summary["final_state"] == "FAILED"
        assert summary["stop_reason"] == "unrecoverable_error"
        assert "driver exploded" in summary["error"]
        assert any(e.event_type == "loop.error" for e in store.list_events("s", limit=100))
    finally:
        store.close()


def test_illegal_transition_is_rejected(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        loop = AgentLoop(store=store, session_id="s", driver=ScriptedDriver([]))
        # IDLE -> CALLING_MODEL is not a legal direct transition.
        with pytest.raises(AgentLoopError):
            loop._transition("CALLING_MODEL")
        # IDLE -> ASSEMBLING_CONTEXT is legal; then ASSEMBLING -> APPLYING_RESULT is not.
        loop._transition("ASSEMBLING_CONTEXT")
        with pytest.raises(AgentLoopError):
            loop._transition("APPLYING_RESULT")
    finally:
        store.close()


def test_run_only_from_idle(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        driver = ScriptedDriver([LoopResult(task_complete=True)])
        loop = AgentLoop(store=store, session_id="s", driver=driver)
        loop.run("first")
        with pytest.raises(AgentLoopError):
            loop.run("second")
    finally:
        store.close()


def test_recovery_rebuilds_state_from_event_stream(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        driver = ScriptedDriver([LoopResult(output="x"), LoopResult(task_complete=True)])
        loop = AgentLoop(store=store, session_id="s", driver=driver)
        loop.run("task")
        # A fresh loop reconstructed from the same store must see the same state.
        recovered = AgentLoop.reconstruct(store, "s", ScriptedDriver([]))
        assert recovered.state == "COMPLETED"
        assert recovered.rounds == loop.rounds
        # Recovery does not depend on the original in-memory loop object.
        assert recovered.rounds == 2
    finally:
        store.close()


def test_waiting_approval_cancels_cleanly(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        driver = ScriptedDriver([LoopResult(stop_reason="waiting_approval")])
        loop = AgentLoop(store=store, session_id="s", driver=driver)
        summary = loop.run("needs approval")
        assert summary["final_state"] == "CANCELLED"
        assert summary["stop_reason"] == "waiting_approval"
    finally:
        store.close()


def test_context_package_is_assembled_and_linked(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        driver = ScriptedDriver([LoopResult(task_complete=True)])
        loop = AgentLoop(store=store, session_id="s", driver=driver)
        summary = loop.run("task", task_type="code-change")
        # The loop assembled a real context package and linked it in the summary.
        assert summary["package_id"] is not None
        package = store.get_context_package(summary["package_id"])
        assert package["assembly"]["recipe"]["id"] == "code-change-balanced"
    finally:
        store.close()
