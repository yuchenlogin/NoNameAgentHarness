"""OpenAI-compatible adapter: contract verified against a replay transport."""

from __future__ import annotations

import json

import pytest

from noname_harness.adapters import ModelAdapterError, ModelMessage, ModelRequest
from noname_harness.openai_adapter import OpenAIAdapter, load_openai_adapter
from noname_harness.plugins import PluginRuntime
from noname_harness.store import HarnessStore
from noname_harness.tools import ToolRegistry


def _req(text="hello", tools=()):
    return ModelRequest(messages=(ModelMessage(role="user", content=text),), tools=tools)


def replay_transport(status=200, payload=None):
    """A deterministic transport that records the request and replays a response."""

    calls = []

    def transport(url, headers, body, timeout):
        calls.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
        return status, json.dumps(payload or {}).encode("utf-8")

    transport.calls = calls
    return transport


def ok_payload(text="hi there", tool_calls=None):
    message = {"content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-123",
        "model": "gpt-4o-mini",
        "choices": [{"message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    }


def test_request_building_and_auth_header(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")
    transport = replay_transport(payload=ok_payload())
    adapter = OpenAIAdapter(transport=transport)
    adapter.complete(_req("say hi"))
    call = transport.calls[0]
    assert call["url"].endswith("/chat/completions")
    assert call["headers"]["Authorization"] == "Bearer sk-test-secret"
    body = json.loads(call["body"].decode("utf-8"))
    assert body["model"] == "gpt-4o-mini"
    assert body["messages"][0] == {"role": "user", "content": "say hi"}


def test_tools_are_mapped_to_vendor_schema(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    transport = replay_transport(payload=ok_payload())
    adapter = OpenAIAdapter(transport=transport)
    tools = ({"name": "search", "description": "d", "input_schema": {"q": "string"}},)
    adapter.complete(_req("look", tools=tools))
    body = json.loads(transport.calls[0]["body"].decode("utf-8"))
    fn = body["tools"][0]["function"]
    assert fn["name"] == "search"
    assert fn["parameters"]["properties"]["q"]["type"] == "string"
    assert fn["parameters"]["required"] == ["q"]


def test_response_mapping_and_vendor_ref(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    transport = replay_transport(payload=ok_payload(text="the answer"))
    adapter = OpenAIAdapter(transport=transport)
    response = adapter.complete(_req())
    assert response.text == "the answer"
    assert response.input_tokens == 5
    assert response.output_tokens == 3
    # vendor_ref carries a reference to the raw response, never credentials.
    assert response.vendor_ref["id"] == "chatcmpl-123"
    assert "sk-" not in str(response.vendor_ref)


def test_tool_call_response_mapping(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    tool_calls = [{
        "id": "call_1",
        "type": "function",
        "function": {"name": "search", "arguments": '{"q": "noname"}'},
    }]
    transport = replay_transport(payload=ok_payload(text="", tool_calls=tool_calls))
    adapter = OpenAIAdapter(transport=transport)
    response = adapter.complete(_req())
    assert response.tool_calls[0]["name"] == "search"
    assert response.tool_calls[0]["arguments"] == {"q": "noname"}
    assert response.tool_calls[0]["id"] == "call_1"


@pytest.mark.parametrize("status,expected", [
    (401, "auth"),
    (403, "auth"),
    (429, "rate_limit"),
    (500, "overloaded"),
    (503, "overloaded"),
    (400, "invalid_request"),
    (418, "unknown"),
])
def test_http_error_classification(monkeypatch, status, expected):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    transport = replay_transport(status=status, payload={"error": {"message": "x"}})
    adapter = OpenAIAdapter(transport=transport)
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter.complete(_req())
    assert exc_info.value.error_class == expected
    assert exc_info.value.vendor_ref["status"] == status


def test_missing_api_key_raises_auth_error(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    adapter = OpenAIAdapter(transport=replay_transport())
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter.complete(_req())
    assert exc_info.value.error_class == "auth"
    assert exc_info.value.retryable is False


def test_capability_and_cost_estimate():
    adapter = OpenAIAdapter()
    cap = adapter.capability()
    assert cap.tool_calling is True
    assert cap.context_window == 128_000
    cost = adapter.estimate_cost(_req("one two three"))
    assert cost["estimated_input_words"] == 3


def test_stream_matches_complete(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    transport = replay_transport(payload=ok_payload(text="full text"))
    adapter = OpenAIAdapter(transport=transport)
    events = list(adapter.stream(_req()))
    text = "".join(e.text for e in events if e.kind == "text_delta")
    assert text == "full text"
    assert events[-1].kind == "completed"


def test_loads_as_a_plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "plugin project")
    try:
        runtime = PluginRuntime(store, ToolRegistry(store))
        adapter = load_openai_adapter(runtime, model_id="gpt-4o")
        assert adapter.id() == "gpt-4o"
        # The load is audited in the ledger.
        loaded = runtime.loaded_plugins()
        assert any(p["plugin_id"] == "model-openai-gpt-4o" for p in loaded)
        assert any(
            e.event_type == "plugin.loaded" for e in store.list_events("system", limit=50)
        )
    finally:
        store.close()


# --- 对抗性审查（REJECT）发现的回归 ---

def test_error_vendor_ref_never_contains_body_or_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-SECRET-abc")
    def transport(url, headers, body, timeout):
        return 401, json.dumps({"error": {"message": "invalid key: Bearer sk-SECRET-abc", "code": "bad_key"}}).encode()
    adapter = OpenAIAdapter(transport=transport)
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter.complete(_req())
    # vendor_ref carries only status + error code, never the body or the key.
    assert "sk-SECRET" not in str(exc_info.value.vendor_ref)
    assert "body" not in exc_info.value.vendor_ref
    assert exc_info.value.vendor_ref["status"] == 401


def test_plaintext_http_base_url_refused_by_default(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    adapter = OpenAIAdapter(base_url="http://attacker.example/v1", transport=replay_transport())
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter.complete(_req())
    assert exc_info.value.error_class == "auth"
    assert exc_info.value.error_class == "auth"


def test_allow_insecure_opt_in_permits_local_http(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    adapter = OpenAIAdapter(
        base_url="http://localhost:11434/v1",
        allow_insecure=True,
        transport=replay_transport(payload=ok_payload(text="local")),
    )
    assert adapter.complete(_req()).text == "local"


def test_dict_arguments_passed_through(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    tool_calls = [{"id": "c", "type": "function", "function": {"name": "t", "arguments": {"q": "x"}}}]
    adapter = OpenAIAdapter(transport=replay_transport(payload=ok_payload(text="", tool_calls=tool_calls)))
    assert adapter.complete(_req()).tool_calls[0]["arguments"] == {"q": "x"}


def test_invalid_json_arguments_normalized(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    tool_calls = [{"id": "c", "type": "function", "function": {"name": "t", "arguments": "{invalid json"}}]
    adapter = OpenAIAdapter(transport=replay_transport(payload=ok_payload(text="", tool_calls=tool_calls)))
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter.complete(_req())
    assert "malformed tool-call arguments" in str(exc_info.value)


def test_plugin_manifest_declares_side_effects(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "p")
    try:
        runtime = PluginRuntime(store, ToolRegistry(store))
        load_openai_adapter(runtime, model_id="gpt-4o")
        event = next(e for e in store.list_events("system", limit=50) if e.event_type == "plugin.loaded")
        assert set(event.payload["side_effects"]) == {"network-egress", "billing"}
    finally:
        store.close()


def test_default_transport_refuses_redirect(monkeypatch):
    # The default _http_transport must not follow redirects (key forwarding).
    from noname_harness.openai_adapter import _NoRedirectHandler
    handler = _NoRedirectHandler()
    assert handler.redirect_request(None, None, None, None, None, None) is None


# --- 复审 (pass 2) 发现的回归 ---

def test_no_choices_vendor_ref_has_no_body(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-SECRET")
    adapter = OpenAIAdapter(
        transport=lambda *a: (200, json.dumps({"note": "Bearer sk-SECRET"}).encode())
    )
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter.complete(_req())
    assert "SECRET" not in str(exc_info.value.vendor_ref)
    assert exc_info.value.vendor_ref == {"id": None}


def test_success_usage_is_allowlisted(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-SECRET")
    payload = ok_payload(text="ok")
    payload["usage"] = {"prompt_tokens": 1, "evil": "sk-SECRET"}
    adapter = OpenAIAdapter(transport=replay_transport(payload=payload))
    response = adapter.complete(_req())
    assert response.vendor_ref["usage"] == {"prompt_tokens": 1}
    assert "SECRET" not in str(response.vendor_ref)


def test_non_utf8_body_vendor_ref_is_json_serialisable(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    from noname_harness.store import _json
    adapter = OpenAIAdapter(transport=lambda *a: (200, b"\xff\xfe garbage"))
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter.complete(_req())
    # vendor_ref holds no bytes, so it can be persisted to the ledger.
    _json({"vendor_ref": exc_info.value.vendor_ref})
    assert exc_info.value.vendor_ref["bytes"] == 10


def test_uppercase_http_scheme_refused(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    adapter = OpenAIAdapter(base_url="HTTP://attacker.example/v1", transport=replay_transport())
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter.complete(_req())
    assert exc_info.value.error_class == "auth"


def test_urlerror_wrapping_timeout_is_retryable(monkeypatch):
    import socket
    import urllib.error
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    def transport(url, headers, body, timeout):
        raise urllib.error.URLError(socket.timeout("timed out"))
    adapter = OpenAIAdapter(transport=transport)
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter.complete(_req())
    assert exc_info.value.error_class == "timeout"
    assert exc_info.value.retryable is True


def test_oversized_dict_arguments_rejected(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    big = {"data": "x" * 2_000_000}
    tool_calls = [{"id": "c", "type": "function", "function": {"name": "t", "arguments": big}}]
    adapter = OpenAIAdapter(transport=replay_transport(payload=ok_payload(text="", tool_calls=tool_calls)))
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter.complete(_req())
    assert "1MB" in str(exc_info.value)
