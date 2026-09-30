"""A real embedding service, behind the same EmbeddingFn protocol.

This upgrades semantic recall from the deterministic local lexical embedding
to true semantic embeddings, while the *contract never changes*: the vector
index is still a rebuildable projection (never the source of truth), the
``EmbeddingFn`` signature is unchanged, and a real service plugs in exactly
where the local one did.

Credential safety is shared with the vendor adapters via
:mod:`noname_harness.vendor_http`: no redirects, HTTPS-only base URL, a
vendor_ref that is a *reference* (never the body, never credentials), and
cause-based error classification.  The API key is read from the environment
and used only in the request header -- it is never stored or logged.

The transport is injectable (a real ``urllib`` POST by default, a
deterministic replay transport in tests), so the contract is verified with no
network and no API key.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from .adapters import ModelAdapterError
from .vendor_http import (
    Transport,
    classify_http_status,
    classify_transport_error,
    safe_usage_ref,
    secure_transport,
    validate_base_url,
)

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_ENV_KEY = "OPENAI_API_KEY"
_ENV_BASE_URL = "OPENAI_BASE_URL"


@dataclass
class OpenAIEmbedding:
    """An OpenAI embeddings service behind the EmbeddingFn protocol.

    ``model_id`` doubles as the embedding-space identifier recorded in the
    vector projection, so a query with a different embedding function is
    refused instead of silently comparing across spaces.
    """

    model_id: str = "text-embedding-3-small"
    timeout: float = 30.0
    transport: Transport = secure_transport
    base_url: str | None = None
    api_key: str | None = None
    allow_insecure: bool = False

    def _endpoint(self) -> str:
        base = validate_base_url(
            self.base_url or os.environ.get(_ENV_BASE_URL) or _DEFAULT_BASE_URL,
            allow_insecure=self.allow_insecure,
        )
        return f"{base}/embeddings"

    def _headers(self) -> dict[str, str]:
        key = self.api_key or os.environ.get(_ENV_KEY)
        if not key:
            raise ModelAdapterError("auth", f"{_ENV_KEY} is not set")
        return {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}

    def __call__(self, text: str) -> list[float]:
        """Embed a single text into a float vector (the EmbeddingFn protocol)."""

        body = json.dumps({"model": self.model_id, "input": text}).encode("utf-8")
        try:
            status, raw = self.transport(self._endpoint(), self._headers(), body, self.timeout)
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
                f"unparseable embedding response: {exc}",
                vendor_ref={"status": 200, "bytes": len(raw)},
            ) from exc
        return self._map_vector(data)

    def _map_vector(self, data: dict[str, Any]) -> list[float]:
        if not isinstance(data, dict):
            raise ModelAdapterError("unknown", "embedding response is not an object")
        items = data.get("data")
        if not isinstance(items, list) or not items:
            raise ModelAdapterError(
                "unknown", "embedding response has no data", vendor_ref={"id": data.get("id")}
            )
        vector = items[0].get("embedding")
        if not isinstance(vector, list) or not all(isinstance(v, (int, float)) for v in vector):
            raise ModelAdapterError(
                "unknown", "embedding vector is malformed", vendor_ref={"id": data.get("id")}
            )
        return [float(v) for v in vector]


def load_openai_embedding(runtime: Any, **kwargs: Any) -> OpenAIEmbedding:
    """Load the embedding service through a PluginRuntime and return it.

    A vendor capability crystallises into a plugin: the manifest is validated
    and the load audited before the service is handed back.  The manifest
    honestly declares network-egress and billing side effects.
    """

    service = OpenAIEmbedding(**kwargs)

    from .plugins import Plugin, PluginManifest

    plugin = Plugin(
        manifest=PluginManifest(
            id=f"embedding-openai-{service.model_id}",
            version="1.0.0",
            capabilities=(f"embedding:{service.model_id}", "embedding-service"),
            max_permission="read",
            side_effects=("network-egress", "billing"),
        ),
        build=lambda: [],
    )
    runtime.load(plugin)
    return service
