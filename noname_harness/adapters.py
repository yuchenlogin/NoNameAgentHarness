"""Model adapters: vendor differences converged into a stable capability.

Business logic chooses models by **capability**, never by a provider's API
field.  An adapter maps a vendor's messages, tool calls, streaming, errors and
cost onto one contract, and preserves a reference to the vendor's raw response
so every call is auditable.

This module defines the contract and a deterministic, network-free adapter
used to verify it.  Real vendor adapters (OpenAI, Anthropic, local models)
plug in behind the same protocol -- as *plugins*, which is exactly the
vision's "capabilities crystallise into plugins" bet.  The kernel never
depends on a specific vendor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol

from .models import ModelCapability

# Unified error classification.  ``retryable`` tells the caller whether
# retrying the same request is sensible; it is a property of the error class,
# not of a vendor's status code.
ERROR_CLASSES = {
    "rate_limit": True,
    "timeout": True,
    "overloaded": True,
    "auth": False,
    "invalid_request": False,
    "cancelled": False,
    "unknown": False,
}


class ModelAdapterError(Exception):
    """A vendor-normalised model error with a stable classification."""

    def __init__(self, error_class: str, message: str, *, vendor_ref: Any = None):
        if error_class not in ERROR_CLASSES:
            raise ValueError(f"invalid error class: {error_class}")
        super().__init__(message)
        self.error_class = error_class
        self.retryable = ERROR_CLASSES[error_class]
        self.vendor_ref = vendor_ref


@dataclass(frozen=True)
class ModelMessage:
    """A single message in a vendor-neutral conversation."""

    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    name: str | None = None

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant", "tool"}:
            raise ValueError(f"invalid message role: {self.role}")


@dataclass(frozen=True)
class ModelRequest:
    """A vendor-neutral completion request."""

    messages: tuple[ModelMessage, ...]
    tools: tuple[dict[str, Any], ...] = ()
    max_output_tokens: int | None = None
    temperature: float | None = None

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("a request must have at least one message")


@dataclass(frozen=True)
class ModelResponse:
    """A vendor-neutral completion response.

    ``vendor_ref`` preserves a reference to the raw vendor response (or the
    response itself for small payloads) so the call remains auditable without
    the kernel depending on the vendor's shape.
    """

    text: str
    tool_calls: tuple[dict[str, Any], ...] = ()
    model_id: str = ""
    finish_reason: str = "stop"
    input_tokens: int | None = None
    output_tokens: int | None = None
    vendor_ref: Any = None


@dataclass(frozen=True)
class StreamEvent:
    """One chunk of a streaming response."""

    kind: str  # "text_delta" | "tool_call" | "completed" | "failed"
    text: str = ""
    payload: Any = None


class ModelAdapter(Protocol):
    """The stable capability every vendor adapter must provide."""

    def id(self) -> str:
        ...

    def capability(self) -> ModelCapability:
        ...

    def complete(self, request: ModelRequest) -> ModelResponse:
        ...

    def stream(self, request: ModelRequest) -> Iterator[StreamEvent]:
        ...

    def estimate_cost(self, request: ModelRequest) -> dict[str, Any]:
        ...


# ---------------------------------------------------------------------------
# A deterministic, network-free adapter for verifying the contract and for
# driving the AgentLoop without any external dependency.
# ---------------------------------------------------------------------------


@dataclass
class LocalEchoAdapter:
    """A deterministic adapter that answers from a scripted rule.

    It is *not* a mock of a vendor; it is the reference implementation of the
    contract: it honours capability reporting, error classification, tool-call
    structuring and vendor_ref preservation, all without network access.  The
    ``responder`` maps a request to either text or a tool call, so tests and
    the AgentLoop can exercise the full pipeline deterministically.
    """

    model_id: str = "local-echo"
    context_window: int | None = 32_000
    # responder(request) -> str | {"tool_call": {...}}
    responder: Any = None

    def id(self) -> str:
        return self.model_id

    def capability(self) -> ModelCapability:
        return ModelCapability(
            reasoning=True,
            vision=False,
            tool_calling=True,
            streaming=True,
            context_window=self.context_window,
        )

    def complete(self, request: ModelRequest) -> ModelResponse:
        reply = self._respond(request)
        if isinstance(reply, dict) and "tool_call" in reply:
            return ModelResponse(
                text="",
                tool_calls=(reply["tool_call"],),
                model_id=self.model_id,
                finish_reason="tool_calls",
                input_tokens=self._count_tokens(request),
                output_tokens=0,
                vendor_ref={"adapter": "local-echo", "reply": reply},
            )
        text = str(reply)
        return ModelResponse(
            text=text,
            model_id=self.model_id,
            finish_reason="stop",
            input_tokens=self._count_tokens(request),
            output_tokens=len(text.split()),
            vendor_ref={"adapter": "local-echo", "reply": text},
        )

    def stream(self, request: ModelRequest) -> Iterator[StreamEvent]:
        response = self.complete(request)
        if response.tool_calls:
            yield StreamEvent(kind="tool_call", payload=response.tool_calls[0])
        else:
            for word in response.text.split(" "):
                yield StreamEvent(kind="text_delta", text=word + " ")
        yield StreamEvent(kind="completed", payload=response)

    def estimate_cost(self, request: ModelRequest) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "input_tokens": self._count_tokens(request),
            "currency": "none",
            "note": "deterministic local adapter; no real cost",
        }

    def _respond(self, request: ModelRequest) -> Any:
        if self.responder is not None:
            return self.responder(request)
        # Default: echo the last user message back, deterministically.
        last_user = next(
            (m for m in reversed(request.messages) if m.role == "user"), None
        )
        return f"echo: {last_user.content if last_user else ''}"

    @staticmethod
    def _count_tokens(request: ModelRequest) -> int:
        # A coarse, deterministic token estimate (words), not a vendor count.
        return sum(len(message.content.split()) for message in request.messages)


# ---------------------------------------------------------------------------
# Bridging an adapter into the AgentLoop's SessionDriver protocol.
# ---------------------------------------------------------------------------


class AdapterDriver:
    """Drive an AgentLoop from a ModelAdapter.

    The driver's job is to translate between the loop's turn-taking and the
    adapter's request/response: it builds a ModelRequest from the assembled
    context package, calls the adapter, and maps the response to a
    :class:`LoopResult` (completing, or requesting a tool call that the loop
    routes through the approval gate).  It never decides routing, memory or
    permissions itself.
    """

    def __init__(self, adapter: ModelAdapter, *, system_prompt: str | None = None):
        self.adapter = adapter
        self.system_prompt = system_prompt

    def act(self, context: dict[str, Any], last_tool_result: Any = None) -> Any:
        from .agent_loop import LoopResult

        request = self._build_request(context, last_tool_result)
        response = self.adapter.complete(request)
        if response.tool_calls:
            call = response.tool_calls[0]
            return LoopResult(
                tool_call={
                    "name": call.get("name"),
                    "arguments": call.get("arguments", {}),
                    "approval_token": call.get("approval_token"),
                }
            )
        return LoopResult(output=response.text, task_complete=True)

    def _build_request(
        self, context: dict[str, Any], last_tool_result: Any
    ) -> ModelRequest:
        messages: list[ModelMessage] = []
        if self.system_prompt:
            messages.append(ModelMessage(role="system", content=self.system_prompt))
        # The assembled package is the user-visible brief for the model.
        import json

        brief = {
            "task": context.get("task"),
            "high": context.get("layers", {}).get("high"),
            "mid": context.get("layers", {}).get("mid"),
            "preference": context.get("preference"),
        }
        messages.append(
            ModelMessage(role="user", content=json.dumps(brief, ensure_ascii=False))
        )
        if last_tool_result is not None:
            messages.append(
                ModelMessage(
                    role="tool",
                    content=json.dumps(last_tool_result, ensure_ascii=False, default=str),
                    name="last_tool_result",
                )
            )
        # Surface the model-visible tool contracts (never the implementations).
        tools = tuple(
            context.get("visible_tools", ())
        )
        return ModelRequest(messages=tuple(messages), tools=tools)
