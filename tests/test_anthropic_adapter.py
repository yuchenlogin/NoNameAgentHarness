"""Anthropic adapter: protocol generality + Messages API mapping."""

from __future__ import annotations

import json

import pytest

from noname_harness.adapters import ModelAdapterError, ModelMessage, ModelRequest
from noname_harness.anthropic_adapter import AnthropicAdapter, load_anthropic_adapter
from noname_harness.plugins import PluginRuntime
from noname_harness.store import HarnessStore
from noname_harness.tools import ToolRegistry


def _req(text="hello", tools=(), system=None):
    messages = []
    if system:
        messages.append(ModelMessage(role="system", content=system))
    messages.append(ModelMessage(role="user", content=text))
    return ModelRequest(messages=tuple(messages), tools=tools)


def replay_transport(status=200, payload=None):
    calls = []

    def transport(url, headers, body, timeout):
        calls.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
        return status, json.dumps(payload or {}).encode("utf-8")

    transport.calls = calls
    return transport


def ok_payload(text="hi", tool_use=None):
    content = [{"type": "text", "text": text}] if text else []
    if tool_use:
        content.append(tool_use)
    return {
        "id": "msg_123",
        "model": "claude-sonnet-4-5",
        "content": content,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 7, "output_tokens": 4},
    }


def test_request_building_anthropic_shape(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    transport = replay_transport(payload=ok_payload())
    adapter = AnthropicAdapter(transport=transport)
    adapter.complete(_req("say hi", system="be terse"))
    call = transport.calls[0]
    assert call["url"].endswith("/messages")
    # Anthropic auth: x-api-key + version, NOT Authorization: Bearer.
    assert call["headers"]["x-api-key"] == "sk-ant-secret"
    assert call["headers"]["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in call["headers"]
    body = json.loads(call["body"].decode("utf-8"))
    # system is a top-level field, not a message.
    assert body["system"] == "be terse"
    assert all(m["role"] != "system" for m in body["messages"])
    assert body["messages"][0] == {"role": "user", "content": "say hi"}
    assert "max_tokens" in body


def test_tools_mapped_to_anthropic_schema(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    transport = replay_transport(payload=ok_payload())
    adapter = AnthropicAdapter(transport=transport)
    tools = ({"name": "search", "description": "d", "input_schema": {"q": "string"}},)
    adapter.complete(_req("look", tools=tools))
    body = json.loads(transport.calls[0]["body"].decode("utf-8"))
    tool = body["tools"][0]
    assert tool["name"] == "search"
    assert tool["input_schema"]["properties"]["q"]["type"] == "string"
    assert tool["input_schema"]["required"] == ["q"]


def test_response_text_and_usage_mapping(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    transport = replay_transport(payload=ok_payload(text="the answer"))
    adapter = AnthropicAdapter(transport=transport)
    response = adapter.complete(_req())
    assert response.text == "the answer"
    assert response.input_tokens == 7
    assert response.output_tokens == 4
    # usage mapped to the unified allowlist (prompt/completion), never the key.
    assert response.vendor_ref["usage"] == {"prompt_tokens": 7, "completion_tokens": 4}
    assert "sk-ant" not in str(response.vendor_ref)


def test_tool_use_block_mapping(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    tool_use = {"type": "tool_use", "id": "toolu_1", "name": "search", "input": {"q": "noname"}}
    transport = replay_transport(payload=ok_payload(text="", tool_use=tool_use))
    adapter = AnthropicAdapter(transport=transport)
    response = adapter.complete(_req())
    assert response.tool_calls[0]["name"] == "search"
    assert response.tool_calls[0]["arguments"] == {"q": "noname"}
    assert response.tool_calls[0]["id"] == "toolu_1"


@pytest.mark.parametrize("status,expected", [
    (401, "auth"), (403, "auth"), (429, "rate_limit"),
    (500, "overloaded"), (529, "unknown"), (400, "invalid_request"),
])
def test_http_error_classification(monkeypatch, status, expected):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    transport = replay_transport(status=status, payload={"type": "error", "error": {"type": "auth_error"}})
    adapter = AnthropicAdapter(transport=transport)
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter.complete(_req())
    assert exc_info.value.error_class == expected
    # vendor_ref is a reference, never the body.
    assert "body" not in exc_info.value.vendor_ref


def test_error_body_never_leaks_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRET")
    def transport(url, headers, body, timeout):
        return 401, json.dumps({"error": {"message": "bad key sk-ant-SECRET", "type": "authentication_error"}}).encode()
    adapter = AnthropicAdapter(transport=transport)
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter.complete(_req())
    assert "SECRET" not in str(exc_info.value.vendor_ref)


def test_plaintext_http_refused(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    adapter = AnthropicAdapter(base_url="http://attacker.example/v1", transport=replay_transport())
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter.complete(_req())
    assert exc_info.value.error_class == "auth"


def test_loads_as_a_plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "p")
    try:
        runtime = PluginRuntime(store, ToolRegistry(store))
        adapter = load_anthropic_adapter(runtime, model_id="claude-opus-4")
        assert adapter.id() == "claude-opus-4"
        event = next(e for e in store.list_events("system", limit=50) if e.event_type == "plugin.loaded")
        assert set(event.payload["side_effects"]) == {"network-egress", "billing"}
    finally:
        store.close()
