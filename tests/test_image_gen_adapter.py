"""Real image-model plugin: contract verified against a replay transport.

No network, no API key: the transport is injected, so request construction,
response mapping, error classification, credential safety and the plugin
lifecycle are all verified deterministically.
"""

from __future__ import annotations

import base64
import json
import socket
import urllib.error

import pytest

from noname_harness.adapters import ModelAdapterError
from noname_harness.card_images import card_image_for
from noname_harness.image_gen_adapter import (
    OpenAIImageGenAdapter,
    build_card_prompt,
    load_image_gen_plugin,
)
from noname_harness.plugins import PluginRuntime
from noname_harness.store import HarnessStore
from noname_harness.taste import TasteService
from noname_harness.taste_cards import TasteCardService
from noname_harness.tools import ToolRegistry

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(64))

def replay_transport(status=200, payload=None, raw=None):
    """A deterministic transport that records the request and replays a response."""

    calls = []

    def transport(url, headers, body, timeout):
        calls.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
        if raw is not None:
            return status, raw
        return status, json.dumps(payload or {}).encode("utf-8")

    transport.calls = calls
    return transport

def ok_payload(b64=None, usage=None):
    payload = {"created": 1760000000, "data": [{"b64_json": b64 or base64.b64encode(_PNG_BYTES).decode()}]}
    if usage:
        payload["usage"] = usage
    return payload

def make_adapter(monkeypatch, transport=None, **kwargs):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-image-secret")
    adapter = OpenAIImageGenAdapter(
        transport=transport or replay_transport(payload=ok_payload()), **kwargs
    )
    # Directly-constructed adapters are fail-closed (F5); activate explicitly
    # here.  The lifecycle gate itself is tested in the plugin section below.
    adapter._mark_plugin_loaded()
    return adapter

_SUMMARY = {"title": "克制", "attitude": "工具要克制、改动小而可回退", "track": "authored"}

# --- request building / response mapping -----------------------------------

def test_request_building_endpoint_auth_and_body(monkeypatch):
    transport = replay_transport(payload=ok_payload())
    adapter = make_adapter(monkeypatch, transport)
    adapter(_SUMMARY)
    call = transport.calls[0]
    assert call["url"] == "https://api.openai.com/v1/images/generations"
    assert call["headers"]["Authorization"] == "Bearer sk-image-secret"
    assert call["headers"]["Content-Type"] == "application/json"
    body = json.loads(call["body"].decode("utf-8"))
    assert body["model"] == "gpt-image-1"
    assert body["n"] == 1
    assert body["size"] == "1024x1024"
    # The prompt is built from the card's text only.
    assert "克制" in body["prompt"]
    assert "工具要克制" in body["prompt"]
    assert "authored" in body["prompt"]
    # A sensible default timeout is passed through.
    assert call["timeout"] == adapter.timeout == 120.0

def test_response_maps_to_image_bytes_and_media_type(monkeypatch):
    adapter = make_adapter(monkeypatch)
    generated = adapter(_SUMMARY)
    assert generated.image_bytes == _PNG_BYTES
    assert generated.media_type == "image/png"

def test_usage_is_allowlisted_and_accounted(monkeypatch):
    usage = {"total_tokens": 1200, "input_tokens": 20, "evil": "sk-image-secret"}
    adapter = make_adapter(monkeypatch, replay_transport(payload=ok_payload(usage=usage)))
    generated = adapter(_SUMMARY)
    assert adapter.last_usage == {"total_tokens": 1200, "input_tokens": 20}
    assert generated.metadata["usage"] == {"total_tokens": 1200, "input_tokens": 20}
    assert "sk-image-secret" not in str(generated.metadata["vendor_ref"])
    assert "evil" not in str(generated.metadata["vendor_ref"])

