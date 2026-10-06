"""Multimodal message format: content blocks, vendor mapping, capability consistency."""

from __future__ import annotations

import json

import pytest

from noname_harness.adapters import (
    ImageBlock,
    LocalEchoAdapter,
    ModelAdapterError,
    ModelMessage,
    ModelRequest,
    TextBlock,
)
from noname_harness.anthropic_adapter import AnthropicAdapter
from noname_harness.openai_adapter import OpenAIAdapter


def replay_transport(status=200, payload=None):
    calls = []

    def transport(url, headers, body, timeout):
        calls.append(body)
        return status, json.dumps(payload or {}).encode("utf-8")

    transport.calls = calls
    return transport


def ok_openai():
    return {"id": "r", "model": "gpt-4o", "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}


def ok_anthropic():
    return {"id": "m", "model": "claude", "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}}


# --- ContentBlock validation ---

def test_image_block_validation():
    with pytest.raises(ValueError):
        ImageBlock(media_type="image/png")  # no data or url
    with pytest.raises(ValueError):
        ImageBlock(media_type="video/mp4", data="x")
    block = ImageBlock(media_type="image/png", data="abc")
    assert block.kind == "image"


def test_model_message_content_validation():
    with pytest.raises(ValueError):
        ModelMessage(role="user", content=123)
    with pytest.raises(ValueError):
        ModelMessage(role="user", content=[{"not": "a block"}])
    msg = ModelMessage(role="user", content=[TextBlock("看这张图"), ImageBlock("image/png", data="x")])
    assert msg.is_multimodal() is True
    assert msg.text() == "看这张图"
    plain = ModelMessage(role="user", content="plain")
    assert plain.is_multimodal() is False
    assert plain.text() == "plain"


def test_plain_string_content_stays_backwards_compatible():
    msg = ModelMessage(role="user", content="hello")
    assert msg.text() == "hello"
    assert not msg.is_multimodal()


# --- [OI] mapping ---

def test_openai_multimodal_maps_to_content_parts(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    transport = replay_transport(payload=ok_openai())
    adapter = OpenAIAdapter(transport=transport)
    request = ModelRequest(messages=(
        ModelMessage(role="user", content=[
            TextBlock("这是什么"),
            ImageBlock("image/png", data="aW1hZ2U="),
        ]),
    ))
    adapter.complete(request)
    body = json.loads(transport.calls[0].decode("utf-8"))
    content = body["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "这是什么"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == "data:image/png;base64,aW1hZ2U="


def test_openai_image_url_source(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    transport = replay_transport(payload=ok_openai())
    adapter = OpenAIAdapter(transport=transport)
    request = ModelRequest(messages=(
        ModelMessage(role="user", content=[ImageBlock("image/png", url="https://example.com/img.png")]),
    ))
    adapter.complete(request)
    body = json.loads(transport.calls[0].decode("utf-8"))
    assert body["messages"][0]["content"][0]["image_url"]["url"] == "https://example.com/img.png"


def test_openai_plain_text_stays_plain_string(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    transport = replay_transport(payload=ok_openai())
    adapter = OpenAIAdapter(transport=transport)
    adapter.complete(ModelRequest(messages=(ModelMessage(role="user", content="plain"),)))
    body = json.loads(transport.calls[0].decode("utf-8"))
    assert body["messages"][0]["content"] == "plain"  # no part array for text


# --- Anthropic mapping ---

def test_anthropic_multimodal_maps_to_image_block(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    transport = replay_transport(payload=ok_anthropic())
    adapter = AnthropicAdapter(transport=transport)
    request = ModelRequest(messages=(
        ModelMessage(role="user", content=[
            TextBlock("分析这张图"),
            ImageBlock("image/jpeg", data="anNvZw=="),
        ]),
    ))
    adapter.complete(request)
    body = json.loads(transport.calls[0].decode("utf-8"))
    content = body["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "分析这张图"}
    assert content[1]["type"] == "image"
    assert content[1]["source"] == {"type": "base64", "media_type": "image/jpeg", "data": "anNvZw=="}


def test_anthropic_image_url_source(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    transport = replay_transport(payload=ok_anthropic())
    adapter = AnthropicAdapter(transport=transport)
    request = ModelRequest(messages=(
        ModelMessage(role="user", content=[ImageBlock("image/png", url="https://example.com/x.png")]),
    ))
    adapter.complete(request)
    body = json.loads(transport.calls[0].decode("utf-8"))
    assert body["messages"][0]["content"][0]["source"] == {"type": "url", "url": "https://example.com/x.png"}


def test_anthropic_plain_text_stays_plain_string(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    transport = replay_transport(payload=ok_anthropic())
    adapter = AnthropicAdapter(transport=transport)
    adapter.complete(ModelRequest(messages=(ModelMessage(role="user", content="plain"),)))
    body = json.loads(transport.calls[0].decode("utf-8"))
    assert body["messages"][0]["content"] == "plain"


# --- capability consistency ---

def test_vision_false_adapter_rejects_image_not_drops_it():
    adapter = LocalEchoAdapter()  # capability.vision == False
    request = ModelRequest(messages=(
        ModelMessage(role="user", content=[TextBlock("hi"), ImageBlock("image/png", data="x")]),
    ))
    # The reference adapter REJECTS the image (classified), never silently drops it.
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter.complete(request)
    assert exc_info.value.error_class == "invalid_request"
    assert adapter.capability().vision is False


def test_token_counting_uses_text_not_blocks():
    request = ModelRequest(messages=(
        ModelMessage(role="user", content=[TextBlock("one two three"), ImageBlock("image/png", data="x")]),
    ))
    from noname_harness.vendor_http import word_count_cost
    cost = word_count_cost("m", request)
    assert cost["estimated_input_words"] == 3


# --- 对抗性审查发现的回归 ---

def test_vision_false_openai_rejects_multimodal_on_any_role(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    adapter = OpenAIAdapter(vision=False, transport=replay_transport())
    # tool role with an image: rejected too (not just user role).
    request = ModelRequest(messages=(
        ModelMessage(role="tool", content=[TextBlock("t"), ImageBlock("image/png", data="x")], name="tid"),
    ))
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter.complete(request)
    assert exc_info.value.error_class == "invalid_request"


def test_vision_false_anthropic_rejects_multimodal_tool_and_system(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    adapter = AnthropicAdapter(vision=False, transport=replay_transport())
    # tool role with image.
    with pytest.raises(ModelAdapterError):
        adapter.complete(ModelRequest(messages=(
            ModelMessage(role="tool", content=[TextBlock("t"), ImageBlock("image/png", data="x")], name="tid"),
        )))
    # system role with image.
    with pytest.raises(ModelAdapterError):
        adapter.complete(ModelRequest(messages=(
            ModelMessage(role="system", content=[TextBlock("s"), ImageBlock("image/png", data="x")]),
            ModelMessage(role="user", content="hi"),
        )))


def test_anthropic_multimodal_system_is_classified_not_typeerror(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    adapter = AnthropicAdapter(transport=replay_transport())  # vision=True (default)
    # A multimodal system message is a caller error, classified (not raw TypeError).
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter.complete(ModelRequest(messages=(
            ModelMessage(role="system", content=[TextBlock("sys"), ImageBlock("image/png", data="x")]),
            ModelMessage(role="user", content="hi"),
        )))
    assert "system message content must be a plain string" in str(exc_info.value)


def test_image_block_requires_exactly_one_source():
    with pytest.raises(ValueError):
        ImageBlock("image/png", data="x", url="https://example.com/i.png")  # both
    with pytest.raises(ValueError):
        ImageBlock("image/png")  # neither
    # exactly one is fine
    ImageBlock("image/png", data="x")
    ImageBlock("image/png", url="https://example.com/i.png")


def test_image_block_size_limit():
    with pytest.raises(ValueError):
        ImageBlock("image/png", data="x" * 21_000_000)


def test_empty_content_block_list_rejected():
    with pytest.raises(ValueError):
        ModelMessage(role="user", content=[])


def test_text_joins_blocks_with_newline():
    msg = ModelMessage(role="user", content=[TextBlock("hello"), TextBlock("world")])
    assert msg.text() == "hello\nworld"


def test_openai_tool_message_flattened_to_text(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    transport = replay_transport(payload=ok_openai())
    adapter = OpenAIAdapter(transport=transport)  # vision=True default
    request = ModelRequest(messages=(
        ModelMessage(role="tool", content=[TextBlock("result text")], name="tid"),
    ))
    adapter.complete(request)
    body = json.loads(transport.calls[0].decode("utf-8"))
    # [OI] tool messages carry string content, not a part array.
    assert body["messages"][0]["content"] == "result text"
