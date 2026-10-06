"""Real embedding service: EmbeddingFn protocol, credential safety, integration."""

from __future__ import annotations

import json

import pytest

from noname_harness.adapters import ModelAdapterError
from noname_harness.embedding_service import OpenAIEmbedding, load_openai_embedding
from noname_harness.plugins import PluginRuntime
from noname_harness.store import HarnessStore
from noname_harness.tools import ToolRegistry


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "embedding project")
    return store, root


def replay_transport(status=200, payload=None):
    calls = []

    def transport(url, headers, body, timeout):
        calls.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
        return status, json.dumps(payload or {}).encode("utf-8")

    transport.calls = calls
    return transport


def ok_payload(dims=4):
    return {
        "id": "emb-123",
        "model": "text-embedding-3-small",
        "data": [{"embedding": [0.1, 0.2, 0.3, 0.4][:dims]}],
        "usage": {"prompt_tokens": 3, "total_tokens": 3},
    }


def test_request_building_and_auth_header(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-emb-secret")
    transport = replay_transport(payload=ok_payload())
    service = OpenAIEmbedding(transport=transport)
    service("hello world")
    call = transport.calls[0]
    assert call["url"].endswith("/embeddings")
    assert call["headers"]["Authorization"] == "Bearer sk-emb-secret"
    body = json.loads(call["body"].decode("utf-8"))
    assert body["model"] == "text-embedding-3-small"
    assert body["input"] == "hello world"


def test_vector_mapping(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    service = OpenAIEmbedding(transport=replay_transport(payload=ok_payload()))
    vector = service("text")
    assert vector == [0.1, 0.2, 0.3, 0.4]
    assert all(isinstance(v, float) for v in vector)


def test_model_id_marks_embedding_space():
    service = OpenAIEmbedding()
    # model_id doubles as the embedding-space identifier for the projection guard.
    assert service.model_id == "text-embedding-3-small"


@pytest.mark.parametrize("status,expected", [
    (401, "auth"), (403, "auth"), (429, "rate_limit"),
    (500, "overloaded"), (400, "invalid_request"),
])
def test_http_error_classification(monkeypatch, status, expected):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    service = OpenAIEmbedding(transport=replay_transport(status=status, payload={"error": {"message": "x"}}))
    with pytest.raises(ModelAdapterError) as exc_info:
        service("text")
    assert exc_info.value.error_class == expected


def test_error_body_never_leaks_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-emb-SECRET")
    def transport(url, headers, body, timeout):
        return 401, json.dumps({"error": {"message": "bad key sk-emb-SECRET"}}).encode()
    service = OpenAIEmbedding(transport=transport)
    with pytest.raises(ModelAdapterError) as exc_info:
        service("text")
    assert "SECRET" not in str(exc_info.value.vendor_ref)


def test_plaintext_http_refused(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    service = OpenAIEmbedding(base_url="http://attacker.example/v1", transport=replay_transport())
    with pytest.raises(ModelAdapterError) as exc_info:
        service("text")
    assert exc_info.value.error_class == "auth"


def test_malformed_responses_fail_loudly(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    cases = [
        b"not json",
        json.dumps({"data": []}).encode(),  # empty data
        json.dumps({"data": [{"embedding": "not-a-vector"}]}).encode(),  # bad vector
        json.dumps([1, 2]).encode(),  # non-dict
    ]
    for raw in cases:
        service = OpenAIEmbedding(transport=lambda u, h, b, t, r=raw: (200, r))
        with pytest.raises(ModelAdapterError):
            service("text")


def test_integrates_with_semantic_recall(tmp_path, monkeypatch):
    """End-to-end: OpenAIEmbedding -> build index -> semantic recall."""
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "note", {"text": "数据库查询超时"})
        store.append_event("s", "note", {"text": "午饭吃什么"})
        # A replay embedding that gives related texts similar vectors.
        def fake_embed_vec(text):
            if "数据库" in text or "DB" in text or "latency" in text.lower() or "超时" in text:
                return [1.0, 0.0, 0.0, 0.0]
            return [0.0, 1.0, 0.0, 0.0]
        def transport(url, headers, body, timeout):
            text = json.loads(body.decode())["input"]
            return 200, json.dumps({"id": "e", "model": "text-embedding-3-small", "data": [{"embedding": fake_embed_vec(text)}], "usage": {}}).encode()
        service = OpenAIEmbedding(transport=transport)
        # Build the index with the real (replay) embedding service.
        result = store.build_embedding_index(service)
        assert result["model_id"] == "text-embedding-3-small"
        # Semantic recall finds the related event by meaning (not just keywords).
        hits = store.search_events_semantic("DB latency 超时", service)
        assert hits
        assert hits[0]["event"].payload["text"] == "数据库查询超时"
        assert hits[0]["similarity"] > 0.9
    finally:
        store.close()


def test_loads_as_a_plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    store, _ = make_store(tmp_path)
    try:
        runtime = PluginRuntime(store, ToolRegistry(store))
        service = load_openai_embedding(runtime, model_id="text-embedding-3-large")
        assert service.model_id == "text-embedding-3-large"
        event = next(e for e in store.list_events("system", limit=50) if e.event_type == "plugin.loaded")
        assert set(event.payload["side_effects"]) == {"network-egress", "billing"}
    finally:
        store.close()


# --- 对抗性审查发现的回归 ---

def test_api_key_not_in_repr():
    service = OpenAIEmbedding(api_key="sk-emb-SECRETKEY")
    assert "sk-emb-SECRETKEY" not in repr(service)
    from noname_harness.openai_adapter import OpenAIAdapter
    from noname_harness.anthropic_adapter import AnthropicAdapter
    assert "sk-x" not in repr(OpenAIAdapter(api_key="sk-x"))
    assert "sk-x" not in repr(AnthropicAdapter(api_key="sk-x"))


def test_non_dict_data_item_fails_with_model_error_not_attribute_error(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    for bad_item in [None, "not-a-dict", 42]:
        service = OpenAIEmbedding(
            transport=replay_transport(payload={"id": "e", "model": "text-embedding-3-small", "data": [bad_item]})
        )
        with pytest.raises(ModelAdapterError):
            service("text")


def test_oversized_input_rejected_locally(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    calls = []
    service = OpenAIEmbedding(transport=lambda *a: calls.append(a) or (200, b"{}"))
    with pytest.raises(ModelAdapterError) as exc_info:
        service("x" * 100_000)
    assert exc_info.value.error_class == "invalid_request"
    assert calls == []  # rejected before any network call


def test_dimension_drift_refused(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    responses = [
        {"id": "e", "model": "text-embedding-3-small", "data": [{"embedding": [0.1, 0.2, 0.3]}], "usage": {}},
        {"id": "e", "model": "text-embedding-3-small", "data": [{"embedding": [0.1, 0.2, 0.3, 0.4, 0.5]}], "usage": {}},
    ]
    service = OpenAIEmbedding(transport=replay_transport(payload=None))
    service.transport = lambda u, h, b, t: (200, json.dumps(responses.pop(0)).encode())
    service("first")  # pins 3 dims
    with pytest.raises(ModelAdapterError) as exc_info:
        service("second")  # 5 dims -> drift refused
    assert "dimension changed" in str(exc_info.value)


def test_vendor_model_mismatch_refused(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    service = OpenAIEmbedding(
        transport=replay_transport(payload={"id": "e", "model": "text-embedding-3-large", "data": [{"embedding": [0.1]}], "usage": {}})
    )
    with pytest.raises(ModelAdapterError) as exc_info:
        service("text")
    assert "expected" in str(exc_info.value)


def test_bool_vector_component_rejected(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    service = OpenAIEmbedding(
        transport=replay_transport(payload={"id": "e", "model": "text-embedding-3-small", "data": [{"embedding": [True, False]}], "usage": {}})
    )
    with pytest.raises(ModelAdapterError):
        service("text")


def test_cross_space_guard_fires_before_embedding_query(tmp_path, monkeypatch):
    """A mismatched query must be refused WITHOUT sending text to the vendor."""
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "note", {"text": "secret query text"})
        ok = {"id": "e", "model": "text-embedding-3-small", "data": [{"embedding": [1.0, 0.0]}], "usage": {}}
        builder = OpenAIEmbedding(transport=replay_transport(payload=ok))
        store.build_embedding_index(builder)
        # Query with a DIFFERENT model: refused before any network call.
        calls = []
        querier = OpenAIEmbedding(model_id="text-embedding-3-large",
                                  transport=lambda *a: calls.append(a) or (200, json.dumps(ok).encode()))
        with pytest.raises(ValueError):
            store.search_events_semantic("secret query text", querier)
        assert calls == []  # no network call for a refused query
    finally:
        store.close()


def test_heterogeneous_index_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s1", "note", {"text": "x"})
        ok = {"id": "e", "model": "text-embedding-3-small", "data": [{"embedding": [1.0, 0.0]}], "usage": {}}
        # Build one session with model A, another with model B -> heterogeneous.
        ok_small = ok
        ok_large = {**ok, "model": "text-embedding-3-large"}
        store.build_embedding_index(OpenAIEmbedding(transport=replay_transport(payload=ok_small)), session_id="s1")
        store.append_event("s2", "note", {"text": "y"})
        store.build_embedding_index(
            OpenAIEmbedding(model_id="text-embedding-3-large", transport=replay_transport(payload=ok_large)),
            session_id="s2",
        )
        with pytest.raises(ValueError):
            store.search_events_semantic("x", OpenAIEmbedding(transport=replay_transport(payload=ok_small)))
    finally:
        store.close()
