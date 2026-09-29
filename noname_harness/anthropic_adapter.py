"""An Anthropic Messages API adapter, packaged as a capability plugin.

A second vendor behind the same :class:`~noname_harness.adapters.ModelAdapter`
protocol -- the proof that the contract is vendor-neutral.  Business logic
drives it through the same ``AdapterDriver`` and AgentLoop with zero changes;
only the request/response mapping differs.  Credential safety (no redirects,
HTTPS-only, safe vendor_ref, cause-based errors) is shared via
:mod:`noname_harness.vendor_http`, so it cannot drift from the OpenAI adapter.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Iterator

from .adapters import (
    ModelAdapterError,
    ModelRequest,
    ModelResponse,
    StreamEvent,
)
from .models import ModelCapability
from .vendor_http import (
    Transport,
    classify_http_status,
    classify_transport_error,
    safe_usage_ref,
    secure_transport,
    validate_base_url,
)

_DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
_ENV_KEY = "ANTHROPIC_API_KEY"
_ENV_BASE_URL = "ANTHROPIC_BASE_URL"
_API_VERSION = "2023-06-01"


@dataclass
class AnthropicAdapter:
    """An Anthropic Messages API adapter."""

    model_id: str = "claude-sonnet-4-5"
    context_window: int | None = 200_000
    timeout: float = 60.0
    transport: Transport = secure_transport
    base_url: str | None = None
    api_key: str | None = None
    allow_insecure: bool = False
    max_output_tokens: int = 4096

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

    def _endpoint(self) -> str:
        base = validate_base_url(
            self.base_url or os.environ.get(_ENV_BASE_URL) or _DEFAULT_BASE_URL,
            allow_insecure=self.allow_insecure,
        )
        return f"{base}/messages"

    def _headers(self) -> dict[str, str]:
        key = self.api_key or os.environ.get(_ENV_KEY)
        if not key:
            raise ModelAdapterError("auth", f"{_ENV_KEY} is not set")
        return {
            "Content-Type": "application/json",
            "x-api-key": key,
            "anthropic-version": _API_VERSION,
        }

    def _build_body(self, request: ModelRequest) -> bytes:
        # Anthropic: system is a top-level field, not a message; content is a
        # list of blocks; tools have an input_schema object.
        system_parts = [m.content for m in request.messages if m.role == "system"]
        messages = []
        for message in request.messages:
            if message.role == "system":
                continue
            role = "user" if message.role in {"user", "tool"} else "assistant"
            messages.append({"role": role, "content": message.content})
        payload: dict[str, Any] = {
            "model": self.model_id,
            "messages": messages,
            "max_tokens": request.max_output_tokens or self.max_output_tokens,
        }
        if system_parts:
            payload["system"] = "\n".join(system_parts)
        if request.tools:
            payload["tools"] = [
                {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            name: {"type": _json_schema_type(t)}
                            for name, t in tool.get("input_schema", {}).items()
                        },
                        "required": list(tool.get("input_schema", {}).keys()),
                    },
                }
                for tool in request.tools
            ]
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
            raise classify_http_status(status, raw)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ModelAdapterError(
                "unknown",
                f"unparseable vendor response: {exc}",
                vendor_ref={"status": 200, "bytes": len(raw)},
            ) from exc
        return self._map_response(data, raw)

    def _map_response(self, data: dict[str, Any], raw: bytes) -> ModelResponse:
        # Anthropic returns content as a list of blocks (text + tool_use).
        blocks = data.get("content") or []
        if not isinstance(blocks, list):
            raise ModelAdapterError(
                "unknown", "vendor returned malformed content blocks", vendor_ref={"id": data.get("id")}
            )
        text_parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
        tool_calls = tuple(
            {
                "name": b.get("name"),
                "arguments": b.get("input") if isinstance(b.get("input"), dict) else {},
                "id": b.get("id"),
            }
            for b in blocks
            if b.get("type") == "tool_use"
        )
        usage = data.get("usage", {})
        return ModelResponse(
            text="\n".join(part for part in text_parts if part),
            tool_calls=tool_calls,
            model_id=data.get("model", self.model_id),
            finish_reason=data.get("stop_reason") or "stop",
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            vendor_ref={
                "status": 200,
                "id": data.get("id"),
                "usage": safe_usage_ref(
                    {
                        "prompt_tokens": usage.get("input_tokens"),
                        "completion_tokens": usage.get("output_tokens"),
                    }
                ),
            },
        )

    def stream(self, request: ModelRequest) -> Iterator[StreamEvent]:
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


def load_anthropic_adapter(runtime: Any, **adapter_kwargs: Any) -> AnthropicAdapter:
    """Load the Anthropic adapter through a PluginRuntime and return it."""

    adapter = AnthropicAdapter(**adapter_kwargs)

    from .plugins import Plugin, PluginManifest

    plugin = Plugin(
        manifest=PluginManifest(
            id=f"model-anthropic-{adapter.model_id}",
            version="1.0.0",
            capabilities=(f"model:{adapter.model_id}", "model-adapter"),
            max_permission="read",
            side_effects=("network-egress", "billing"),
        ),
        build=lambda: [],
    )
    runtime.load(plugin)
    return adapter
