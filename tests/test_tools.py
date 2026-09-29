"""Tool registry: surface separation and a ledger-backed physical approval gate."""

from __future__ import annotations

import pytest

from noname_harness.store import HarnessStore
from noname_harness.tools import (
    ApprovalToken,
    Tool,
    ToolApprovalRequired,
    ToolError,
    ToolRegistry,
    ToolSchema,
    ToolShadowingError,
    ToolValidationError,
)


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "tools project")
    return store, root


def _schema(name="echo"):
    return ToolSchema(name=name, description="echo back", input_schema={"text": "string"})


# --- surface separation ------------------------------------------------------

def test_model_visible_surface_hides_implementation(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(_schema(), execute=lambda a: a["text"], permission="read", approval="never"))
        visible = registry.visible_tools(session_id="s")[0]
        assert set(visible) == {"name", "description", "input_schema"}
        for hidden in ("execute", "permission", "approval", "scope"):
            assert hidden not in visible
    finally:
        store.close()


# --- the physical approval gate ----------------------------------------------

def test_gated_tool_cannot_run_without_a_token(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        calls = []
        registry.register(Tool(_schema("w"), execute=lambda a: calls.append(a) or "ok",
                               permission="write", approval="always"))
        with pytest.raises(ToolApprovalRequired):
            registry.request("w", {"text": "x"}, session_id="s")
        assert calls == []
        types = [e.event_type for e in store.list_events("s", limit=10)]
        assert "tool.approval_required" in types
        assert "tool.completed" not in types
    finally:
        store.close()


def test_gated_tool_runs_only_with_a_valid_call_bound_token(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(_schema("w"), execute=lambda a: "written",
                               permission="write", approval="always"))
        token = registry.grant_approval("w", {"text": "x"}, approver_id="user", session_id="s")
        result = registry.request("w", {"text": "x"}, session_id="s", approval_token=token)
        assert result["output"] == "written"
        types = [e.event_type for e in store.list_events("s", limit=20)]
        assert "tool.approval_granted" in types
        assert "tool.approved" in types
        assert "tool.completed" in types
    finally:
        store.close()


def test_approval_token_is_single_use(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(_schema("w"), execute=lambda a: "ok", permission="write", approval="always"))
        token = registry.grant_approval("w", {"text": "x"}, approver_id="user", session_id="s")
        registry.request("w", {"text": "x"}, session_id="s", approval_token=token)
        # Reusing the same token is rejected: it was consumed.
        with pytest.raises(ToolApprovalRequired):
            registry.request("w", {"text": "x"}, session_id="s", approval_token=token)
        types = [e.event_type for e in store.list_events("s", limit=30)]
        assert "tool.approval_rejected" in types
    finally:
        store.close()


def test_approval_token_is_bound_to_exact_arguments(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(_schema("w"), execute=lambda a: "ok", permission="write", approval="always"))
        # Approval for text="x" must not authorise text="rm -rf".
        token = registry.grant_approval("w", {"text": "x"}, approver_id="user", session_id="s")
        with pytest.raises(ToolApprovalRequired):
            registry.request("w", {"text": "rm -rf"}, session_id="s", approval_token=token)
    finally:
        store.close()


def test_subclass_cannot_override_the_gate(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        class EvilTool(Tool):
            @property
            def requires_approval(self):
                return False
        registry = ToolRegistry(store)
        # Subclasses are refused at registration: the gate cannot be removed.
        with pytest.raises(ToolError):
            registry.register(EvilTool(_schema("evil"), execute=lambda a: "x",
                                       permission="destructive", approval="always"))
    finally:
        store.close()


def test_self_asserted_boolean_does_not_exist(tmp_path):
    """There is no `approved=True` escape hatch anymore."""
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(_schema("w"), execute=lambda a: "ok", permission="write", approval="always"))
        import inspect
        sig = inspect.signature(registry.request)
        assert "approved" not in sig.parameters
    finally:
        store.close()


# --- shadowing monotonicity ---------------------------------------------------

def test_shadowing_cannot_downgrade_permission(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(_schema(), execute=lambda a: "g", permission="write", approval="always", scope="global"))
        with pytest.raises(ToolShadowingError):
            registry.register(Tool(_schema(), execute=lambda a: "s", permission="read", approval="never", scope="session", session_id="s"))
    finally:
        store.close()


def test_shadowing_cannot_drop_approval(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(_schema(), execute=lambda a: "g", permission="write", approval="always", scope="global"))
        with pytest.raises(ToolShadowingError):
            registry.register(Tool(_schema(), execute=lambda a: "s", permission="write", approval="never", scope="session", session_id="s"))
    finally:
        store.close()


def test_wider_scope_cannot_shadow_narrower(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(_schema(), execute=lambda a: "s", permission="read", approval="never", scope="session", session_id="s"))
        with pytest.raises(ToolShadowingError):
            registry.register(Tool(_schema(), execute=lambda a: "g", permission="read", approval="never", scope="global"))
    finally:
        store.close()


def test_legal_shadowing_is_recorded(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(_schema(), execute=lambda a: "global", permission="read", approval="never", scope="global"))
        result = registry.register(Tool(_schema(), execute=lambda a: "session", permission="read", approval="never", scope="session", session_id="s"))
        assert result["shadowed"] == {"scope": "global", "permission": "read"}
        types = [e.event_type for e in store.list_events("system", limit=20)]
        assert "tool.shadowed" in types
        assert "tool.registered" in types
    finally:
        store.close()


# --- real scoping -------------------------------------------------------------

def test_session_tool_is_invisible_and_unrunnable_from_other_sessions(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(_schema(), execute=lambda a: "secret", permission="read", approval="never", scope="session", session_id="alice"))
        # Visible to alice, not bob.
        assert len(registry.visible_tools(session_id="alice")) == 1
        assert len(registry.visible_tools(session_id="bob")) == 0
        # Runnable by alice, not bob.
        assert registry.request("echo", {"text": "x"}, session_id="alice")["output"] == "secret"
        with pytest.raises(ToolError):
            registry.request("echo", {"text": "x"}, session_id="bob")
    finally:
        store.close()


def test_session_scoped_tool_requires_session_id(tmp_path):
    with pytest.raises(ValueError):
        Tool(_schema(), execute=lambda a: 1, permission="read", approval="never", scope="session")


# --- validation, logging completeness, misc ----------------------------------

def test_validation_failure_is_logged(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(
            ToolSchema(name="calc", description="d", input_schema={"n": "integer"}),
            execute=lambda a: a["n"], permission="read", approval="never",
        ))
        with pytest.raises(ToolValidationError):
            registry.request("calc", {"n": "one"}, session_id="s")
        types = [e.event_type for e in store.list_events("s", limit=10)]
        assert "tool.validation_failed" in types
    finally:
        store.close()


def test_tool_failure_is_logged_with_no_orphan_request(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        def boom(args):
            raise RuntimeError("kaboom")
        registry.register(Tool(_schema(), execute=boom, permission="read", approval="never"))
        with pytest.raises(ToolError):
            registry.request("echo", {"text": "x"}, session_id="s")
        types = [e.event_type for e in store.list_events("s", limit=10)]
        assert "tool.failed" in types
    finally:
        store.close()


def test_request_logs_arguments_hash_not_raw_content(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(_schema("w"), execute=lambda a: "ok", permission="write", approval="always"))
        with pytest.raises(ToolApprovalRequired):
            registry.request("w", {"text": "a-secret-value"}, session_id="s")
        # The raw secret never reaches the append-only ledger pre-approval.
        for event in store.list_events("s", limit=10):
            assert "a-secret-value" not in str(event.payload)
        requested = next(e for e in store.list_events("s", limit=10) if e.event_type == "tool.requested")
        assert "arguments_hash" in requested.payload
    finally:
        store.close()


def test_completed_logs_real_elapsed_time(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(_schema(), execute=lambda a: "ok", permission="read", approval="never"))
        registry.request("echo", {"text": "x"}, session_id="s")
        completed = next(e for e in store.list_events("s", limit=10) if e.event_type == "tool.completed")
        assert "elapsed_ms" in completed.payload
        assert isinstance(completed.payload["elapsed_ms"], int)
    finally:
        store.close()


def test_unregister_and_invalid_declarations(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(_schema(), execute=lambda a: "x", permission="read", approval="never"))
        assert registry.unregister("echo") is True
        assert registry.unregister("echo") is False
        with pytest.raises(ToolError):
            registry.request("ghost", {}, session_id="s")
        with pytest.raises(ValueError):
            Tool(_schema(), execute=lambda a: 1, permission="destructive", approval="never")
        with pytest.raises(ValueError):
            Tool(_schema(), execute=lambda a: 1, permission="read", approval="sometimes")
        with pytest.raises(ValueError):
            ToolSchema(name="  ", description="d", input_schema={})
        with pytest.raises(ToolError):
            registry.grant_approval("ghost", {}, approver_id="u", session_id="s")
        # echo is unregistered by now, so re-register to test the approver check.
        registry.register(Tool(_schema("write2"), execute=lambda a: "x", permission="write", approval="always"))
        with pytest.raises(ValueError):
            registry.grant_approval("write2", {}, approver_id="  ", session_id="s")
    finally:
        store.close()


# --- grant durability across restarts -----------------------------------------

def test_unconsumed_token_survives_restart_and_stays_single_use(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    db = root / ".noname" / "harness.db"
    schema = ToolSchema("w", "d", {"text": "string"})

    with HarnessStore(db) as store:
        store.initialize_project(root, "restart project")
        registry = ToolRegistry(store)
        registry.register(Tool(schema, execute=lambda a: "ok", permission="write", approval="always"))
        token = registry.grant_approval("w", {"text": "x"}, approver_id="user", session_id="s")

    # Reopen: the grant is durable evidence, so the token must still verify.
    with HarnessStore(db) as store2:
        registry2 = ToolRegistry(store2)
        registry2.register(Tool(schema, execute=lambda a: "ok", permission="write", approval="always"))
        assert registry2.request("w", {"text": "x"}, session_id="s", approval_token=token)["output"] == "ok"

    # Reopen again: the consumed token must stay consumed.
    with HarnessStore(db) as store3:
        registry3 = ToolRegistry(store3)
        registry3.register(Tool(schema, execute=lambda a: "ok", permission="write", approval="always"))
        with pytest.raises(ToolApprovalRequired):
            registry3.request("w", {"text": "x"}, session_id="s", approval_token=token)


def test_token_ids_are_unique_across_grants(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(_schema("w"), execute=lambda a: "ok", permission="write", approval="always"))
        ids = {
            registry.grant_approval("w", {"text": "x"}, approver_id="u", session_id="s").id
            for _ in range(5)
        }
        assert len(ids) == 5  # identical arguments still get distinct tokens
    finally:
        store.close()


# ---复审 (round 5) 发现的回归 ---

def test_grant_is_recorded_before_token_goes_live(tmp_path):
    """A failed ledger append must not leave a live, unlogged token."""
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(_schema("w"), execute=lambda a: "ok", permission="write", approval="always"))
        # Empty session_id makes append_event raise; the token must NOT go live.
        with pytest.raises(ValueError):
            registry.grant_approval("w", {"text": "x"}, approver_id="u", session_id="")
        # No grant event was written, and no live token exists.
        assert registry._grants == {}
        assert not any(
            e.event_type == "tool.approval_granted" for e in store.list_events(limit=50)
        )
    finally:
        store.close()


def test_unregister_then_reregister_cannot_downgrade_permission(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(_schema(), execute=lambda a: "g", permission="destructive", approval="always", scope="global"))
        registry.unregister("echo")
        # The tombstone remembers the strongest-ever gate; a weaker re-register
        # is refused even though no live tool shadows it.
        with pytest.raises(ToolShadowingError):
            registry.register(Tool(_schema(), execute=lambda a: "x", permission="read", approval="never", scope="global"))
        with pytest.raises(ToolShadowingError):
            registry.register(Tool(_schema(), execute=lambda a: "x", permission="write", approval="always", scope="global"))
    finally:
        store.close()


def test_unregister_then_reregister_cannot_drop_approval(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(_schema(), execute=lambda a: "g", permission="write", approval="always", scope="global"))
        registry.unregister("echo")
        with pytest.raises(ToolShadowingError):
            registry.register(Tool(_schema(), execute=lambda a: "x", permission="write", approval="never", scope="global"))
    finally:
        store.close()


def test_token_is_bound_to_its_session(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(_schema("w"), execute=lambda a: "ok", permission="write", approval="always"))
        token = registry.grant_approval("w", {"text": "x"}, approver_id="u", session_id="alice")
        # alice's token must not authorise bob's call.
        with pytest.raises(ToolApprovalRequired):
            registry.request("w", {"text": "x"}, session_id="bob", approval_token=token)
        # ...but it works for alice.
        assert registry.request("w", {"text": "x"}, session_id="alice", approval_token=token)["output"] == "ok"
    finally:
        store.close()


def test_failed_execution_returns_the_token(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        calls = {"n": 0}
        def flaky(args):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            return "ok"
        registry.register(Tool(_schema("w"), execute=flaky, permission="write", approval="always"))
        token = registry.grant_approval("w", {"text": "x"}, approver_id="u", session_id="s")
        # First attempt fails; the approval must not be burned.
        with pytest.raises(ToolError):
            registry.request("w", {"text": "x"}, session_id="s", approval_token=token)
        # Retry with the SAME token succeeds -- approval is per-completed-call.
        assert registry.request("w", {"text": "x"}, session_id="s", approval_token=token)["output"] == "ok"
    finally:
        store.close()


def test_unserialisable_arguments_are_logged_and_raise_cleanly(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(_schema(), execute=lambda a: "x", permission="read", approval="never"))
        with pytest.raises(ToolValidationError):
            registry.request("echo", {"text": {"a", "b"}}, session_id="s")
        types = [e.event_type for e in store.list_events("s", limit=10)]
        assert "tool.validation_failed" in types
    finally:
        store.close()
