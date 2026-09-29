"""Model adapter contract and AgentLoop integration."""

from __future__ import annotations

import pytest

from noname_harness.adapters import (
    AdapterDriver,
    LocalEchoAdapter,
    ModelAdapterError,
    ModelMessage,
    ModelRequest,
)
from noname_harness.agent_loop import AgentLoop
from noname_harness.store import HarnessStore
from noname_harness.tools import Tool, ToolRegistry, ToolSchema


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "adapter project")
    return store, root


def _req(text="hello"):
    return ModelRequest(messages=(ModelMessage(role="user", content=text),))


def test_request_and_message_validation():
    with pytest.raises(ValueError):
        ModelMessage(role="owner", content="x")
    with pytest.raises(ValueError):
        ModelRequest(messages=())
    with pytest.raises(ValueError):
        ModelAdapterError("explode", "x")


def test_error_classification_and_retryability():
    rate = ModelAdapterError("rate_limit", "slow down")
    assert rate.retryable is True
    auth = ModelAdapterError("auth", "bad key", vendor_ref={"status": 401})
    assert auth.retryable is False
    assert auth.vendor_ref == {"status": 401}


def test_echo_adapter_text_response_preserves_vendor_ref():
    adapter = LocalEchoAdapter()
    response = adapter.complete(_req("say hi"))
    assert "echo: say hi" in response.text
    assert response.model_id == "local-echo"
    assert response.vendor_ref is not None
    assert response.finish_reason == "stop"


def test_echo_adapter_tool_call_structuring():
    adapter = LocalEchoAdapter(
        responder=lambda req: {"tool_call": {"name": "search", "arguments": {"q": "x"}}}
    )
    response = adapter.complete(_req("look up"))
    assert response.tool_calls[0]["name"] == "search"
    assert response.finish_reason == "tool_calls"


def test_echo_adapter_capability_and_cost():
    adapter = LocalEchoAdapter()
    cap = adapter.capability()
    assert cap.tool_calling is True
    assert cap.context_window == 32_000
    cost = adapter.estimate_cost(_req("one two three"))
    assert cost["input_tokens"] == 3
    assert cost["currency"] == "none"


def test_streaming_emits_deltas_and_completion():
    adapter = LocalEchoAdapter()
    events = list(adapter.stream(_req("hello world")))
    kinds = [e.kind for e in events]
    assert "text_delta" in kinds
    assert kinds[-1] == "completed"


def test_adapter_driver_produces_text_result(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        adapter = LocalEchoAdapter(responder=lambda req: "the answer")
        driver = AdapterDriver(adapter)
        loop = AgentLoop(store=store, session_id="s", driver=driver)
        summary = loop.run("question", task_type="question")
        assert summary["final_state"] == "COMPLETED"
        assert summary["output"] == "the answer"
    finally:
        store.close()


def test_adapter_driver_routes_tool_call_through_gate(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        registry.register(Tool(
            ToolSchema(name="search", description="d", input_schema={"q": "string"}),
            execute=lambda a: [f"hit:{a['q']}"], permission="read", approval="never",
        ))
        # First turn: model requests a tool call; second turn: it answers.
        responses = iter([
            {"tool_call": {"name": "search", "arguments": {"q": "noname"}}},
            "based on the search, the answer",
        ])
        adapter = LocalEchoAdapter(responder=lambda req: next(responses))
        driver = AdapterDriver(adapter)
        loop = AgentLoop(store=store, session_id="s", driver=driver, tool_registry=registry)
        summary = loop.run("research", task_type="research")
        assert summary["final_state"] == "COMPLETED"
        assert summary["rounds"] == 2
        # The tool call went through the approval gate (ledger has tool events).
        types = [e.event_type for e in store.list_events("s", limit=100)]
        assert "tool.requested" in types
        assert "tool.completed" in types
    finally:
        store.close()


def test_adapter_driver_sends_visible_tools_not_implementations(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        captured = []
        def responder(request):
            captured.append(request)
            return "done"
        adapter = LocalEchoAdapter(responder=responder)
        driver = AdapterDriver(adapter)
        # Inject a context with visible_tools to confirm they're passed through.
        context = {
            "task": "t",
            "layers": {"high": [], "mid": []},
            "visible_tools": [{"name": "search", "description": "d", "input_schema": {}}],
        }
        result = driver.act(context)
        assert result.task_complete is True
        sent = captured[0]
        assert sent.tools[0]["name"] == "search"
        assert "execute" not in sent.tools[0]
    finally:
        store.close()
