"""Memory extraction: from raw evidence to reviewable candidates, never facts.

The extractor is the first half of the two-stage pipeline (docs/memory-model
§4): *coverage extraction* (high recall) plus the candidate it emits must then
pass *long-term gating* (high precision) through the existing review gate.
Two rules are structural, not advisory:

- **The extractor never writes long-term state.**  It only produces candidates,
  each carrying a source event, an extraction_reason and a confidence -- the
  same contract the conservative structured-hint curator already honours.  A
  human (or an independent reviewer) approves; the extractor never confirms
  itself.
- **"No candidate" is also a reasoned result.**  An extraction run records what
  it scanned and why it found nothing (or something), so the ledger can answer
  "was anything worth remembering here?"

The extractor function is **injectable**: a real LLM extractor (driven through
a ModelAdapter) plugs in behind the same protocol, while the default is a
deterministic rule-based extractor that scans for the completeness-check
categories (§4.3) with conservative rules, so the pipeline is verifiable with
zero network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

from .store import HarnessStore

# An extractor maps a batch of events to extraction candidates.
ExtractorFn = Callable[[list[dict[str, Any]]], list["ExtractionCandidate"]]


@dataclass(frozen=True)
class ExtractionCandidate:
    """One reviewable memory candidate from an extraction run."""

    layer: str  # "high" | "mid"
    logical_key: str
    content: Any
    source_event_ids: list[str]
    extraction_reason: str
    confidence: float
    kind: str = "state"
    category: str = "observation"  # which §4.3 completeness category matched

    def __post_init__(self) -> None:
        if self.layer not in {"high", "mid"}:
            raise ValueError(f"invalid candidate layer: {self.layer}")
        if not self.logical_key.strip():
            raise ValueError("candidate logical_key cannot be empty")
        if not self.source_event_ids:
            raise ValueError("a candidate must cite at least one source event")
        if not self.extraction_reason.strip():
            raise ValueError("a candidate must carry an extraction_reason")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True)
class ExtractionReport:
    """The audited result of one extraction run (including "nothing found")."""

    scanned_events: int
    candidates: tuple[ExtractionCandidate, ...]
    notes: str

    def describe(self) -> dict[str, Any]:
        return {
            "scanned_events": self.scanned_events,
            "candidate_count": len(self.candidates),
            "notes": self.notes,
        }


class ExtractorProtocol(Protocol):
    def __call__(self, events: list[dict[str, Any]]) -> list[ExtractionCandidate]:
        ...


# ---------------------------------------------------------------------------
# Default deterministic rule-based extractor (coverage extraction, §4.3).
# ---------------------------------------------------------------------------

# Event types that map to completeness-check categories, with conservative
# rules.  These are deliberately high-recall but low-confidence: the review
# gate is what decides permanence.
_DECISION_TYPES = {"decision.accepted", "project.goal", "project.constraint", "canon.candidate"}
_TASK_TYPES = {"task.updated", "task.blocked", "task.progress", "phase.updated"}
_FAILURE_TYPES = {"test.failed", "tool.failed"}
_EXPLICIT_REMEMBER_MARKERS = ("记住", "remember", "记一下", "别忘了")


def rule_based_extractor(events: list[dict[str, Any]]) -> list[ExtractionCandidate]:
    """A deterministic, network-free coverage extractor (§4.3 categories).

    Scans events for the completeness-check categories and emits conservative
    candidates.  It is the deterministic stand-in for an LLM extractor: it
    proves the pipeline (candidate -> review gate) without any model.  The
    rules favour recall over confidence; nothing here is a fact until reviewed.
    """

    candidates: list[ExtractionCandidate] = []
    for event in events:
        event_type = event.get("event_type", "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        event_id = event.get("id") or event.get("event_id")
        if not event_id:
            continue

        # Category: explicit "remember this" instructions (highest priority).
        text = str(payload.get("text", "")) + str(payload.get("content", ""))
        if any(marker in text for marker in _EXPLICIT_REMEMBER_MARKERS):
            candidates.append(
                ExtractionCandidate(
                    layer="high",
                    logical_key=str(payload.get("key", "explicit_memory")),
                    content=payload.get("content", payload.get("text")),
                    source_event_ids=[event_id],
                    extraction_reason="用户明确要求记住这条内容",
                    confidence=0.95,
                    category="explicit_remember",
                )
            )
            continue

        # Category: project decisions & constraints (canon candidates).
        if event_type in _DECISION_TYPES and payload.get("key") and payload.get("content") is not None:
            candidates.append(
                ExtractionCandidate(
                    layer="high",
                    logical_key=str(payload["key"]),
                    content=payload["content"],
                    source_event_ids=[event_id],
                    extraction_reason=f"事件 {event_type} 记录了一条项目决策或约束",
                    confidence=0.7,
                    category="decision",
                )
            )

        # Category: unfinished tasks & blockers (task-state candidates).
        if event_type in _TASK_TYPES and payload.get("key") and payload.get("content") is not None:
            candidates.append(
                ExtractionCandidate(
                    layer="mid",
                    logical_key=str(payload["key"]),
                    content=payload["content"],
                    source_event_ids=[event_id],
                    extraction_reason=f"事件 {event_type} 记录了当前任务态/阻塞",
                    confidence=0.6,
                    category="task_state",
                )
            )

        # Category: tool/test failures with reusable lessons (episode candidates).
        if event_type in _FAILURE_TYPES and payload.get("message"):
            candidates.append(
                ExtractionCandidate(
                    layer="mid",
                    logical_key=f"failure_{payload.get('path', 'unknown')}",
                    content={"failure": payload.get("message"), "path": payload.get("path")},
                    source_event_ids=[event_id],
                    extraction_reason=f"事件 {event_type} 记录了一次失败，可能有可复用的教训",
                    confidence=0.4,
                    category="failure_lesson",
                )
            )
    return candidates


@dataclass
class MemoryExtractor:
    """Drive an extractor over session events into reviewable candidates.

    The extractor is never a gate: every candidate it produces still goes
    through ``store.create_proposal`` (which enforces provenance) and the human
    review gate.  The run itself is audited as a ``memory.extracted`` event,
    including when nothing was found.
    """

    store: HarnessStore
    extractor: ExtractorFn = rule_based_extractor

    def scan(
        self,
        *,
        session_id: str,
        limit: int = 200,
        create_proposals: bool = True,
        proposed_by: str = "extractor",
    ) -> ExtractionReport:
        """Run one extraction pass and audit it.

        Returns an :class:`ExtractionReport`; when ``create_proposals`` is true,
        each candidate is also turned into a durable *proposal* (candidate for
        review) -- never into active state.  Duplicates of an existing proposal
        for the same source event and key are skipped.
        """

        events = [
            {
                "id": event.id,
                "event_id": event.id,
                "event_type": event.event_type,
                "payload": event.payload,
                "occurred_at": event.occurred_at,
            }
            for event in self.store.list_events(session_id=session_id, limit=limit)
        ]
        raw_candidates = list(self.extractor(events))
        # Deduplicate against existing proposals for the same source+key.
        candidates: list[ExtractionCandidate] = []
        proposals_created = 0
        for candidate in raw_candidates:
            if self.store.proposal_exists_for_event(
                candidate.source_event_ids[0], logical_key=candidate.logical_key
            ):
                continue
            candidates.append(candidate)
            if create_proposals:
                self.store.create_proposal(
                    candidate.layer,
                    candidate.logical_key,
                    candidate.content,
                    candidate.source_event_ids,
                    kind=candidate.kind,
                    proposed_by=proposed_by,
                    confidence=candidate.confidence,
                    reason=candidate.extraction_reason,
                )
                proposals_created += 1

        notes = (
            f"扫描 {len(events)} 个事件，产出 {len(candidates)} 个候选"
            f"（新建 {proposals_created} 个审核提案）"
            if candidates
            else f"扫描 {len(events)} 个事件，未发现值得进入长期层的候选（这也是有理由的结果）"
        )
        report = ExtractionReport(
            scanned_events=len(events),
            candidates=tuple(candidates),
            notes=notes,
        )
        # Audit the run: coverage extraction is part of the ledger.
        self.store.append_event(
            session_id,
            "memory.extracted",
            {
                "scanned_events": report.scanned_events,
                "candidate_count": len(candidates),
                "proposals_created": proposals_created,
                "extractor": getattr(self.extractor, "__name__", "custom"),
                "notes": notes,
            },
        )
        return report
