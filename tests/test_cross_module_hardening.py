"""Regression tests for the final holistic cross-module audit."""

from __future__ import annotations

import pytest

from noname_harness.agent_loop import AgentLoop, LoopResult
from noname_harness.store import HarnessStore, WorkspaceBoundaryError
from noname_harness.taste import TasteService
from noname_harness.taste_cards import TasteCardService
from noname_harness.tools import Tool, ToolApprovalRequired, ToolRegistry, ToolSchema


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "cross project")
    return store, root


# --- query() is a read-only seam, not a write backdoor -----------------------

def test_query_cannot_insert_bypassing_review_gates(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        for write_sql in [
            "INSERT INTO taste_records (id, track, scope, content_json, status, source_event_ids_json, origin, actor_id, recorded_at) VALUES ('x','authored','user','{}','active','[]','authored','u','2026-01-01')",
            "UPDATE taste_records SET status='active'",
            "DELETE FROM taste_records",
            "DROP TABLE taste_records",
        ]:
            with pytest.raises(ValueError):
                store.query(write_sql)
        # Read-only PRAGMA query forms still work.
        assert store.query("PRAGMA table_info(taste_records)") is not None
        # A write PRAGMA assignment is refused.
        with pytest.raises(ValueError):
            store.query("PRAGMA foreign_keys = OFF")
    finally:
        store.close()


# --- tombstones & generations survive restart --------------------------------

def test_gate_monotonicity_survives_restart(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    db = root / ".noname" / "harness.db"
    schema = ToolSchema("w", "d", {"text": "string"})
    with HarnessStore(db) as store:
        store.initialize_project(root, "x")
        registry = ToolRegistry(store)
        registry.register(Tool(schema, execute=lambda a: "ok", permission="destructive", approval="always"))
    # After a restart, the tombstone must still forbid a weaker re-registration.
    with HarnessStore(db) as store2:
        registry2 = ToolRegistry(store2)
        from noname_harness.tools import ToolShadowingError
        with pytest.raises(ToolShadowingError):
            registry2.register(Tool(schema, execute=lambda a: "ok", permission="read", approval="never"))


def test_failed_execution_does_not_burn_approval_across_restart(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    db = root / ".noname" / "harness.db"
    schema = ToolSchema("w", "d", {"text": "string"})
    calls = {"n": 0}
    def flaky(args):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return "ok"
    with HarnessStore(db) as store:
        store.initialize_project(root, "x")
        registry = ToolRegistry(store)
        registry.register(Tool(schema, execute=flaky, permission="write", approval="always"))
        token = registry.grant_approval("w", {"text": "x"}, approver_id="u", session_id="s")
        from noname_harness.tools import ToolError
        with pytest.raises(ToolError):
            registry.request("w", {"text": "x"}, session_id="s", approval_token=token)
    # Restart before retrying: the failed attempt must NOT have consumed the grant.
    with HarnessStore(db) as store2:
        registry2 = ToolRegistry(store2)
        registry2.register(Tool(schema, execute=flaky, permission="write", approval="always"))
        assert registry2.request("w", {"text": "x"}, session_id="s", approval_token=token)["output"] == "ok"


# --- agent loop routes tool calls through the gate ----------------------------

def test_loop_tool_call_goes_through_approval_gate(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        executed = []
        registry.register(Tool(
            ToolSchema(name="search", description="d", input_schema={"q": "string"}),
            execute=lambda a: executed.append(a) or ["hit"], permission="read", approval="never",
        ))
        driver_results = iter([
            LoopResult(tool_call={"name": "search", "arguments": {"q": "x"}}),
            LoopResult(task_complete=True),
        ])
        class D:
            def act(self, ctx, last=None):
                return next(driver_results)
        loop = AgentLoop(store=store, session_id="s", driver=D(), tool_registry=registry)
        summary = loop.run("t")
        assert summary["final_state"] == "COMPLETED"
        assert executed == [{"q": "x"}]
        assert "tool.requested" in [e.event_type for e in store.list_events("s", limit=50)]
    finally:
        store.close()


# --- card candidates surface in the review inbox ------------------------------

def test_card_candidates_appear_in_review_inbox(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        taste = TasteService(store)
        record = taste.record_authored({"judgement": "克制"})
        cards = TasteCardService(store)
        cards.create_card(title="克制", attitude="工具要克制", track="authored", scope="user", taste_ids=[record["id"]])
        inbox = store.review_inbox()
        assert inbox["counts"]["card"] == 1
        assert inbox["card_pending"][0]["title"] == "克制"
    finally:
        store.close()


# --- WAL sidecar protection ----------------------------------------------------

def test_package_output_cannot_target_db_sidecars(tmp_path):
    store, root = make_store(tmp_path)
    try:
        package = store.assemble_context_package("t", session_id="s")
        db = root / ".noname" / "harness.db"
        for sidecar in (db, str(db) + "-wal", str(db) + "-shm"):
            with pytest.raises(WorkspaceBoundaryError):
                store.write_context_package(sidecar, package)
    finally:
        store.close()


# --- loop/tool/plugin events do not crowd the work window ----------------------

def test_bookkeeping_events_stay_out_of_work_window(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(
            ToolSchema(name="r", description="d", input_schema={"q": "string"}),
            execute=lambda a: "x", permission="read", approval="never",
        ))
        registry.request("r", {"q": "x"}, session_id="s")  # tool.requested/completed
        store.append_event("s", "test.failed", {"path": "t.py", "message": "boom"})
        work_types = [e.event_type for e in store.list_work_events(session_id="s", limit=50)]
        assert "test.failed" in work_types
        assert not any(t.startswith(("tool.", "loop.", "plugin.", "taste.")) for t in work_types)
    finally:
        store.close()
