"""Plugin runtime: crystallised capability that cannot bypass the kernel."""

from __future__ import annotations

import pytest

from noname_harness.plugins import (
    Plugin,
    PluginContribution,
    PluginError,
    PluginManifest,
    PluginRuntime,
)
from noname_harness.store import HarnessStore
from noname_harness.tools import (
    Tool,
    ToolApprovalRequired,
    ToolRegistry,
    ToolSchema,
    ToolShadowingError,
)


def make_runtime(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "plugin project")
    return store, ToolRegistry(store)


def _read_tool(name="search", scope="session", session_id="s"):
    return Tool(
        ToolSchema(name=name, description="d", input_schema={"q": "string"}),
        execute=lambda a: [a["q"]],
        permission="read", approval="never", scope=scope, session_id=session_id if scope == "session" else None,
    )


def _plugin(tools, **manifest_kwargs):
    kwargs = dict(id="demo", version="1.0.0", capabilities=("search",))
    kwargs.update(manifest_kwargs)
    return Plugin(
        manifest=PluginManifest(**kwargs),
        build=lambda: [PluginContribution(tool=t) for t in tools],
    )


def test_manifest_validation(tmp_path):
    with pytest.raises(PluginError):
        PluginManifest(id="  ", version="1", capabilities=("x",))
    with pytest.raises(PluginError):
        PluginManifest(id="p", version="1", capabilities=())
    with pytest.raises(PluginError):
        PluginManifest(id="p", version="1", capabilities=("x",), max_permission="fly")
    with pytest.raises(PluginError):
        PluginManifest(id="p", version="1", capabilities=("x",), min_interface=5, max_interface=2)


def test_load_registers_tools_and_logs(tmp_path):
    store, registry = make_runtime(tmp_path)
    try:
        runtime = PluginRuntime(store, registry)
        result = runtime.load(_plugin([_read_tool()]))
        assert result["loaded"] == "demo"
        assert result["tools"] == ["search"]
        # The tool is usable through the normal registry.
        assert registry.request("search", {"q": "x"}, session_id="s")["output"] == ["x"]
        types = [e.event_type for e in store.list_events("system", limit=50)]
        assert "plugin.loaded" in types
    finally:
        store.close()


def test_plugin_cannot_bypass_approval_gate(tmp_path):
    store, registry = make_runtime(tmp_path)
    try:
        runtime = PluginRuntime(store, registry)
        gated = Tool(
            ToolSchema(name="delete", description="d", input_schema={"path": "string"}),
            execute=lambda a: "gone", permission="destructive", approval="always", scope="session", session_id="s",
        )
        runtime.load(_plugin([gated], max_permission="destructive"))
        # Even a plugin-contributed destructive tool needs a real approval token.
        with pytest.raises(ToolApprovalRequired):
            registry.request("delete", {"path": "/etc/passwd"}, session_id="s")
    finally:
        store.close()


def test_plugin_tool_cannot_exceed_manifest_permission(tmp_path):
    store, registry = make_runtime(tmp_path)
    try:
        runtime = PluginRuntime(store, registry)
        # Manifest declares max read, but contributes a write tool -> refused.
        write_tool = Tool(
            ToolSchema(name="w", description="d", input_schema={"q": "string"}),
            execute=lambda a: "x", permission="write", approval="always", scope="session", session_id="s",
        )
        with pytest.raises(PluginError):
            runtime.load(_plugin([write_tool], max_permission="read"))
        assert runtime.loaded_plugins() == []
    finally:
        store.close()


def test_plugin_cannot_use_global_scope_without_requesting_it(tmp_path):
    store, registry = make_runtime(tmp_path)
    try:
        runtime = PluginRuntime(store, registry)
        global_tool = _read_tool(scope="global")
        with pytest.raises(PluginError):
            runtime.load(_plugin([global_tool]))
        # With an explicit request, global scope is allowed and audited.
        runtime.load(_plugin([_read_tool(name="gs", scope="global")], requests_global_scope=True))
        assert registry.request("gs", {"q": "x"}, session_id="s")["output"] == ["x"]
    finally:
        store.close()


def test_unload_reclaims_contributions_reversibly(tmp_path):
    store, registry = make_runtime(tmp_path)
    try:
        runtime = PluginRuntime(store, registry)
        runtime.load(_plugin([_read_tool()]))
        assert registry.get("search") is not None
        result = runtime.unload("demo")
        assert result["reclaimed_tools"] == ["search"]
        assert registry.get("search") is None
        with pytest.raises(PluginError):
            runtime.unload("demo")
        types = [e.event_type for e in store.list_events("system", limit=50)]
        assert "plugin.unloaded" in types
    finally:
        store.close()


def test_failed_load_rolls_back_partial_registration(tmp_path):
    store, registry = make_runtime(tmp_path)
    try:
        runtime = PluginRuntime(store, registry)
        # Pre-register a strong tool the plugin will illegally shadow.
        registry.register(Tool(
            ToolSchema(name="search", description="d", input_schema={"q": "string"}),
            execute=lambda a: "orig", permission="write", approval="always", scope="global",
        ))
        # Plugin contributes a same-named weaker tool -> shadowing rejected,
        # and the plugin's other tool must be rolled back too.
        weaker = _read_tool(scope="global")  # weaker permission + would need global
        plugin = Plugin(
            manifest=PluginManifest(id="demo", version="1", capabilities=("x",), requests_global_scope=True),
            build=lambda: [
                PluginContribution(tool=_read_tool(name="other", scope="global")),
                PluginContribution(tool=weaker),
            ],
        )
        with pytest.raises((PluginError, ToolShadowingError)):
            runtime.load(plugin)
        # Nothing half-loaded: 'other' was rolled back, plugin not loaded.
        assert registry.get("other") is None
        assert runtime.loaded_plugins() == []
        # The original strong tool is untouched.
        assert registry.get("search").permission == "write"
    finally:
        store.close()


def test_incompatible_interface_version_refused(tmp_path):
    store, registry = make_runtime(tmp_path)
    try:
        runtime = PluginRuntime(store, registry)
        with pytest.raises(PluginError):
            runtime.load(_plugin([_read_tool()], min_interface=99, max_interface=100))
    finally:
        store.close()


def test_double_load_refused(tmp_path):
    store, registry = make_runtime(tmp_path)
    try:
        runtime = PluginRuntime(store, registry)
        runtime.load(_plugin([_read_tool()]))
        with pytest.raises(PluginError):
            runtime.load(_plugin([_read_tool()]))
    finally:
        store.close()
