"""Router: an explicit, reasoned choice about what happens to the context.

The vision's first principle: a session faces a choice -- continue the current
context, or be reborn into a new one carrying key memory.  That choice belongs
to an explicit **router**, decided jointly by phase boundary, saturation,
model switch and the user's instruction.  The router:

- **outputs a decision with a reason, and never modifies long-term memory.**
  It reads projections (saturation, pending approvals, task state) and emits a
  :class:`RouteDecision`; it does not fork, compact or rewrite anything
  itself -- execution is the caller's job (e.g. the AgentLoop or the user).
- **is deterministic and explainable.**  Every decision carries the signals
  that produced it, so it can be replayed and audited, and the routing rules
  can become explainable over time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .store import HarnessStore

VALID_ROUTES = {"continue", "fork", "rebirth", "switch_recipe", "spawn_subagent"}


@dataclass(frozen=True)
class RouteDecision:
    """The router's output: a route plus the reason and evidence behind it."""

    route: str
    reason: str
    signals: dict[str, Any]
    suggested_recipe: str | None = None

    def __post_init__(self) -> None:
        if self.route not in VALID_ROUTES:
            raise ValueError(f"invalid route: {self.route}")
        if not self.reason.strip():
            raise ValueError("a route decision must carry a reason")

    def describe(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "reason": self.reason,
            "signals": self.signals,
            "suggested_recipe": self.suggested_recipe,
        }


@dataclass
class Router:
    """Decide how a session's context should proceed, with explicit reasons."""

    store: HarnessStore
    # Saturation is measured in work events in the current session.  Above
    # ``saturate_at`` the router recommends a rebirth (compact + new context);
    # above ``fork_at`` it recommends a fork.  These are heuristics a user can
    # override, recorded with every decision.
    saturate_at: int = 40
    fork_at: int = 80

    def decide(
        self,
        *,
        session_id: str,
        task_type: str | None = None,
        user_instruction: str | None = None,
        record: bool = True,
    ) -> RouteDecision:
        """Decide the route for a session and record it (with its reason).

        The user's explicit instruction always wins; otherwise the decision is
        driven by saturation and pending approvals.  The router reads
        projections only and never writes long-term memory.
        """

        signals = self._gather_signals(session_id)

        # 1. The user's explicit instruction is decisive.
        if user_instruction:
            route = self._route_from_instruction(user_instruction)
            decision = RouteDecision(
                route=route,
                reason=f"user instruction: {user_instruction}",
                signals=signals,
                suggested_recipe=self._suggest_recipe(task_type),
            )
        # 2. A pending approval blocks continuation: the session is waiting on
        #    a human, so forking/rebirthing now would lose that thread.
        elif signals["pending_approvals"] > 0:
            decision = RouteDecision(
                route="continue",
                reason="an approval is pending; the session is waiting on a human decision",
                signals=signals,
                suggested_recipe=self._suggest_recipe(task_type),
            )
        # 3. Saturation drives fork / rebirth.
        elif signals["work_events"] >= self.fork_at:
            decision = RouteDecision(
                route="fork",
                reason=(
                    f"context is saturated ({signals['work_events']} work events "
                    f">= fork threshold {self.fork_at}); fork to a child agent "
                    "carrying the current state"
                ),
                signals=signals,
                suggested_recipe=self._suggest_recipe(task_type),
            )
        elif signals["work_events"] >= self.saturate_at:
            decision = RouteDecision(
                route="rebirth",
                reason=(
                    f"context is filling up ({signals['work_events']} work events "
                    f">= saturation threshold {self.saturate_at}); assemble a handoff "
                    "package and be reborn into a fresh context"
                ),
                signals=signals,
                suggested_recipe=self._suggest_recipe(task_type),
            )
        else:
            decision = RouteDecision(
                route="continue",
                reason=f"context has room ({signals['work_events']} work events); continue",
                signals=signals,
                suggested_recipe=self._suggest_recipe(task_type),
            )

        if record:
            self.store.append_event(
                session_id,
                "route.selected",
                {
                    "route": decision.route,
                    "reason": decision.reason,
                    "task_type": task_type,
                    "user_instruction": user_instruction,
                    "signals": decision.signals,
                    "suggested_recipe": decision.suggested_recipe,
                    "thresholds": {"saturate_at": self.saturate_at, "fork_at": self.fork_at},
                },
            )
        return decision

    # ------------------------------------------------------------------
    # signals (read-only projections)
    # ------------------------------------------------------------------
    def _gather_signals(self, session_id: str) -> dict[str, Any]:
        work_events = len(self.store.list_work_events(session_id=session_id, limit=100000))
        pending_approvals = len(
            [
                e
                for e in self.store.list_events(session_id=session_id, limit=1000)
                if e.event_type == "tool.approval_required"
            ]
        )
        # Resolve the count of approvals already granted, so a "pending" one is
        # a request with no matching grant yet.
        granted = len(
            [
                e
                for e in self.store.list_events(session_id=session_id, limit=1000)
                if e.event_type in {"tool.approved", "tool.approval_granted"}
            ]
        )
        pending = max(0, pending_approvals - granted)
        return {
            "work_events": work_events,
            "pending_approvals": pending,
            "saturate_at": self.saturate_at,
            "fork_at": self.fork_at,
        }

    @staticmethod
    def _route_from_instruction(instruction: str) -> str:
        """Map an explicit instruction word to a route, conservatively."""

        text = instruction.strip().lower()
        if text in {"fork", "派生", "分支"}:
            return "fork"
        if text in {"rebirth", "重生", "compact", "压缩"}:
            return "rebirth"
        if text in {"switch", "switch_recipe", "切换配方", "换模型"}:
            return "switch_recipe"
        if text in {"subagent", "spawn", "子agent", "子代理"}:
            return "spawn_subagent"
        # An unrecognised instruction means "keep going" -- never guess a
        # destructive route from ambiguous input.
        return "continue"

    @staticmethod
    def _suggest_recipe(task_type: str | None) -> str | None:
        if task_type is None:
            return None
        try:
            from .recipes import resolve_recipe

            return resolve_recipe(task_type).id
        except ValueError:
            return None
