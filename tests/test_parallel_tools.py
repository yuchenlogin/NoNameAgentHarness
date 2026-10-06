"""Parallel tool calls: several tools in one turn, each through the approval gate."""

from __future__ import annotations

import pytest

from noname_harness.adapters import AdapterDriver, LocalEchoAdapter, ModelResponse
from noname_harness.agent_loop import AgentLoop, AgentLoopError, LoopResult
from noname_harness.store import HarnessStore
from noname_harness.tools import Tool, ToolApprovalRequired, ToolRegistry, ToolSchema


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "parallel project")
    return store, root


def _multi_adapter(responses):
    it = iter(responses)

    class MultiAdapter(LocalEchoAdapter):
        def complete(self, request):
            reply = next(it)
            if isinstance(reply, dict) and "tool_calls" in reply:
                return ModelResponse(
                    text="", tool_calls=tuple(reply["tool_calls"]),
                    model_id="multi", finish_reason="tool_calls",
                )
            return ModelResponse(text=str(reply), model_id="multi", finish_reason="stop")

    return MultiAdapter()


def test_parallel_calls_execute_all_through_gate(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        executed = []
        for name in ("search", "read", "write"):
            registry.register(Tool(
                ToolSchema(name=name, description="d", input_schema={}),
                execute=(lambda n: (lambda a: executed.append(n) or f"r_{n}"))(name),
                permission="read", approval="never",
            ))
        adapter = _multi_adapter([
            {"tool_calls": [
                {"name": "search", "arguments": {}, "id": "c1"},
                {"name": "read", "arguments": {}, "id": "c2"},
                {"name": "write", "arguments": {}, "id": "c3"},
            ]},
            "all done",
        ])
        driver = AdapterDriver(adapter, tool_registry=registry)
        loop = AgentLoop(store=store, session_id="s", driver=driver, tool_registry=registry)
        summary = loop.run("t")
        assert summary["final_state"] == "COMPLETED"
        assert executed == ["search", "read", "write"]
        assert summary["rounds"] == 2
    finally:
        store.close()


def test_gated_parallel_call_stops_whole_turn_conservatively(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        executed = []
        registry.register(Tool(
            ToolSchema(name="safe", description="d", input_schema={}),
            execute=lambda a: executed.append("safe") or "ok", permission="read", approval="never",
        ))
        registry.register(Tool(
            ToolSchema(name="danger", description="d", input_schema={}),
            execute=lambda a: executed.append("danger") or "ok", permission="destructive", approval="always",
        ))
        adapter = _multi_adapter([
            {"tool_calls": [
                {"name": "safe", "arguments": {}, "id": "c1"},
                {"name": "danger", "arguments": {}, "id": "c2"},
            ]},
        ])
        driver = AdapterDriver(adapter, tool_registry=registry)
        loop = AgentLoop(store=store, session_id="s", driver=driver, tool_registry=registry)
        summary = loop.run("t")
        # The gated call needs approval: the whole turn stops (conservative),
        # no result is fabricated, and the loop waits for approval.
        assert summary["final_state"] == "CANCELLED"
        assert summary["stop_reason"] == "waiting_approval"
        types = [e.event_type for e in store.list_events("s", limit=50)]
        assert "tool.approval_required" in types
    finally:
        store.close()


def test_parallel_results_correlated_by_id(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(
            ToolSchema(name="a", description="d", input_schema={}),
            execute=lambda args: "result_a", permission="read", approval="never",
        ))
        registry.register(Tool(
            ToolSchema(name="b", description="d", input_schema={}),
            execute=lambda args: "result_b", permission="read", approval="never",
        ))
        captured_requests = []
        adapter = _multi_adapter([
            {"tool_calls": [
                {"name": "a", "arguments": {}, "id": "call_a"},
                {"name": "b", "arguments": {}, "id": "call_b"},
            ]},
            "done",
        ])
        # Capture the request the driver builds for turn 2.
        original_build = AdapterDriver._build_request
        def spy(self, context, last_tool_result=None):
            req = original_build(self, context, last_tool_result)
            captured_requests.append((req, last_tool_result))
            return req
        AdapterDriver._build_request = spy
        driver = AdapterDriver(adapter, tool_registry=registry)
        loop = AgentLoop(store=store, session_id="s", driver=driver, tool_registry=registry)
        summary = loop.run("t")
        assert summary["final_state"] == "COMPLETED"
        # Turn 2's request carries two tool results, each correlated to its call id.
        turn2_req = captured_requests[-1][0]
        tool_msgs = [m for m in turn2_req.messages if m.role == "tool"]
        assert len(tool_msgs) == 2
        names = {m.name for m in tool_msgs}
        assert names == {"call_a", "call_b"}
    finally:
        AdapterDriver._build_request = original_build
        store.close()


def test_tool_call_and_tool_calls_are_mutually_exclusive(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        class BadDriver:
            def act(self, context, last_tool_result=None):
                return LoopResult(
                    tool_call={"name": "a"},
                    tool_calls=[{"name": "b"}],
                )
        loop = AgentLoop(store=store, session_id="s", driver=BadDriver())
        summary = loop.run("t")
        assert summary["final_state"] == "FAILED"
        assert "mutually exclusive" in summary["error"]
    finally:
        store.close()


def test_empty_tool_calls_list_rejected(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        class BadDriver:
            def act(self, context, last_tool_result=None):
                return LoopResult(tool_calls=[])
        loop = AgentLoop(store=store, session_id="s", driver=BadDriver())
        summary = loop.run("t")
        assert summary["final_state"] == "FAILED"
        assert "empty list" in summary["error"]
    finally:
        store.close()
