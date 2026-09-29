"""Shared, credential-safe HTTP plumbing for vendor model adapters.

Both the OpenAI and Anthropic adapters (and any future vendor) need exactly
the same hard-won guarantees: no redirect-following (which would forward
credentials to another host), HTTPS-only base URLs by default, error bodies
that never reach the ledger, and cause-based error classification.  Those live
here once, so the guarantees cannot drift between vendors.  Vendor adapters
supply only their *differences*: request building and response parsing.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import urlsplit

from .adapters import ModelAdapterError

# A transport maps (url, headers, body_bytes, timeout) -> (status, response_bytes).
Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]

# The only usage fields ever preserved in a vendor_ref (allowlist, coerced int).
_USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: following one would forward the Authorization /
    x-api-key credential to whatever host the redirect points at."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def secure_transport(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
    """A credential-safe POST transport (stdlib only).

    Redirects are refused so credentials only ever reach the configured
    endpoint.  Failures are classified by cause: a true timeout is retryable;
    DNS/connection/TLS failures are not.
    """

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise ModelAdapterError(
                "invalid_request",
                f"endpoint redirected ({exc.code}); redirects are refused to protect credentials",
            ) from exc
        return exc.code, exc.read()
    except (socket.timeout, TimeoutError) as exc:
        raise ModelAdapterError("timeout", f"request timed out after {timeout}s") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise ModelAdapterError("timeout", f"request timed out after {timeout}s") from exc
        raise ModelAdapterError(
            "overloaded", f"cannot reach the endpoint: {type(exc.reason).__name__}"
        ) from exc


def classify_transport_error(exc: Exception, timeout: float) -> ModelAdapterError:
    """Classify a transport-layer exception by cause (used for injected transports)."""

    cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(cause, (socket.timeout, TimeoutError)):
        return ModelAdapterError("timeout", f"request timed out after {timeout}s")
    if isinstance(exc, urllib.error.URLError):
        return ModelAdapterError("overloaded", f"cannot reach the endpoint: {type(cause).__name__}")
    return ModelAdapterError("unknown", f"transport failed: {type(exc).__name__}")


def validate_base_url(base: str, *, allow_insecure: bool) -> str:
    """Return a normalised base URL, enforcing HTTPS unless opted out.

    The base URL is a trust boundary: the credential is sent to whatever host
    it names.  Plaintext or non-HTTP(S) schemes are refused unless
    ``allow_insecure`` is set (e.g. a local model server).
    """

    scheme = urlsplit(base.strip().lower()).scheme
    allowed = {"https"} if not allow_insecure else {"https", "http"}
    if scheme not in allowed:
        raise ModelAdapterError(
            "auth",
            f"refusing base URL scheme {scheme!r} (would send credentials over a "
            "non-HTTPS channel); pass allow_insecure=True for a local plaintext endpoint",
        )
    return base.rstrip("/")


def safe_error_ref(status: int, raw: bytes) -> dict[str, Any]:
    """A vendor_ref for an HTTP error: a *reference*, never the body.

    An error body can contain anything -- including, for a 401, the credential
    itself -- and would be persisted verbatim into the append-only ledger.  Only
    the status and a short vendor error code are kept.
    """

    ref: dict[str, Any] = {"status": status}
    try:
        body = json.loads(raw.decode("utf-8"))
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            code = error.get("code") or error.get("type")
            if code:
                ref["error_code"] = str(code)[:100]
        elif isinstance(body, dict) and body.get("type"):
            ref["error_code"] = str(body["type"])[:100]
    except (ValueError, UnicodeDecodeError):
        pass
    return ref


def safe_usage_ref(usage: Any) -> dict[str, int]:
    """The allowlisted numeric usage fields from a vendor usage object."""

    if not isinstance(usage, dict):
        return {}
    return {key: usage[key] for key in _USAGE_FIELDS if isinstance(usage.get(key), int)}


def classify_http_status(status: int, raw: bytes) -> ModelAdapterError:
    """Map an HTTP status to a unified error class with a safe vendor_ref."""

    ref = safe_error_ref(status, raw)
    if status in {401, 403}:
        return ModelAdapterError("auth", f"vendor auth failed ({status})", vendor_ref=ref)
    if status == 429:
        return ModelAdapterError("rate_limit", "vendor rate limit (429)", vendor_ref=ref)
    if status in {500, 502, 503, 504}:
        return ModelAdapterError("overloaded", f"vendor overloaded ({status})", vendor_ref=ref)
    if status == 400:
        return ModelAdapterError("invalid_request", "vendor rejected request (400)", vendor_ref=ref)
    return ModelAdapterError("unknown", f"vendor error ({status})", vendor_ref=ref)
