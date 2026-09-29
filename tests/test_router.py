"""Router: explicit, reasoned, deterministic context routing."""

from __future__ import annotations

import pytest

from noname_harness.router import RouteDecision, Router
from noname_harness.store import HarnessStore
from noname_harness.tools import Tool, ToolApprovalRequired, ToolRegistry, ToolSchema


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "router project")
    return store, root


def test_continue_when_context_has_room(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "artifact.changed", {"path": "a.py"})
        router = Router(store)
        decision = router.decide(session_id="s")
        assert decision.route == "continue"
        assert "room" in decision.reason
        # Recorded with its reason.
        event = next(e for e in store.list_events("s", limit=10) if e.event_type == "route.selected")
        assert event.payload["route"] == "continue"
        assert event.payload["signals"]["work_events"] >= 0
    finally:
        store.close()


def test_rebirth_when_saturated(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        for i in range(45):
            store.append_event("s", "artifact.changed", {"path": f"f{i}.py"})
        router = Router(store, saturate_at=40, fork_at=80)
        decision = router.decide(session_id="s")
        assert decision.route == "rebirth"
        assert "handoff" in decision.reason or "reborn" in decision.reason
    finally:
        store.close()


def test_fork_when_over_fork_threshold(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        for i in range(85):
            store.append_event("s", "artifact.changed", {"path": f"f{i}.py"})
        router = Router(store, saturate_at=40, fork_at=80)
        decision = router.decide(session_id="s")
        assert decision.route == "fork"
    finally:
        store.close()


def test_pending_approval_blocks_routing_to_continue(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(
            ToolSchema(name="w", description="d", input_schema={"x": "string"}),
            execute=lambda a: "ok", permission="write", approval="always",
        ))
        with pytest.raises(ToolApprovalRequired):
            registry.request("w", {"x": "y"}, session_id="s")
        # Even a saturated session stays on "continue" while awaiting approval.
        for i in range(100):
            store.append_event("s", "artifact.changed", {"path": f"f{i}.py"})
        router = Router(store)
        decision = router.decide(session_id="s")
        assert decision.route == "continue"
        assert "approval" in decision.reason.lower()
        assert decision.signals["pending_approvals"] == 1
    finally:
        store.close()


def test_user_instruction_is_decisive(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        router = Router(store)
        for instruction, expected in [
            ("fork", "fork"),
            ("rebirth", "rebirth"),
            ("重生", "rebirth"),
            ("switch", "switch_recipe"),
            ("subagent", "spawn_subagent"),
        ]:
            decision = router.decide(session_id="s", user_instruction=instruction)
            assert decision.route == expected, instruction
            assert "user instruction" in decision.reason
    finally:
        store.close()


def test_ambiguous_instruction_defaults_to_continue(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        router = Router(store)
        decision = router.decide(session_id="s", user_instruction="maybe do something?")
        # Never guess a destructive route from ambiguous input.
        assert decision.route == "continue"
    finally:
        store.close()


def test_router_never_modifies_long_term_memory(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        event = store.append_event("s", "project.constraint", {"key": "b", "content": {"text": "x"}})
        proposal = store.create_proposal("high", "b", {"text": "x"}, [event.id])
        store.review_proposal(proposal["id"], "accept", "user")
        before = store.active_state("high")
        router = Router(store)
        router.decide(session_id="s")
        # The decision is an event, not a change to durable state.
        assert store.active_state("high") == before
    finally:
        store.close()


def test_recipe_is_suggested_for_task_type(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        router = Router(store)
        decision = router.decide(session_id="s", task_type="code-change")
        assert decision.suggested_recipe == "code-change-balanced"
    finally:
        store.close()


def test_decision_validation(tmp_path):
    with pytest.raises(ValueError):
        RouteDecision(route="explode", reason="x", signals={})
    with pytest.raises(ValueError):
        RouteDecision(route="continue", reason="  ", signals={})
