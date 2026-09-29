"""Agent loop: an explicit, recoverable state machine that only drives.

The loop is deliberately thin.  It does **not** route models, write memory or
judge permissions -- those belong to the services it already has
(``assemble_context_package``, ``resolve_recipe``, ``ToolRegistry``).  It only
drives a host-provided :class:`SessionDriver` through a fixed state machine and
records every transition as a session event, so a run can be replayed and
recovered from the event stream alone.

State machine (from docs/runtime-architecture.md)::

    IDLE
      -> ASSEMBLING_CONTEXT
      -> SELECTING_MODEL
      -> CALLING_MODEL
      -> WAITING_TOOL / STREAMING_OUTPUT
      -> APPLYING_RESULT
      -> CHECKING_STOP
      -> COMPACTING / COMPLETED / FAILED / CANCELLED

Stop conditions are explicit: task complete, user pause, round limit, budget
limit, unrecoverable error, waiting for approval.  Because no real model is
called in the prototype, ``CALLING_MODEL`` delegates to the injected driver;
a production driver would invoke a Model Adapter behind the same interface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .store import HarnessStore

# Explicit states.  Terminal states: COMPLETED / FAILED / CANCELLED.
STATES = {
    "IDLE",
    "ASSEMBLING_CONTEXT",
    "SELECTING_MODEL",
    "CALLING_MODEL",
    "WAITING_TOOL",
    "STREAMING_OUTPUT",
    "APPLYING_RESULT",
    "CHECKING_STOP",
    "COMPACTING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
}
TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED"}

# Explicit stop reasons.
STOP_REASONS = {
    "task_complete",
    "user_pause",
    "round_limit",
    "budget_limit",
    "unrecoverable_error",
    "waiting_approval",
}

# The legal transitions.  Anything else is a bug in the driver, not a choice.
_TRANSITIONS: dict[str, set[str]] = {
    "IDLE": {"ASSEMBLING_CONTEXT", "CANCELLED"},
    "ASSEMBLING_CONTEXT": {"SELECTING_MODEL", "FAILED", "CANCELLED"},
    "SELECTING_MODEL": {"CALLING_MODEL", "FAILED", "CANCELLED"},
    "CALLING_MODEL": {"WAITING_TOOL", "STREAMING_OUTPUT", "APPLYING_RESULT", "FAILED", "CANCELLED"},
    "WAITING_TOOL": {"APPLYING_RESULT", "FAILED", "CANCELLED"},
    "STREAMING_OUTPUT": {"APPLYING_RESULT", "FAILED", "CANCELLED"},
    "APPLYING_RESULT": {"CHECKING_STOP", "FAILED", "CANCELLED"},
    "CHECKING_STOP": {"CALLING_MODEL", "COMPACTING", "COMPLETED", "FAILED", "CANCELLED"},
    "COMPACTING": {"CALLING_MODEL", "COMPLETED", "FAILED", "CANCELLED"},
    "COMPLETED": set(),
    "FAILED": set(),
    "CANCELLED": set(),
}


class AgentLoopError(Exception):
    """Raised on illegal transitions or driver contract violations."""


@dataclass(frozen=True)
class LoopResult:
    """The outcome of a single driver turn.

    The driver reports what happened; the loop decides the next state.  A
    driver may request a tool call (``tool_call``), signal completion
    (``task_complete``), or ask to stop for a budget/round reason.
    """

    output: Any = None
    tool_call: dict[str, Any] | None = None
    task_complete: bool = False
    stop_reason: str | None = None


class SessionDriver(Protocol):
    """The host-provided behaviour the loop drives.

    Implementations must be deterministic given the same context for the
    prototype to stay reproducible.  ``act`` receives the assembled context
    package (and any tool result from the previous turn) and returns a
    :class:`LoopResult`.
    """

    def act(self, context: dict[str, Any], last_tool_result: Any = None) -> LoopResult:
        ...


@dataclass
class AgentLoop:
    """Drive a session through the state machine, logging every transition."""

    store: HarnessStore
    session_id: str
    driver: SessionDriver
    max_rounds: int = 10
    budget_rounds: int | None = None
    _state: str = field(default="IDLE", init=False)
    _rounds: int = field(default=0, init=False)

    @property
    def state(self) -> str:
        return self._state

    @property
    def rounds(self) -> int:
        return self._rounds

    def _transition(self, to_state: str, detail: dict[str, Any] | None = None) -> None:
        if to_state not in STATES:
            raise AgentLoopError(f"unknown state: {to_state}")
        allowed = _TRANSITIONS.get(self._state, set())
        if to_state not in allowed:
            raise AgentLoopError(f"illegal transition {self._state} -> {to_state}")
        payload = {"from": self._state, "to": to_state, "round": self._rounds}
        if detail:
            payload.update(detail)
        self.store.append_event(self.session_id, "loop.transition", payload)
        self._state = to_state

    def run(self, task: str, *, task_type: str | None = None) -> dict[str, Any]:
        """Drive the loop to a terminal state and return a run summary."""

        if self._state != "IDLE":
            raise AgentLoopError("run() may only be called from IDLE")
        context: dict[str, Any] = {}
        last_tool_result: Any = None
        try:
            self._transition("ASSEMBLING_CONTEXT")
            context = self.store.assemble_context_package(
                task, session_id=self.session_id, task_type=task_type
            )
            self._transition("SELECTING_MODEL")
            # Model selection is recorded by the package's recipe; the loop
            # does not choose a model itself.
            self._transition("CALLING_MODEL")

            while True:
                result = self.driver.act(context, last_tool_result)
                self._rounds += 1

                if result.tool_call is not None:
                    self._transition("WAITING_TOOL", {"tool": result.tool_call.get("name")})
                    last_tool_result = result.tool_call.get("result")
                    self._transition("APPLYING_RESULT")
                else:
                    self._transition("APPLYING_RESULT")

                self._transition("CHECKING_STOP")
                stop = self._check_stop(result)
                if stop is not None:
                    state, reason = stop
                    self._transition(state, {"reason": reason})
                    return self._summary(reason, context)
                # Otherwise loop back for another model turn.
                self._transition("CALLING_MODEL")
        except AgentLoopError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalised into FAILED
            if self._state not in TERMINAL_STATES:
                # Transition to FAILED from any non-terminal state is legal via
                # the machine only from specific states; force-record the error.
                self.store.append_event(
                    self.session_id,
                    "loop.error",
                    {"state": self._state, "error": str(exc), "round": self._rounds},
                )
                self._state = "FAILED"
            return self._summary("unrecoverable_error", context, error=str(exc))

    def _check_stop(self, result: LoopResult) -> tuple[str, str] | None:
        if result.stop_reason is not None:
            if result.stop_reason not in STOP_REASONS:
                raise AgentLoopError(f"unknown stop reason: {result.stop_reason}")
            if result.stop_reason == "task_complete" or result.task_complete:
                return ("COMPLETED", "task_complete")
            if result.stop_reason == "waiting_approval":
                return ("CANCELLED", "waiting_approval")
            if result.stop_reason == "user_pause":
                return ("CANCELLED", "user_pause")
            return ("FAILED", result.stop_reason)
        if result.task_complete:
            return ("COMPLETED", "task_complete")
        if self._rounds >= self.max_rounds:
            return ("FAILED", "round_limit")
        if self.budget_rounds is not None and self._rounds >= self.budget_rounds:
            return ("FAILED", "budget_limit")
        return None

    def _summary(
        self, reason: str, context: dict[str, Any], error: str | None = None
    ) -> dict[str, Any]:
        summary = {
            "session_id": self.session_id,
            "final_state": self._state,
            "stop_reason": reason,
            "rounds": self._rounds,
            "package_id": context.get("package_id"),
        }
        if error is not None:
            summary["error"] = error
        self.store.append_event(self.session_id, "loop.finished", summary)
        return summary

    # ------------------------------------------------------------------
    # recovery
    # ------------------------------------------------------------------
    @classmethod
    def reconstruct(
        cls,
        store: HarnessStore,
        session_id: str,
        driver: SessionDriver,
        **kwargs: Any,
    ) -> "AgentLoop":
        """Rebuild loop state from the event stream, not process memory.

        The current state and round count are derived purely from the recorded
        ``loop.transition`` events, so a recovered loop continues exactly where
        the event stream left off.
        """

        loop = cls(store=store, session_id=session_id, driver=driver, **kwargs)
        transitions = [
            e for e in store.list_events(session_id=session_id, limit=100000)
            if e.event_type == "loop.transition"
        ]
        # Events come newest-first from list_events; replay oldest-first.
        state = "IDLE"
        rounds = 0
        for event in reversed(transitions):
            state = event.payload.get("to", state)
            rounds = max(rounds, int(event.payload.get("round", 0)))
        loop._state = state
        loop._rounds = rounds
        return loop
