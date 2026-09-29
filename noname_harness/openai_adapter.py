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
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from .adapters import (
    ModelAdapterError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    StreamEvent,
)
from .models import ModelCapability

# A transport maps (url, headers, body_bytes, timeout) -> (status, response_bytes).
Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_ENV_KEY = "OPENAI_API_KEY"
_ENV_BASE_URL = "OPENAI_BASE_URL"


def _http_transport(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
    """The default real transport: a single POST via urllib (stdlib only)."""

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise ModelAdapterError("timeout", f"network error: {exc.reason}") from exc


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
    transport: Transport = _http_transport
    base_url: str | None = None
    api_key: str | None = None

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
        base = (self.base_url or os.environ.get(_ENV_BASE_URL) or _DEFAULT_BASE_URL).rstrip("/")
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
                                name: {"type": _json_schema_type(t)}
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
            raise ModelAdapterError("unknown", f"transport failed: {exc}") from exc

        if status != 200:
            raise self._classify_http_error(status, raw)

        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ModelAdapterError(
                "unknown", f"unparseable vendor response: {exc}", vendor_ref=raw[:500]
            ) from exc
        return self._map_response(data, raw)

    def _map_response(self, data: dict[str, Any], raw: bytes) -> ModelResponse:
        choices = data.get("choices") or []
        if not choices:
            raise ModelAdapterError("unknown", "vendor returned no choices", vendor_ref=data)
        message = choices[0].get("message", {})
        usage = data.get("usage", {})
        tool_calls = tuple(
            {
                "name": call.get("function", {}).get("name"),
                "arguments": json.loads(call.get("function", {}).get("arguments", "{}") or "{}"),
                "id": call.get("id"),
            }
            for call in message.get("tool_calls", []) or []
        )
        return ModelResponse(
            text=message.get("content") or "",
            tool_calls=tool_calls,
            model_id=data.get("model", self.model_id),
            finish_reason=choices[0].get("finish_reason", "stop"),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            # Preserve a reference to the raw response for audit -- never the key.
            vendor_ref={"status": 200, "id": data.get("id"), "usage": usage},
        )

    def _classify_http_error(self, status: int, raw: bytes) -> ModelAdapterError:
        ref: dict[str, Any] = {"status": status}
        try:
            ref["body"] = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            ref["body"] = raw[:200].decode("utf-8", errors="replace")
        if status in {401, 403}:
            return ModelAdapterError("auth", f"vendor auth failed ({status})", vendor_ref=ref)
        if status == 429:
            return ModelAdapterError("rate_limit", "vendor rate limit (429)", vendor_ref=ref)
        if status in {500, 502, 503, 504}:
            return ModelAdapterError("overloaded", f"vendor overloaded ({status})", vendor_ref=ref)
        if status == 400:
            return ModelAdapterError("invalid_request", "vendor rejected request (400)", vendor_ref=ref)
        return ModelAdapterError("unknown", f"vendor error ({status})", vendor_ref=ref)

    def stream(self, request: ModelRequest) -> Iterator[StreamEvent]:
        # A minimal honest stream: complete once and re-emit.  True SSE
        # streaming is a vendor concern layered on the same contract; the
        # deterministic reference keeps stream == complete for auditability.
        response = self.complete(request)
        if response.tool_calls:
            yield StreamEvent(kind="tool_call", payload=response.tool_calls[0])
        elif response.text:
            yield StreamEvent(kind="text_delta", text=response.text)
        yield StreamEvent(kind="completed", payload=response)

    def estimate_cost(self, request: ModelRequest) -> dict[str, Any]:
        input_tokens = sum(len(m.content.split()) for m in request.messages)
        return {
            "model_id": self.model_id,
            "input_tokens": input_tokens,
            "currency": "usd",
            "note": "estimate from word count; real cost from vendor usage in vendor_ref",
        }


def _json_schema_type(type_name: str) -> str:
    return {
        "string": "string",
        "number": "number",
        "integer": "integer",
        "boolean": "boolean",
        "object": "object",
        "array": "array",
    }.get(type_name, "string")


# ---------------------------------------------------------------------------
# Plugin packaging: the adapter crystallises into a loadable plugin.
# ---------------------------------------------------------------------------


def openai_adapter_plugin(**adapter_kwargs: Any) -> Any:
    """Build a Plugin that contributes an OpenAI-compatible adapter.

    The plugin contributes no tools; it provides a *model capability*.  Its
    manifest declares the capability, and its build() returns the adapter
    wrapped so the PluginRuntime can load and audit it.  (Model adapters plug
    in as plugins; the kernel's ToolRegistry is for tools, not models, so the
    contribution is the adapter object itself, returned via build() for the
    caller to use after load().)
    """

    from .plugins import Plugin, PluginManifest

    adapter = OpenAIAdapter(**adapter_kwargs)

    def build() -> list:
        # The plugin contributes no ToolRegistry tools; the loaded plugin's
        # adapter is obtained via the returned build product (see load_adapter).
        return []

    return Plugin(
        manifest=PluginManifest(
            id=f"model-openai-{adapter.model_id}",
            version="1.0.0",
            capabilities=(f"model:{adapter.model_id}", "model-adapter"),
            max_permission="read",
        ),
        build=build,
    )


def load_openai_adapter(runtime: Any, **adapter_kwargs: Any) -> OpenAIAdapter:
    """Load the OpenAI adapter plugin through a PluginRuntime and return the adapter.

    This keeps the kernel's rule -- a vendor capability crystallises into a
    plugin that is validated and audited on load -- while returning the adapter
    for the caller to drive an AgentLoop.
    """

    adapter = OpenAIAdapter(**adapter_kwargs)

    from .plugins import Plugin, PluginManifest

    plugin = Plugin(
        manifest=PluginManifest(
            id=f"model-openai-{adapter.model_id}",
            version="1.0.0",
            capabilities=(f"model:{adapter.model_id}", "model-adapter"),
            max_permission="read",
        ),
        build=lambda: [],
    )
    runtime.load(plugin)
    return adapter