def test_missing_api_key_raises_auth(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    adapter = OpenAIImageGenAdapter(transport=replay_transport(payload=ok_payload()))
    adapter._mark_plugin_loaded()
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter(_SUMMARY)
    assert exc_info.value.error_class == "auth"
    assert exc_info.value.retryable is False

# --- HTTPS enforcement ------------------------------------------------------

def test_plaintext_http_base_url_refused_by_default(monkeypatch):
    # Refused at construction now (fail-closed): an insecure trust boundary
    # can never be configured, let alone used.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-image-secret")
    with pytest.raises(ModelAdapterError) as exc_info:
        OpenAIImageGenAdapter(base_url="http://attacker.example/v1")
    assert exc_info.value.error_class == "auth"

def test_allow_insecure_opt_in_permits_local_http(monkeypatch):
    transport = replay_transport(payload=ok_payload())
    adapter = make_adapter(
        monkeypatch, transport, base_url="http://localhost:8080/v1", allow_insecure=True
    )
    adapter(_SUMMARY)
    assert transport.calls[0]["url"] == "http://localhost:8080/v1/images/generations"

# --- redirect refusal --------------------------------------------------------

def test_default_transport_refuses_redirect():
    from noname_harness.vendor_http import _NoRedirectHandler
    handler = _NoRedirectHandler()
    assert handler.redirect_request(None, None, None, None, None, None) is None

def test_redirect_response_is_not_followed_and_classified():
    # The shared secure transport raises on any 3xx instead of following it
    # (a followed redirect would forward the Bearer key to another host).
    import urllib.request
    from noname_harness.vendor_http import secure_transport

    class RedirectOpener:
        def open(self, request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 302, "Found", {"Location": "http://evil.example/x"}, None
            )

    real_build = urllib.request.build_opener
    urllib.request.build_opener = lambda *handlers: RedirectOpener()
    try:
        with pytest.raises(ModelAdapterError) as exc_info:
            secure_transport(
                "https://api.openai.com/v1/images/generations",
                {"Authorization": "Bearer k"}, b"{}", 5.0,
            )
    finally:
        urllib.request.build_opener = real_build
    assert "redirect" in str(exc_info.value).lower()
    assert exc_info.value.retryable is False

# --- error classification ----------------------------------------------------

@pytest.mark.parametrize("status,expected,retryable", [
    (401, "auth", False),
    (403, "auth", False),
    (429, "rate_limit", True),
    (400, "invalid_request", False),
    (500, "overloaded", True),
    (503, "overloaded", True),
    (418, "unknown", False),
])
def test_http_error_classification(monkeypatch, status, expected, retryable):
    adapter = make_adapter(
        monkeypatch, replay_transport(status=status, payload={"error": {"message": "x"}})
    )
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter(_SUMMARY)
    assert exc_info.value.error_class == expected
    assert exc_info.value.retryable is retryable
    assert exc_info.value.vendor_ref["status"] == status

def test_transport_timeout_is_retryable(monkeypatch):
    def transport(url, headers, body, timeout):
        raise socket.timeout("timed out")

    adapter = make_adapter(monkeypatch, transport)
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter(_SUMMARY)
    assert exc_info.value.error_class == "timeout"
    assert exc_info.value.retryable is True

def test_transport_urlerror_timeout_is_retryable(monkeypatch):
    def transport(url, headers, body, timeout):
        raise urllib.error.URLError(TimeoutError("timed out"))

    adapter = make_adapter(monkeypatch, transport)
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter(_SUMMARY)
    assert exc_info.value.error_class == "timeout"
    assert exc_info.value.retryable is True

# --- vendor_ref whitelist -----------------------------------------------------

def test_error_vendor_ref_never_contains_body_or_key(monkeypatch):
    secret = "sk-image-secret"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    def transport(url, headers, body, timeout):
        return 401, json.dumps(
            {"error": {"message": f"invalid key: Bearer {secret}", "code": "bad_key"}}
        ).encode()

    adapter = OpenAIImageGenAdapter(transport=transport)
    adapter._mark_plugin_loaded()
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter(_SUMMARY)
    ref = exc_info.value.vendor_ref
    assert secret not in str(ref)
    assert "body" not in ref
    assert ref["status"] == 401
    # "bad_key" is not a known vendor enum token: recorded as "unlisted" so no
    # attacker-controlled string reaches the ledger (F1).
    assert ref.get("error_code") == "unlisted"

def test_vendor_ref_never_contains_bytes(monkeypatch):
    adapter = make_adapter(monkeypatch)
    adapter(_SUMMARY)
    ref = adapter.last_vendor_ref
    assert all(not isinstance(v, (bytes, bytearray)) for v in ref.values())
    usage = ref.get("usage", {})
    assert all(not isinstance(v, (bytes, bytearray)) for v in usage.values())

def test_success_vendor_ref_is_a_reference_not_the_body(monkeypatch):
    adapter = make_adapter(monkeypatch)
    adapter(_SUMMARY)
    # The reference keeps status/created, never the megabyte-scale image payload.
    assert adapter.last_vendor_ref["status"] == 200
    assert adapter.last_vendor_ref["created"] == 1760000000
    assert "data" not in adapter.last_vendor_ref
    assert "b64_json" not in str(adapter.last_vendor_ref)

# --- credential hygiene --------------------------------------------------------

def test_api_key_never_in_repr_or_str(monkeypatch):
    adapter = OpenAIImageGenAdapter(api_key="sk-super-secret-key")
    assert "sk-super-secret-key" not in repr(adapter)
    assert "sk-super-secret-key" not in str(adapter)

# --- response robustness -------------------------------------------------------

def test_unparseable_response_is_classified(monkeypatch):
    adapter = make_adapter(monkeypatch, replay_transport(raw=b"not json{"))
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter(_SUMMARY)
    assert exc_info.value.error_class == "unknown"
    assert exc_info.value.vendor_ref["status"] == 200

def test_missing_image_data_is_classified(monkeypatch):
    adapter = make_adapter(monkeypatch, replay_transport(payload={"created": 1, "data": []}))
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter(_SUMMARY)
    assert "no image data" in str(exc_info.value)

def test_invalid_base64_is_classified_not_a_crash(monkeypatch):
    adapter = make_adapter(monkeypatch, replay_transport(payload=ok_payload(b64="!!!not-b64!!!")))
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter(_SUMMARY)
    assert exc_info.value.error_class == "unknown"

def test_url_response_refused_single_audited_egress(monkeypatch):
    payload = {"created": 1, "data": [{"url": "https://cdn.example/img.png"}]}
    adapter = make_adapter(monkeypatch, replay_transport(payload=payload))
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter(_SUMMARY)
    assert "URL fetching is refused" in str(exc_info.value)
    assert "https://cdn.example" not in str(exc_info.value.vendor_ref)

def test_invalid_size_rejected_locally(monkeypatch):
    # Size is validated at construction (fail-closed validate-then-use): an
    # invalid size can never be configured, let alone sent.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-image-secret")
    with pytest.raises(ModelAdapterError) as exc_info:
        OpenAIImageGenAdapter(size="huge")
    assert exc_info.value.error_class == "invalid_request"

# --- ImageGenerator protocol / metadata contract ------------------------------

def test_metadata_contract_is_complete_and_seed_honest(monkeypatch):
    adapter = make_adapter(monkeypatch)
    generated = adapter(_SUMMARY)
    metadata = generated.metadata
    for key in ("model", "prompt", "seed", "version", "size"):
        assert key in metadata
    assert metadata["model"] == "openai-image-gpt-image-1"
    # The vendor API accepts no seed: honest, never forged.
    assert metadata["seed"] is None
    assert metadata["seed_supported"] is False
    assert metadata["rebuildable"] is False
    assert metadata["abstract"] is True
    assert metadata["no_faces"] is True
    # The recorded prompt is exactly what was sent.
    assert metadata["prompt"] == build_card_prompt(_SUMMARY)

def test_generator_id_follows_model_config(monkeypatch):
    adapter = make_adapter(monkeypatch, model_id="dall-e-3")
    assert adapter.generator_id() == "openai-image-dall-e-3"
    assert adapter(_SUMMARY).metadata["model"] == "openai-image-dall-e-3"

def test_works_as_card_image_for_generator(monkeypatch):
    transport = replay_transport(payload=ok_payload())
    adapter = make_adapter(monkeypatch, transport)
    generated = card_image_for(
        {"title": "克制", "attitude": "a", "track": "authored", "secret": "must-not-leak"},
        adapter,
    )
    assert generated.image_bytes == _PNG_BYTES
    body = json.loads(transport.calls[0]["body"].decode("utf-8"))
    # The multi-modal boundary holds: only title/attitude/track reach the vendor.
    assert "must-not-leak" not in body["prompt"]

def test_full_pipeline_persists_via_taste_card_service(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-image-secret")
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "image project")
    try:
        taste = TasteService(store)
        record = taste.record_authored({"judgement": "工具要克制"}, scope="user")
        cards = TasteCardService(store)
        card = cards.create_card(
            title="克制", attitude="工具要克制", track="authored",
            scope="user", taste_ids=[record["id"]],
        )
        adapter = OpenAIImageGenAdapter(transport=replay_transport(payload=ok_payload()))
        adapter._mark_plugin_loaded()
        updated = cards.generate_image(card["id"], "yuchen", generator=adapter)
        image = updated["image"]
        assert image["model"] == "openai-image-gpt-image-1"
        assert image["media_type"] == "image/png"
        assert image["seed"] is None
        assert image["seed_supported"] is False
        # Bytes persisted to the workspace as an append-only evidence span path.
        assert (root / image["path"]).read_bytes() == _PNG_BYTES
        event = store.get_event(image["event_id"])
        assert event.event_type == "card.image.generated"
        assert store.evidence_for_event(event.id)
        assert store.verify_integrity()["ok"] is True
    finally:
        store.close()

# --- plugin packaging -----------------------------------------------------------

def _runtime(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "plugin project")
    return store, PluginRuntime(store, ToolRegistry(store))

def test_loads_as_zero_tool_plugin_and_audits(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    store, runtime = _runtime(tmp_path)
    try:
        adapter = load_image_gen_plugin(runtime, transport=replay_transport(payload=ok_payload()))
        assert adapter.generator_id() == "openai-image-gpt-image-1"
        loaded = runtime.loaded_plugins()
        entry = next(p for p in loaded if p["plugin_id"] == "imagegen-openai-gpt-image-1")
        assert entry["capabilities"] == ["image-generation"]
        assert entry["tools"] == []  # zero-tool plugin does not break the runtime
        event = next(e for e in store.list_events("system", limit=50) if e.event_type == "plugin.loaded")
        assert event.payload["plugin_id"] == "imagegen-openai-gpt-image-1"
        assert set(event.payload["side_effects"]) == {"network-egress", "billing"}
        assert event.payload["tools"] == []
        assert store.verify_integrity()["ok"] is True
    finally:
        store.close()

def test_plugin_unloads_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    store, runtime = _runtime(tmp_path)
    try:
        load_image_gen_plugin(runtime, transport=replay_transport(payload=ok_payload()))
        result = runtime.unload("imagegen-openai-gpt-image-1")
        assert result["unloaded"] == "imagegen-openai-gpt-image-1"
        assert runtime.loaded_plugins() == []
        assert any(
            e.event_type == "plugin.unloaded" for e in store.list_events("system", limit=50)
        )
    finally:
        store.close()

# --- prompt bounds --------------------------------------------------------------

def test_long_card_text_is_truncated(monkeypatch):
    transport = replay_transport(payload=ok_payload())
    adapter = make_adapter(monkeypatch, transport)
    bomb = {"title": "t" * 100_000, "attitude": "a" * 100_000, "track": "x" * 100_000}
    generated = adapter(bomb)
    body = json.loads(transport.calls[0]["body"].decode("utf-8"))
    # Both the wire prompt and the recorded prompt are capped (prompt-bomb defence).
    assert len(body["prompt"]) <= 4_000
    assert len(generated.metadata["prompt"]) <= 4_000
    assert body["prompt"] == generated.metadata["prompt"]

def test_prompt_contains_no_credentials(monkeypatch):
    secret = "sk-image-secret"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    adapter = OpenAIImageGenAdapter(transport=replay_transport(payload=ok_payload()))
    adapter._mark_plugin_loaded()
    generated = adapter(_SUMMARY)
    assert secret not in generated.metadata["prompt"]
    assert secret not in str(generated.metadata["vendor_ref"])


# --- adversarial-review regression tests (REPORT.md: F1-F5 + lows) -------------

# F1: error_code enum allowlist blocks credential echo through the ledger.

def test_error_code_credential_echo_collapsed_to_unlisted(monkeypatch):
    # A hostile endpoint echoes the de-prefixed key body (32 lowercase chars,
    # which matches the old shape regex) as the error code.
    key_body = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    assert len(key_body) == 32
    def transport(url, headers, body, timeout):
        return 401, json.dumps({"error": {"message": "denied", "code": key_body}}).encode()

    adapter = OpenAIImageGenAdapter(transport=transport)
    adapter._mark_plugin_loaded()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-image-secret")
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter(_SUMMARY)
    ref = exc_info.value.vendor_ref
    assert ref["error_code"] == "unlisted"
    assert key_body not in str(ref)

def test_error_code_known_enum_tokens_survive(monkeypatch):
    def transport(url, headers, body, timeout):
        return 401, json.dumps({"error": {"code": "invalid_api_key"}}).encode()

    adapter = OpenAIImageGenAdapter(transport=transport)
    adapter._mark_plugin_loaded()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-image-secret")
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter(_SUMMARY)
    assert exc_info.value.vendor_ref["error_code"] == "invalid_api_key"

def test_error_code_malformed_still_dropped(monkeypatch):
    def transport(url, headers, body, timeout):
        return 401, json.dumps({"error": {"code": "sk-SECRET-Key!!!"}}).encode()

    adapter = OpenAIImageGenAdapter(transport=transport)
    adapter._mark_plugin_loaded()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-image-secret")
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter(_SUMMARY)
    assert "error_code" not in exc_info.value.vendor_ref

# F2: image payload size cap, checked before decoding.

def test_oversized_b64_rejected_without_decoding(monkeypatch):
    from noname_harness import image_gen_adapter as iga

    big_b64 = "A" * (iga._MAX_B64_CHARS + 4)
    transport = replay_transport(payload=ok_payload(b64=big_b64))
    adapter = make_adapter(monkeypatch, transport)
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter(_SUMMARY)
    assert exc_info.value.error_class == "invalid_request"
    assert exc_info.value.retryable is False
    assert "limit" in str(exc_info.value)

def test_max_size_image_still_accepted(monkeypatch):
    from noname_harness import image_gen_adapter as iga

    image = bytes(iga._MAX_IMAGE_BYTES)
    b64 = base64.b64encode(image).decode()
    adapter = make_adapter(monkeypatch, replay_transport(payload=ok_payload(b64=b64)))
    generated = adapter(_SUMMARY)
    assert generated.image_bytes == image

# F3: vendor-controlled media_type whitelist (no active content to disk).

def test_svg_media_type_refused(monkeypatch):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    payload = {
        "created": 1760000000,
        "data": [{"b64_json": base64.b64encode(svg).decode(), "mime_type": "image/svg+xml"}],
    }
    adapter = make_adapter(monkeypatch, replay_transport(payload=payload))
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter(_SUMMARY)
    assert exc_info.value.error_class == "invalid_request"
    assert exc_info.value.retryable is False

def test_media_type_with_control_chars_refused(monkeypatch):
    payload = {
        "created": 1760000000,
        "data": [{
            "b64_json": base64.b64encode(_PNG_BYTES).decode(),
            "mime_type": "image/png\r\nInjected: yes",
        }],
    }
    adapter = make_adapter(monkeypatch, replay_transport(payload=payload))
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter(_SUMMARY)
    assert exc_info.value.error_class == "invalid_request"

def test_webp_media_type_accepted_and_mapped(monkeypatch):
    payload = {
        "created": 1760000000,
        "data": [{"b64_json": base64.b64encode(_PNG_BYTES).decode(), "mime_type": "image/webp"}],
    }
    adapter = make_adapter(monkeypatch, replay_transport(payload=payload))
    assert adapter(_SUMMARY).media_type == "image/webp"

# F4: usage/created ints bounded (no bools, negatives, or huge values).

def test_usage_negative_huge_and_bool_dropped(monkeypatch):
    usage = {
        "total_tokens": -1_000_000_000,
        "input_tokens": True,
        "output_tokens": 2**63,
    }
    payload = ok_payload(usage=usage)
    payload["created"] = -5
    adapter = make_adapter(monkeypatch, replay_transport(payload=payload))
    generated = adapter(_SUMMARY)
    assert adapter.last_usage == {}
    assert generated.metadata["usage"] == {}
    assert "created" not in adapter.last_vendor_ref

# F5: side effects bound to the plugin lifecycle (fail-closed).

def test_unloaded_adapter_refuses_to_generate(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-image-secret")
    adapter = OpenAIImageGenAdapter(transport=replay_transport(payload=ok_payload()))
    with pytest.raises(ModelAdapterError) as exc_info:
        adapter(_SUMMARY)
    assert exc_info.value.error_class == "invalid_request"
    assert "not active" in str(exc_info.value)

def test_plugin_unload_deactivates_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    store, runtime = _runtime(tmp_path)
    try:
        transport = replay_transport(payload=ok_payload())
        adapter = load_image_gen_plugin(runtime, transport=transport)
        adapter(_SUMMARY)  # works while loaded
        assert len(transport.calls) == 1
        runtime.unload("imagegen-openai-gpt-image-1")
        with pytest.raises(ModelAdapterError) as exc_info:
            adapter(_SUMMARY)
        assert "not active" in str(exc_info.value)
        assert len(transport.calls) == 1  # no egress after unload
    finally:
        store.close()

# LOW: canonical size reaches the wire (validate-then-use).

def test_size_is_canonicalised_and_sent(monkeypatch):
    transport = replay_transport(payload=ok_payload())
    adapter = make_adapter(monkeypatch, transport, size="  1024X1024 ")
    adapter(_SUMMARY)
    body = json.loads(transport.calls[0]["body"].decode("utf-8"))
    assert body["size"] == "1024x1024"
    assert adapter.size == "1024x1024"

# LOW: timeout validated at construction.

@pytest.mark.parametrize("bad_timeout", [0, -1, float("nan"), float("inf"), "abc", None])
def test_invalid_timeout_rejected_at_construction(monkeypatch, bad_timeout):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-image-secret")
    with pytest.raises(ModelAdapterError) as exc_info:
        OpenAIImageGenAdapter(timeout=bad_timeout)
    assert exc_info.value.error_class == "invalid_request"

# LOW: base_url trust boundary tightened.

@pytest.mark.parametrize("bad_url", [
    "https://user:pass@api.openai.com/v1",
    "https://api.openai.com./v1",
    "https://api.openai.com/v1\x0b",
])
def test_base_url_userinfo_trailing_dot_control_chars_refused(monkeypatch, bad_url):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-image-secret")
    with pytest.raises(ModelAdapterError) as exc_info:
        OpenAIImageGenAdapter(base_url=bad_url)
    assert exc_info.value.error_class == "auth"

# LOW: a failed call must not inherit the previous success's usage.

def test_failed_call_clears_last_usage(monkeypatch):
    secret = "sk-image-secret"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    state = {"ok": True}
    def transport(url, headers, body, timeout):
        if state["ok"]:
            return 200, json.dumps(ok_payload(usage={"total_tokens": 42})).encode()
        return 500, json.dumps({"error": {"message": "boom"}}).encode()

    adapter = OpenAIImageGenAdapter(transport=transport)
    adapter._mark_plugin_loaded()
    adapter(_SUMMARY)
    assert adapter.last_usage == {"total_tokens": 42}
    state["ok"] = False
    with pytest.raises(ModelAdapterError):
        adapter(_SUMMARY)
    assert adapter.last_usage == {}
    assert adapter.last_vendor_ref == {}
