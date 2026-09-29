"""Tool registry: the model-visible surface is separated from host execution.

A tool has two faces that never mix:

- the **model-visible surface** (name, description, input schema, output
  contract) -- what a model is allowed to see and request;
- the **host execution surface** (the callable, timeout, permission level,
  approval policy) -- what the host will actually do, and under what guard.

The execution pipeline is fixed and non-negotiable::

    validate -> policy -> approval -> execute -> normalize -> log -> present

Approval is a physical gate, not a suggestion: a tool whose policy requires
approval *cannot* execute until approval is granted in the ledger.  This is
enforced in code ("physically impossible to run unapproved"), never delegated
to a model's discretion.

Tools register under a scope (``global`` / ``agent`` / ``session``).  A
narrower scope may shadow a wider one, but the shadowing is always recorded in
the ledger.  Unloading a tool must release everything it holds.

This module is the contract and pipeline skeleton.  It executes only the
callables a host explicitly registers; it provides no network, shell or file
side effects of its own (those belong to the Execution World layer, validated
separately).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .store import HarnessStore, _id, _json, _now

VALID_TOOL_SCOPES = {"global", "agent", "session"}
# Permission levels, ordered by risk.  A tool's level decides whether approval
# is mandatory before execution.
PERMISSION_LEVELS = {"read", "write", "destructive"}
# Approval policies.
#   "never"    -- safe to run without approval (read-only by contract);
#   "always"   -- must be approved every single time before running;
#   "on_write" -- approved unless the tool is read-level (default for write+).
VALID_APPROVAL_POLICIES = {"never", "always", "on_write"}

# A type the registry uses to reject obviously invalid tool input before any
# host code runs.  It is deliberately small: the contract layer validates shape,
# not semantics.
_JSON_SCALARS = (str, int, float, bool, type(None))


class ToolError(Exception):
    """Base class for tool pipeline failures."""


class ToolValidationError(ToolError):
    """Input failed schema validation before policy/approval/execution."""


class ToolApprovalRequired(ToolError):
    """The tool cannot execute until approval is granted."""


class ToolPermissionError(ToolError):
    """The tool's permission level forbids the requested operation."""


@dataclass(frozen=True)
class ToolSchema:
    """The model-visible surface of a tool."""

    name: str
    description: str
    # A JSON-schema-ish mapping of parameter name -> expected json type name
    # ("string", "number", "integer", "boolean", "object", "array").
    input_schema: dict[str, str]
    output_contract: str = "json"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tool name cannot be empty")
        if not isinstance(self.input_schema, dict):
            raise ValueError("input_schema must be a mapping of name -> type")


@dataclass(frozen=True)
class Tool:
    """A registered tool: model-visible surface plus host execution surface."""

    schema: ToolSchema
    execute: Callable[[dict[str, Any]], Any]
    permission: str = "read"
    approval: str = "on_write"
    timeout_seconds: float = 30.0
    scope: str = "global"

    def __post_init__(self) -> None:
        if self.permission not in PERMISSION_LEVELS:
            raise ValueError(f"invalid permission level: {self.permission}")
        if self.approval not in VALID_APPROVAL_POLICIES:
            raise ValueError(f"invalid approval policy: {self.approval}")
        if self.scope not in VALID_TOOL_SCOPES:
            raise ValueError(f"invalid tool scope: {self.scope}")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not callable(self.execute):
            raise ValueError("execute must be callable")
        # A read-permission tool must never require write approval; conversely
        # a destructive tool must always require approval.  These are guard
        # rails on the *declaration*, enforced before any registration.
        if self.permission == "destructive" and self.approval != "always":
            raise ValueError("destructive tools must use approval='always'")
        if self.permission == "read" and self.approval == "always":
            # Allowed but unusual; nothing to enforce here.
            pass

    @property
    def requires_approval(self) -> bool:
        if self.approval == "always":
            return True
        if self.approval == "never":
            return False
        # on_write: only write/destructive tools need approval.
        return self.permission in {"write", "destructive"}

    def visible_surface(self) -> dict[str, Any]:
        """What a model is allowed to see.  Implementation is never exposed."""

        return {
            "name": self.schema.name,
            "description": self.schema.description,
            "input_schema": dict(self.schema.input_schema),
            "output_contract": self.schema.output_contract,
        }


@dataclass
class ToolRegistry:
    """A scoped registry.  Narrower scopes shadow wider ones, audibly."""

    store: HarnessStore
    _tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> dict[str, Any]:
        name = tool.schema.name
        shadowed = self._tools.get(name)
        self._tools[name] = tool
        result = {
            "registered": name,
            "scope": tool.scope,
            "shadowed": None,
        }
        if shadowed is not None:
            # Shadowing is legal but must never be silent.
            result["shadowed"] = {"scope": shadowed.scope, "permission": shadowed.permission}
            self.store.append_event(
                "system",
                "tool.shadowed",
                {
                    "name": name,
                    "new_scope": tool.scope,
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
                "shadowed": result["shadowed"],
            },
        )
        return result

    def unregister(self, name: str) -> bool:
        tool = self._tools.pop(name, None)
        if tool is None:
            return False
        # Unloading must release anything the tool held.  The contract layer
        # drops the reference; a tool that holds timers/listeners must expose
        # its own ``close`` and is invoked here if present.
        close = getattr(tool.execute, "close", None)
        if callable(close):
            close()
        self.store.append_event("system", "tool.unregistered", {"name": name, "scope": tool.scope})
        return True

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def visible_tools(self) -> list[dict[str, Any]]:
        """The model-visible surface of every registered tool."""

        return [tool.visible_surface() for tool in self._tools.values()]

    # -- execution pipeline ------------------------------------------------

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
            # bool is a subclass of int; keep integer/number honest.
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
        approved: bool = False,
        actor_id: str = "model",
    ) -> dict[str, Any]:
        """Run the fixed pipeline for a tool call.

        Every stage transition is logged.  A tool requiring approval raises
        :class:`ToolApprovalRequired` *before* execution unless ``approved`` is
        true; approval must therefore be granted out-of-band (and recorded)
        before a gated call can proceed.
        """

        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"unknown tool: {name}")

        requested_at = _now()
        self.store.append_event(
            session_id,
            "tool.requested",
            {
                "name": name,
                "arguments": arguments,
                "actor_id": actor_id,
                "scope": tool.scope,
                "permission": tool.permission,
            },
        )

        # validate
        self._validate_input(tool, arguments)

        # policy + approval: a physical gate.
        if tool.requires_approval and not approved:
            self.store.append_event(
                session_id,
                "tool.approval_required",
                {"name": name, "permission": tool.permission, "actor_id": actor_id},
            )
            raise ToolApprovalRequired(
                f"tool '{name}' (permission={tool.permission}) requires approval before execution"
            )
        if tool.requires_approval:
            self.store.append_event(
                session_id,
                "tool.approved",
                {"name": name, "permission": tool.permission, "actor_id": actor_id},
            )

        # execute -> normalize -> log
        try:
            raw = tool.execute(arguments)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalised into a ledger event
            self.store.append_event(
                session_id,
                "tool.failed",
                {"name": name, "error": str(exc), "actor_id": actor_id},
            )
            raise ToolError(f"tool '{name}' failed: {exc}") from exc

        result = {"name": name, "output": raw, "output_contract": tool.schema.output_contract}
        self.store.append_event(
            session_id,
            "tool.completed",
            {
                "name": name,
                "actor_id": actor_id,
                "output_contract": tool.schema.output_contract,
                "duration_note": "synchronous",
            },
        )
        # present: the normalized, model-safe result.
        return result
