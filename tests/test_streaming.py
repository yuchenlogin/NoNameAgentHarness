"""True SSE streaming for both vendor adapters (replay, no network)."""

from __future__ import annotations

import json

import pytest

from noname_harness.adapters import ModelAdapterError, ModelMessage, ModelRequest
from noname_harness.anthropic_adapter import AnthropicAdapter
from noname_harness.openai_adapter import OpenAIAdapter


def _req(text="hello"):
    return ModelRequest(messages=(ModelMessage(role="user", content=text),))


def sse_lines(*payloads):
    """Build a deterministic SSE byte-line stream from JSON payloads."""

    lines = [f"data: {json.dumps(p)}\n".encode("utf-8") for p in payloads]
    lines.append(b"data: [DONE]\n")
    return iter(lines)


def sse_transport(payloads):
    calls = []

    def transport(url, headers, body, timeout):
        calls.append({"url": url, "headers": headers, "body": body})
        return sse_lines(*payloads)

    transport.calls = calls
    return transport


# --- OpenAI SSE ---

def test_openai_stream_emits_text_deltas_and_completion(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    transport = sse_transport([
        {"choices": [{"delta": {"content": "Hello"}}]},
        {"choices": [{"delta": {"content": " world"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
    ])
    adapter = OpenAIAdapter(stream_transport=transport)
    events = list(adapter.stream(_req()))
    text = "".join(e.text for e in events if e.kind == "text_delta")
    assert text == "Hello world"
    completed = next(e for e in events if e.kind == "completed")
    assert completed.payload.text == "Hello world"
    assert completed.payload.input_tokens == 5
    # stream: true was sent to the vendor.
    body = json.loads(transport.calls[0]["body"].decode("utf-8"))
    assert body["stream"] is True


def test_openai_stream_tool_call(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    transport = sse_transport([
        {"choices": [{"delta": {"tool_calls": [{"id": "c1", "function": {"name": "search", "arguments": '{"q": "x"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ])
    adapter = OpenAIAdapter(stream_transport=transport)
    events = list(adapter.stream(_req()))
    tool_event = next(e for e in events if e.kind == "tool_call")
    assert tool_event.payload["name"] == "search"
    completed = next(e for e in events if e.kind == "completed")
    assert completed.payload.tool_calls[0]["arguments"] == {"q": "x"}
    assert completed.payload.finish_reason == "tool_calls"


def test_openai_stream_malformed_sse_raises_classified(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    def transport(url, headers, body, timeout):
        return iter([b"data: {not json}\n"])
    adapter = OpenAIAdapter(stream_transport=transport)
    with pytest.raises(ModelAdapterError) as exc_info:
        list(adapter.stream(_req()))
    assert "malformed SSE" in str(exc_info.value)


def test_openai_stream_falls_back_to_complete_without_transport(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    def http_transport(url, headers, body, timeout):
        return 200, json.dumps({
            "id": "r", "model": "gpt-4o",
            "choices": [{"message": {"content": "full"}, "finish_reason": "stop"}], "usage": {},
        }).encode()
    adapter = OpenAIAdapter(transport=http_transport, stream_transport=None)  # explicit replay opt-out
    events = list(adapter.stream(_req()))
    text = "".join(e.text for e in events if e.kind == "text_delta")
    assert text == "full"


# --- Anthropic SSE ---

def test_anthropic_stream_emits_text_deltas_and_completion(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    transport = sse_transport([
        {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "你好"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "世界"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
    ])
    adapter = AnthropicAdapter(stream_transport=transport)
    events = list(adapter.stream(_req()))
    text = "".join(e.text for e in events if e.kind == "text_delta")
    assert text == "你好世界"
    completed = next(e for e in events if e.kind == "completed")
    assert completed.payload.input_tokens == 5
    assert completed.payload.output_tokens == 2
    assert completed.payload.finish_reason == "stop"


def test_anthropic_stream_tool_use(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    transport = sse_transport([
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "search"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"q": '}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '"x"}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 3}},
    ])
    adapter = AnthropicAdapter(stream_transport=transport)
    events = list(adapter.stream(_req()))
    tool_event = next(e for e in events if e.kind == "tool_call")
    assert tool_event.payload["name"] == "search"
    # partial_json fragments are accumulated and parsed.
    assert tool_event.payload["arguments"] == {"q": "x"}
    completed = next(e for e in events if e.kind == "completed")
    assert completed.payload.finish_reason == "tool_calls"


def test_anthropic_stream_falls_back_without_transport(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    def http_transport(url, headers, body, timeout):
        return 200, json.dumps({
            "id": "m", "model": "claude", "content": [{"type": "text", "text": "full"}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1},
        }).encode()
    adapter = AnthropicAdapter(transport=http_transport, stream_transport=None)  # explicit replay opt-out
    events = list(adapter.stream(_req()))
    assert any(e.kind == "text_delta" and "full" in e.text for e in events)


# --- 对抗性审查发现的回归 ---

def test_openai_real_fragmented_tool_call_arguments(monkeypatch):
    """Real OpenAI sends name once, then argument fragments in later deltas."""
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    transport = sse_transport([
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "search", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"q":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ' "x"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ])
    adapter = OpenAIAdapter(stream_transport=transport)
    events = list(adapter.stream(_req()))
    completed = next(e for e in events if e.kind == "completed")
    # The fragments are accumulated by index and parsed, NOT dropped.
    assert completed.payload.tool_calls[0]["name"] == "search"
    assert completed.payload.tool_calls[0]["arguments"] == {"q": "x"}
    assert completed.payload.finish_reason == "tool_calls"


def test_openai_stream_usage_is_allowlisted(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    transport = sse_transport([
        {"choices": [{"delta": {"content": "x"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "evil": "sk-SECRET"}},
    ])
    adapter = OpenAIAdapter(stream_transport=transport)
    completed = next(e for e in adapter.stream(_req()) if e.kind == "completed")
    assert "SECRET" not in str(completed.payload.vendor_ref)
    assert completed.payload.vendor_ref["usage"] == {"prompt_tokens": 1}


def test_anthropic_orphan_tool_block_flushed_without_crash(monkeypatch):
    """Stream ends early (no content_block_stop): orphan tool_use is flushed, not crashed."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    transport = sse_transport([
        {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "search"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"q": "x"}'}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1}},
    ])
    adapter = AnthropicAdapter(stream_transport=transport)
    events = list(adapter.stream(_req()))
    # The orphan block is flushed as a tool_call event AND included in completed.
    tool_events = [e for e in events if e.kind == "tool_call"]
    assert len(tool_events) == 1
    assert tool_events[0].payload["name"] == "search"
    completed = next(e for e in events if e.kind == "completed")
    assert completed.payload.tool_calls[0]["name"] == "search"


def test_anthropic_missing_index_is_classified_not_keyerror(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    transport = sse_transport([
        {"type": "content_block_start", "content_block": {"type": "tool_use", "id": "t1", "name": "x"}},  # no index
    ])
    adapter = AnthropicAdapter(stream_transport=transport)
    with pytest.raises(ModelAdapterError) as exc_info:
        list(adapter.stream(_req()))
    assert "missing index" in str(exc_info.value)


def test_sse_bom_does_not_eat_first_event(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    def transport(url, headers, body, timeout):
        return iter([
            b'\xef\xbb\xbfdata: {"choices": [{"delta": {"content": "first"}}]}\n',
            b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n',
            b"data: [DONE]\n",
        ])
    adapter = OpenAIAdapter(stream_transport=transport)
    text = "".join(e.text for e in adapter.stream(_req()) if e.kind == "text_delta")
    assert "first" in text  # BOM did not swallow the first event


def test_secure_stream_transport_is_the_default():
    """secure_stream_transport is the default stream transport (not dead code)."""
    from noname_harness.vendor_http import secure_stream_transport
    assert OpenAIAdapter().stream_transport is secure_stream_transport
    assert AnthropicAdapter().stream_transport is secure_stream_transport


def test_injected_transport_error_is_classified(monkeypatch):
    import socket
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    def transport(url, headers, body, timeout):
        raise socket.timeout("timed out")
    adapter = OpenAIAdapter(stream_transport=transport)
    with pytest.raises(ModelAdapterError) as exc_info:
        list(adapter.stream(_req()))
    assert exc_info.value.error_class == "timeout"
