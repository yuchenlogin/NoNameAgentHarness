"""Tool registry: the model-visible surface is separated from host execution.

A tool has two faces that never mix:

- the **model-visible surface** (name, description, input schema) -- what a
  model is allowed to see and request;
- the **host execution surface** (the callable, permission level, approval
  policy, scope) -- what the host will actually do, and under what guard.

The pipeline is::

    validate -> approval -> execute -> log -> return

**Approval is a physical gate backed by the ledger, not a caller-supplied
boolean.**  A gated tool cannot execute until a one-time approval token is
presented.  Tokens are minted by :meth:`ToolRegistry.grant_approval` -- the
*approver's* path, which writes ``tool.approval_granted`` -- and are bound to
``(tool name, canonical hash of arguments, approver)`` and single-use.  The
executor never marks its own homework: it verifies the token against the
in-memory grant set (itself derived from ledger events) and records
``tool.approved`` only as a *reference* to a prior grant.

Two further hard rules make the gate structural rather than advisory:

- **Monotonic shadowing**: a same-name registration may never weaken the
  permission or approval requirement of the tool it shadows, and a narrower
  scope may not be shadowed by a wider one.
- **No subclassing for registration**: only exact :class:`Tool` instances can
  be registered, so the approval gate cannot be overridden away.

This module is the contract and pipeline skeleton.  It executes only the
callables a host explicitly registers and provides no shell, network or file
side effects of its own (those belong to the Execution World layer, validated
separately).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from .store import HarnessStore

VALID_TOOL_SCOPES = {"global", "agent", "session"}
PERMISSION_LEVELS = {"read", "write", "destructive"}
# Ordered weakest -> strongest so shadowing monotonicity can be enforced.
_PERMISSION_RANK = {"read": 0, "write": 1, "destructive": 2}
VALID_APPROVAL_POLICIES = {"never", "always"}
_SCOPE_RANK = {"global": 0, "agent": 1, "session": 2}


class ToolError(Exception):
    """Base class for tool pipeline failures."""


class ToolValidationError(ToolError):
    """Input failed schema validation before approval/execution."""


class ToolApprovalRequired(ToolError):
    """The tool cannot execute until a valid approval token is presented."""


class ToolShadowingError(ToolError):
    """A registration tried to weaken or mis-scope an existing tool."""


@dataclass(frozen=True)
class ToolSchema:
    """The model-visible surface of a tool."""

    name: str
    description: str
    input_schema: dict[str, str]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tool name cannot be empty")
        if not isinstance(self.input_schema, dict):
            raise ValueError("input_schema must be a mapping of name -> type")


@dataclass(frozen=True)
class Tool:
    """A registered tool: model-visible surface plus host execution surface.

    ``approval`` is binary: ``never`` (read-only by contract, runs freely) or
    ``always`` (must be approved for every call).  ``permission`` describes the
    risk level; a destructive tool must always require approval.
    """

    schema: ToolSchema
    execute: Callable[[dict[str, Any]], Any]
    permission: str = "read"
    approval: str = "never"
    scope: str = "global"
    # Optional session binding for session-scoped tools.
    session_id: str | None = None

    def __post_init__(self) -> None:
        if self.permission not in PERMISSION_LEVELS:
            raise ValueError(f"invalid permission level: {self.permission}")
        if self.approval not in VALID_APPROVAL_POLICIES:
            raise ValueError(f"invalid approval policy: {self.approval}")
        if self.scope not in VALID_TOOL_SCOPES:
            raise ValueError(f"invalid tool scope: {self.scope}")
        if not callable(self.execute):
            raise ValueError("execute must be callable")
        if self.permission == "destructive" and self.approval != "always":
            raise ValueError("destructive tools must use approval='always'")
        if self.scope == "session" and not (self.session_id and self.session_id.strip()):
            raise ValueError("session-scoped tools must declare session_id")

    @property
    def requires_approval(self) -> bool:
        return self.approval == "always"

    def visible_surface(self) -> dict[str, Any]:
        """What a model is allowed to see.  Implementation is never exposed."""

        return {
            "name": self.schema.name,
            "description": self.schema.description,
            "input_schema": dict(self.schema.input_schema),
        }


def _canonical_arguments(arguments: dict[str, Any]) -> str:
    """A stable canonical form for binding approvals to exact arguments."""

    return json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _arguments_hash(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_arguments(arguments).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ApprovalToken:
    """A one-time, call-bound approval grant.  Verified, never self-asserted."""

    id: str
    tool_name: str
    arguments_hash: str
    approver_id: str
    granted_at: str


@dataclass
class ToolRegistry:
    """A scoped registry with a ledger-backed approval gate."""

    store: HarnessStore
    _tools: dict[str, Tool] = field(default_factory=dict)
    # Live (unconsumed) approval tokens, keyed by token id.
    _grants: dict[str, ApprovalToken] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Rebuild live grants from the ledger: a grant is durable evidence, so
        # an unconsumed token must survive a restart.  Grants recorded by
        # tool.approval_granted minus those consumed by tool.approved are live.
        consumed: set[str] = set()
        granted: dict[str, ApprovalToken] = {}
        for event in self.store.list_events(session_id=None, limit=100000):
            if event.event_type == "tool.approval_granted":
                payload = event.payload
                granted[payload["token_id"]] = ApprovalToken(
                    id=payload["token_id"],
                    tool_name=payload["name"],
                    arguments_hash=payload["arguments_hash"],
                    approver_id=payload["approver_id"],
                    granted_at=event.occurred_at,
                )
            elif event.event_type == "tool.approved":
                token_id = event.payload.get("approval_token_id") or event.payload.get("token_id")
                if token_id:
                    consumed.add(token_id)
        self._grants = {
            token_id: token for token_id, token in granted.items() if token_id not in consumed
        }

    # ------------------------------------------------------------------
    # registration
    # ------------------------------------------------------------------
    def register(self, tool: Tool) -> dict[str, Any]:
        # The gate must not be overridable: only exact Tool instances register.
        if type(tool) is not Tool:
            raise ToolError("only exact Tool instances can be registered")
        name = tool.schema.name
        shadowed = self._tools.get(name)
        if shadowed is not None:
            self._check_shadowing(shadowed, tool)
        self._tools[name] = tool
        result = {"registered": name, "scope": tool.scope, "shadowed": None}
        if shadowed is not None:
            result["shadowed"] = {"scope": shadowed.scope, "permission": shadowed.permission}
            self.store.append_event(
                "system",
                "tool.shadowed",
                {
                    "name": name,
                    "new_scope": tool.scope,
                    "new_permission": tool.permission,
                    "previous_scope": shadowed.scope,
                    "previous_permission": shadowed.permission,
                },
            )
        self.store.append_event(
            "system",
            "tool.registered",
            {
                "name": name,
                "scope": tool.scope,
                "permission": tool.permission,
                "approval": tool.approval,
                "session_id": tool.session_id,
                "shadowed": result["shadowed"],
            },
        )
        return result

    @staticmethod
    def _check_shadowing(old: Tool, new: Tool) -> None:
        """A shadowing registration may never weaken the gate or mis-scope.

        - scope monotonicity: a wider scope may not shadow a narrower one;
        - permission monotonicity: the new tool's risk may not be lower;
        - approval monotonicity: the new tool may not drop a required approval.
        """

        if _SCOPE_RANK[new.scope] < _SCOPE_RANK[old.scope]:
            raise ToolShadowingError(
                f"a {new.scope}-scoped tool cannot shadow a {old.scope}-scoped tool"
            )
        if _PERMISSION_RANK[new.permission] < _PERMISSION_RANK[old.permission]:
            raise ToolShadowingError(
                f"cannot shadow {old.permission} tool with lower-risk {new.permission} tool"
            )
        if old.requires_approval and not new.requires_approval:
            raise ToolShadowingError(
                "cannot shadow an approval-gated tool with one that needs no approval"
            )

    def unregister(self, name: str) -> bool:
        tool = self._tools.pop(name, None)
        if tool is None:
            return False
        self.store.append_event(
            "system", "tool.unregistered", {"name": name, "scope": tool.scope}
        )
        return True

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def visible_tools(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """The model-visible surface a given session is allowed to see.

        Session-scoped tools are visible only to their own session; wider
        scopes are visible to all.  This is what makes scope real rather than
        decorative.
        """

        visible = []
        for tool in self._tools.values():
            if tool.scope == "session" and tool.session_id != session_id:
                continue
            visible.append(tool.visible_surface())
        return visible

    # ------------------------------------------------------------------
    # approval (the approver's path)
    # ------------------------------------------------------------------
    def grant_approval(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        approver_id: str,
        session_id: str,
    ) -> ApprovalToken:
        """Mint a one-time approval token for an exact call.  Writes the grant.

        This is the only path that may authorise a gated call.  The grant is
        recorded in the ledger by the approver, never by the executor.
        """

        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"unknown tool: {name}")
        if not approver_id.strip():
            raise ValueError("approver_id cannot be empty")
        token = ApprovalToken(
            id=self._new_token_id(arguments),
            tool_name=name,
            arguments_hash=_arguments_hash(arguments),
            approver_id=approver_id,
            granted_at=self._now(),
        )
        self._grants[token.id] = token
        self.store.append_event(
            session_id,
            "tool.approval_granted",
            {
                "token_id": token.id,
                "name": name,
                "arguments_hash": token.arguments_hash,
                "approver_id": approver_id,
            },
        )
        return token

    # ------------------------------------------------------------------
    # execution pipeline
    # ------------------------------------------------------------------
    def _validate_input(self, tool: Tool, arguments: dict[str, Any]) -> None:
        if not isinstance(arguments, dict):
            raise ToolValidationError("tool arguments must be an object")
        schema = tool.schema.input_schema
        unknown = set(arguments) - set(schema)
        if unknown:
            raise ToolValidationError(f"unexpected tool arguments: {sorted(unknown)}")
        type_map = {
            "string": str,
            "number": (int, float),
            "integer": int,
            "boolean": bool,
            "object": dict,
            "array": list,
        }
        for param, expected in schema.items():
            if param not in arguments:
                raise ToolValidationError(f"missing required argument: {param}")
            value = arguments[param]
            py_type = type_map.get(expected)
            if py_type is None:
                raise ToolValidationError(f"unknown schema type for {param}: {expected}")
            if expected in {"integer", "number"} and isinstance(value, bool):
                raise ToolValidationError(f"argument {param} must be {expected}, got boolean")
            if not isinstance(value, py_type):
                raise ToolValidationError(
                    f"argument {param} must be {expected}, got {type(value).__name__}"
                )

    def request(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        session_id: str,
        approval_token: ApprovalToken | None = None,
        actor_id: str = "model",
    ) -> dict[str, Any]:
        """Run the pipeline for a tool call.  Every outcome is logged.

        A gated tool executes only when presented a valid, unconsumed approval
        token bound to this exact call's arguments.  There is no boolean to
        self-assert; the token must have been minted by ``grant_approval``.
        """

        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"unknown tool: {name}")
        # Scope enforcement: a session-scoped tool runs only for its session.
        if tool.scope == "session" and tool.session_id != session_id:
            raise ToolError(f"tool '{name}' is not available in this session")

        # Record the request with an arguments *hash*, not the raw content: a
        # not-yet-approved gated call must not persist model-controlled content
        # into the append-only ledger.
        self.store.append_event(
            session_id,
            "tool.requested",
            {
                "name": name,
                "arguments_hash": _arguments_hash(arguments),
                "actor_id": actor_id,
                "scope": tool.scope,
                "permission": tool.permission,
                "gated": tool.requires_approval,
            },
        )

        # validate
        try:
            self._validate_input(tool, arguments)
        except ToolValidationError as exc:
            self.store.append_event(
                session_id,
                "tool.validation_failed",
                {"name": name, "error": str(exc), "actor_id": actor_id},
            )
            raise

        # approval: verify a ledger-backed, call-bound, single-use token.
        if tool.requires_approval:
            token = self._verify_approval(tool, arguments, approval_token, session_id, actor_id)
        else:
            token = None

        # execute -> log
        import time

        started = time.monotonic()
        try:
            raw = tool.execute(arguments)
        except Exception as exc:  # noqa: BLE001 - every failure is logged
            self.store.append_event(
                session_id,
                "tool.failed",
                {"name": name, "error": str(exc), "actor_id": actor_id},
            )
            raise ToolError(f"tool '{name}' failed: {exc}") from exc
        elapsed_ms = int((time.monotonic() - started) * 1000)

        result = {"name": name, "output": raw}
        self.store.append_event(
            session_id,
            "tool.completed",
            {
                "name": name,
                "actor_id": actor_id,
                "elapsed_ms": elapsed_ms,
                "approval_token_id": token.id if token else None,
            },
        )
        return result

    def _verify_approval(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        token: ApprovalToken | None,
        session_id: str,
        actor_id: str,
    ) -> ApprovalToken:
        name = tool.schema.name
        if token is None:
            self.store.append_event(
                session_id,
                "tool.approval_required",
                {"name": name, "permission": tool.permission, "actor_id": actor_id},
            )
            raise ToolApprovalRequired(
                f"tool '{name}' (permission={tool.permission}) requires an approval token"
            )
        live = self._grants.get(token.id)
        arguments_hash = _arguments_hash(arguments)
        if (
            live is None
            or live.tool_name != name
            or live.arguments_hash != arguments_hash
        ):
            self.store.append_event(
                session_id,
                "tool.approval_rejected",
                {
                    "name": name,
                    "token_id": token.id,
                    "reason": "invalid, consumed, or argument-mismatched token",
                    "actor_id": actor_id,
                },
            )
            raise ToolApprovalRequired(
                f"approval token for '{name}' is invalid, consumed, or bound to different arguments"
            )
        # Single-use: consume the token so it cannot authorise a second call.
        del self._grants[token.id]
        self.store.append_event(
            session_id,
            "tool.approved",
            {
                "name": name,
                "token_id": token.id,
                "approver_id": live.approver_id,
                "actor_id": actor_id,
            },
        )
        return live

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _new_token_id(self, arguments: dict[str, Any]) -> str:
        """A unique, unguessable token id.

        It embeds the argument hash (so a token is self-describing) plus a
        random component, so ids never collide across restarts and cannot be
        predicted from the arguments alone.
        """

        import uuid

        digest = hashlib.sha256(_canonical_arguments(arguments).encode("utf-8")).hexdigest()[:12]
        return f"apr_{digest}_{uuid.uuid4().hex[:16]}"

    @staticmethod
    def _now() -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
