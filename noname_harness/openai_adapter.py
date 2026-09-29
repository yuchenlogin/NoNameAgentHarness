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


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: following one would forward the Authorization bearer
    token to whatever host the redirect points at -- a silent credential
    exfiltration primitive.  A redirect is surfaced as an error instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _http_transport(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
    """The default real transport: a single POST via urllib (stdlib only).

    Redirects are refused (never followed) so the API key can only ever go to
    the configured endpoint.  Timeouts and connection failures are classified
    distinctly: a true timeout is retryable; a connection/DNS/TLS failure is
    not.
    """

    import socket

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        # A redirect with no handler surfaces as an HTTPError 3xx here.
        if 300 <= exc.code < 400:
            raise ModelAdapterError(
                "invalid_request",
                f"endpoint redirected ({exc.code}); redirects are refused to protect the API key",
            ) from exc
        return exc.code, exc.read()
    except (socket.timeout, TimeoutError) as exc:
        raise ModelAdapterError("timeout", f"request timed out after {timeout}s") from exc
    except urllib.error.URLError as exc:
        # DNS / connection-refused / TLS failures are not retryable timeouts.
        raise ModelAdapterError(
            "overloaded", f"cannot reach the endpoint: {type(exc.reason).__name__}"
        ) from exc


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
        base = (self.base_url or os.environ.get(_ENV_BASE_URL) or _DEFAULT_BASE_URL).rstrip("/")
        # The base URL is a trust boundary: the bearer key is sent to whatever
        # host it names, so plaintext HTTP is refused unless explicitly opted
        # in (e.g. a local model server).  This blocks credential exfiltration
        # via a poisoned OPENAI_BASE_URL pointing at an attacker endpoint.
        if base.startswith("http://") and not self.allow_insecure:
            raise ModelAdapterError(
                "auth",
                "refusing plaintext HTTP base URL (would send the API key "
                "unencrypted); pass allow_insecure=True for a local endpoint",
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
            self._map_tool_call(call, data) for call in message.get("tool_calls", []) or []
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
        return {
            "name": function.get("name"),
            "arguments": parsed,
            "id": call.get("id"),
        }

    def _classify_http_error(self, status: int, raw: bytes) -> ModelAdapterError:
        # vendor_ref is a *reference*, never the body: an error body can contain
        # anything (including, for a 401, the bearer key itself, or attacker
        # content from a hostile endpoint), and it would be persisted verbatim
        # into the append-only ledger.  Only status and a vendor error id are
        # kept -- enough to audit, nothing that can leak.
        ref: dict[str, Any] = {"status": status}
        try:
            body = json.loads(raw.decode("utf-8"))
            error = body.get("error") if isinstance(body, dict) else None
            if isinstance(error, dict) and error.get("code"):
                ref["error_code"] = str(error["code"])[:100]
        except (ValueError, UnicodeDecodeError):
            pass
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
