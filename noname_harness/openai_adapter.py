"""An OpenAI-compatible model adapter, packaged as a capability plugin.

This is the reference for how a *real vendor* plugs into the harness: behind
the same :class:`~noname_harness.adapters.ModelAdapter` protocol as the
deterministic ``LocalEchoAdapter``, so business logic never notices the
difference.  It honours the vision's bet -- a vendor capability crystallises
into a plugin, and the kernel never depends on the vendor.

Two deliberate design choices keep it testable and honest:

- **The transport is injectable.**  Request-building and response-parsing are
  separated from the actual HTTP call (a callable).  Tests inject a
  deterministic replay transport, so request construction, response mapping,
  error classification and vendor_ref preservation are all verified without
  network or an API key.  The default transport is a real ``urllib`` POST.
- **The API key is never stored or logged.**  It is read from the environment
  and used only in the request header; ``vendor_ref`` carries a *reference* to
  the raw response, never credentials.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterator

from .adapters import (
    ModelAdapterError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    StreamEvent,
)
from .models import ModelCapability
from .vendor_http import _NoRedirectHandler  # re-exported for backwards-compatible imports
from .vendor_http import (
    Transport,
    classify_http_status,
    classify_transport_error,
    json_schema_type,
    safe_usage_ref,
    secure_transport,
    validate_base_url,
    word_count_cost,
)

# A transport maps (url, headers, body_bytes, timeout) -> (status, response_bytes).

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_ENV_KEY = "OPENAI_API_KEY"
_ENV_BASE_URL = "OPENAI_BASE_URL"


@dataclass
class OpenAIAdapter:
    """An OpenAI-compatible chat-completions adapter.

    Works with the OpenAI API and with compatible endpoints (set
    ``OPENAI_BASE_URL``).  The model id and capability come from configuration,
    not hard-coded vendor specifics, so the same class serves many endpoints.
    """

    model_id: str = "gpt-4o-mini"
    context_window: int | None = 128_000
    timeout: float = 60.0
    transport: Transport = secure_transport
    base_url: str | None = None
    # repr=False: the credential must never appear in a repr/log/traceback.
    api_key: str | None = field(default=None, repr=False)
    # Opt-in escape hatch for plaintext HTTP (e.g. a local model server).
    allow_insecure: bool = False

    def id(self) -> str:
        return self.model_id

    def capability(self) -> ModelCapability:
        return ModelCapability(
            reasoning=True,
            vision=True,
            tool_calling=True,
            streaming=True,
            context_window=self.context_window,
        )

    # -- request/response mapping ------------------------------------------

    def _endpoint(self) -> str:
        base = validate_base_url(
            self.base_url or os.environ.get(_ENV_BASE_URL) or _DEFAULT_BASE_URL,
            allow_insecure=self.allow_insecure,
        )
        return f"{base}/chat/completions"

    def _headers(self) -> dict[str, str]:
        key = self.api_key or os.environ.get(_ENV_KEY)
        if not key:
            raise ModelAdapterError("auth", f"{_ENV_KEY} is not set")
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        }

    def _build_body(self, request: ModelRequest) -> bytes:
        payload: dict[str, Any] = {
            "model": self.model_id,
            "messages": [
                {k: v for k, v in {"role": m.role, "content": m.content, "name": m.name}.items() if v is not None}
                for m in request.messages
            ],
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                name: {"type": json_schema_type(t)}
                                for name, t in tool.get("input_schema", {}).items()
                            },
                            "required": list(tool.get("input_schema", {}).keys()),
                        },
                    },
                }
                for tool in request.tools
            ]
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        return json.dumps(payload).encode("utf-8")

    def complete(self, request: ModelRequest) -> ModelResponse:
        body = self._build_body(request)
        headers = self._headers()
        try:
            status, raw = self.transport(self._endpoint(), headers, body, self.timeout)
        except ModelAdapterError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalised at the vendor seam
            raise classify_transport_error(exc, self.timeout) from exc

        if status != 200:
            raise self._classify_http_error(status, raw)

        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ModelAdapterError(
                "unknown",
                f"unparseable vendor response: {exc}",
                # Never raw bytes (not JSON-serialisable, and could carry
                # content) -- a length is enough to audit.
                vendor_ref={"status": 200, "bytes": len(raw)},
            ) from exc
        return self._map_response(data, raw)

    def _map_response(self, data: dict[str, Any], raw: bytes) -> ModelResponse:
        choices = data.get("choices") or []
        if not choices:
            raise ModelAdapterError(
                "unknown",
                "vendor returned no choices",
                vendor_ref={"id": data.get("id")} if isinstance(data, dict) else None,
            )
        message = choices[0].get("message", {})
        usage = data.get("usage", {})
        tool_calls = tuple(
            self._map_tool_call(call, data) for call in message.get("tool_calls", []) or []
        )
        return ModelResponse(
            text=message.get("content") or "",
            tool_calls=tool_calls,
            model_id=data.get("model", self.model_id),
            finish_reason=choices[0].get("finish_reason", "stop"),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            # Preserve a reference for audit -- an allowlist, never the raw
            # body: only the vendor id and the three numeric usage fields are
            # kept, so hostile extra keys can never smuggle content into the
            # ledger, and no credentials are ever stored.
            vendor_ref={
                "status": 200,
                "id": data.get("id"),
                "usage": safe_usage_ref(usage),
            },
        )

    def _map_tool_call(self, call: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        """Map one vendor tool call, normalising malformed arguments.

        A vendor (or OpenAI-compatible server) may return ``arguments`` as a
        dict instead of a string, or as truncated/invalid JSON mid-generation.
        Neither may escape as a raw TypeError/JSONDecodeError outside the
        ModelAdapterError contract.
        """

        function = call.get("function", {})
        arguments = function.get("arguments", "{}")
        if isinstance(arguments, dict):
            parsed = arguments
        else:
            try:
                parsed = json.loads(arguments or "{}")
            except (ValueError, TypeError) as exc:
                raise ModelAdapterError(
                    "unknown",
                    "vendor returned malformed tool-call arguments",
                    vendor_ref={"id": data.get("id")},
                ) from exc
        # A dict (or parsed dict) of unbounded size is a memory-amplification
        # DoS into the ledger; cap the serialized size.
        try:
            serialized = json.dumps(parsed)
        except (ValueError, TypeError) as exc:
            raise ModelAdapterError(
                "unknown",
                "vendor tool-call arguments are not JSON-serialisable",
                vendor_ref={"id": data.get("id")},
            ) from exc
        if len(serialized) > 1_000_000:
            raise ModelAdapterError(
                "unknown",
                "vendor tool-call arguments exceed the 1MB limit",
                vendor_ref={"id": data.get("id")},
            )
        return {
            "name": function.get("name"),
            "arguments": parsed,
            "id": call.get("id"),
        }

    def _classify_http_error(self, status: int, raw: bytes) -> ModelAdapterError:
        return classify_http_status(status, raw)

    def stream(self, request: ModelRequest) -> Iterator[StreamEvent]:
        # A minimal honest stream: complete once and re-emit.  True SSE
        # streaming is a vendor concern layered on the same contract; the
        # deterministic reference keeps stream == complete for auditability.
        response = self.complete(request)
        for call in response.tool_calls:
            yield StreamEvent(kind="tool_call", payload=call)
        if not response.tool_calls and response.text:
            yield StreamEvent(kind="text_delta", text=response.text)
        yield StreamEvent(kind="completed", payload=response)

    def estimate_cost(self, request: ModelRequest) -> dict[str, Any]:
        return word_count_cost(self.model_id, request)


# ---------------------------------------------------------------------------
# Plugin packaging: the adapter crystallises into a loadable plugin.
# ---------------------------------------------------------------------------


def load_openai_adapter(runtime: Any, **adapter_kwargs: Any) -> OpenAIAdapter:
    """Load the OpenAI adapter through a PluginRuntime and return it.

    A vendor capability crystallises into a plugin: the manifest is validated
    and the load audited (``plugin.loaded``) before the adapter is handed back.
    The manifest honestly declares the plugin's real side effects --
    ``network-egress`` (outbound HTTPS) and ``billing`` (metered) -- because
    ``max_permission`` covers only contributed tools, and this plugin
    contributes none.  ``build()`` returns the adapter itself so the caller can
    drive an AgentLoop with it.
    """

    adapter = OpenAIAdapter(**adapter_kwargs)

    from .plugins import Plugin, PluginManifest

    plugin = Plugin(
        manifest=PluginManifest(
            id=f"model-openai-{adapter.model_id}",
            version="1.0.0",
            capabilities=(f"model:{adapter.model_id}", "model-adapter"),
            max_permission="read",
            side_effects=("network-egress", "billing"),
        ),
        build=lambda: [],
    )
    runtime.load(plugin)
    return adapter
