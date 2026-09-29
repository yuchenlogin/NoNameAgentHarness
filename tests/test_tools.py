"""Tool registry contracts: separation of surfaces, physical approval gate."""

from __future__ import annotations

import pytest

from noname_harness.store import HarnessStore
from noname_harness.tools import (
    Tool,
    ToolApprovalRequired,
    ToolError,
    ToolRegistry,
    ToolSchema,
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


def test_model_visible_surface_hides_implementation(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(_schema(), execute=lambda a: a["text"], permission="read", approval="never"))
        visible = registry.visible_tools()[0]
        # The model sees the contract, never the callable or host policy.
        assert set(visible) == {"name", "description", "input_schema", "output_contract"}
        assert "execute" not in visible
        assert "permission" not in visible
        assert "approval" not in visible
    finally:
        store.close()


def test_read_tool_runs_without_approval(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(_schema(), execute=lambda a: a["text"].upper(), permission="read", approval="never"))
        result = registry.request("echo", {"text": "hi"}, session_id="s")
        assert result["output"] == "HI"
        types = [e.event_type for e in store.list_events("s", limit=10)]
        assert "tool.requested" in types
        assert "tool.completed" in types
        assert "tool.approved" not in types  # no approval needed for read
    finally:
        store.close()


def test_write_tool_requires_approval_and_is_physically_blocked(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        calls = []
        def write_impl(args):
            calls.append(args)
            return "written"
        registry.register(Tool(_schema("write"), execute=write_impl, permission="write", approval="on_write"))

        # Without approval the tool physically cannot run -- the callable never fires.
        with pytest.raises(ToolApprovalRequired):
            registry.request("write", {"text": "x"}, session_id="s")
        assert calls == []
        types = [e.event_type for e in store.list_events("s", limit=10)]
        assert "tool.approval_required" in types
        assert "tool.completed" not in types

        # With approval granted (out-of-band), it runs and the approval is logged.
        result = registry.request("write", {"text": "x"}, session_id="s", approved=True)
        assert result["output"] == "written"
        assert calls == [{"text": "x"}]
        types = [e.event_type for e in store.list_events("s", limit=20)]
        assert "tool.approved" in types
        assert "tool.completed" in types
    finally:
        store.close()


def test_destructive_tool_must_always_be_approved(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        # Declaration-level guard: destructive requires approval='always'.
        with pytest.raises(ValueError):
            Tool(_schema("rm"), execute=lambda a: None, permission="destructive", approval="on_write")
        registry = ToolRegistry(store)
        registry.register(Tool(_schema("rm"), execute=lambda a: "gone", permission="destructive", approval="always"))
        with pytest.raises(ToolApprovalRequired):
            registry.request("rm", {"text": "x"}, session_id="s")
        assert registry.request("rm", {"text": "x"}, session_id="s", approved=True)["output"] == "gone"
    finally:
        store.close()


def test_validation_rejects_bad_input_before_any_execution(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        calls = []
        registry.register(Tool(
            ToolSchema(name="calc", description="d", input_schema={"n": "integer", "m": "number"}),
            execute=lambda a: calls.append(a) or a["n"] + a["m"],
            permission="read", approval="never",
        ))
        # missing required
        with pytest.raises(ToolValidationError):
            registry.request("calc", {"n": 1}, session_id="s")
        # unknown argument
        with pytest.raises(ToolValidationError):
            registry.request("calc", {"n": 1, "m": 2, "evil": 3}, session_id="s")
        # wrong type
        with pytest.raises(ToolValidationError):
            registry.request("calc", {"n": "one", "m": 2}, session_id="s")
        # bool sneaking into integer (bool is a subclass of int)
        with pytest.raises(ToolValidationError):
            registry.request("calc", {"n": True, "m": 2}, session_id="s")
        # nothing ever executed
        assert calls == []
        # valid call works
        assert registry.request("calc", {"n": 1, "m": 2.5}, session_id="s")["output"] == 3.5
    finally:
        store.close()


def test_shadowing_is_recorded_in_ledger(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(_schema(), execute=lambda a: "global", permission="read", approval="never", scope="global"))
        result = registry.register(Tool(_schema(), execute=lambda a: "session", permission="read", approval="never", scope="session"))
        assert result["shadowed"] == {"scope": "global", "permission": "read"}
        # The narrower scope now serves the call.
        assert registry.request("echo", {"text": "x"}, session_id="s")["output"] == "session"
        # Both the registration and the shadowing are in the ledger.
        types = [e.event_type for e in store.list_events("system", limit=20)]
        assert "tool.registered" in types
        assert "tool.shadowed" in types
    finally:
        store.close()


def test_unregister_releases_and_logs(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        closed = []
        def impl(args):
            return "x"
        impl.close = lambda: closed.append(True)
        registry.register(Tool(_schema(), execute=impl, permission="read", approval="never"))
        assert registry.unregister("echo") is True
        assert closed == [True]  # resources released on unload
        assert registry.unregister("echo") is False
        assert registry.get("echo") is None
        types = [e.event_type for e in store.list_events("system", limit=20)]
        assert "tool.unregistered" in types
    finally:
        store.close()


def test_tool_failure_is_normalized_and_logged(tmp_path):
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


def test_unknown_tool_and_invalid_declarations(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        with pytest.raises(ToolError):
            registry.request("ghost", {}, session_id="s")
        with pytest.raises(ValueError):
            Tool(_schema(), execute=lambda a: 1, permission="fly", approval="never")
        with pytest.raises(ValueError):
            Tool(_schema(), execute=lambda a: 1, permission="read", approval="sometimes")
        with pytest.raises(ValueError):
            Tool(_schema(), execute=lambda a: 1, permission="read", approval="never", scope="universe")
        with pytest.raises(ValueError):
            Tool(_schema(), execute=lambda a: 1, permission="read", approval="never", timeout_seconds=0)
        with pytest.raises(ValueError):
            ToolSchema(name="  ", description="d", input_schema={})
    finally:
        store.close()
