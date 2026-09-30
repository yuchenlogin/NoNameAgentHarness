"""Memory extraction: candidates, never facts; audited runs; injectable."""

from __future__ import annotations

import pytest

from noname_harness.extractor import (
    ExtractionCandidate,
    MemoryExtractor,
    rule_based_extractor,
)
from noname_harness.store import HarnessStore


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "extract project")
    return store, root


def test_candidate_validation():
    with pytest.raises(ValueError):
        ExtractionCandidate(layer="low", logical_key="k", content={}, source_event_ids=["e"], extraction_reason="r", confidence=0.5)
    with pytest.raises(ValueError):
        ExtractionCandidate(layer="high", logical_key="  ", content={}, source_event_ids=["e"], extraction_reason="r", confidence=0.5)
    with pytest.raises(ValueError):
        ExtractionCandidate(layer="high", logical_key="k", content={}, source_event_ids=[], extraction_reason="r", confidence=0.5)
    with pytest.raises(ValueError):
        ExtractionCandidate(layer="high", logical_key="k", content={}, source_event_ids=["e"], extraction_reason="  ", confidence=0.5)
    with pytest.raises(ValueError):
        ExtractionCandidate(layer="high", logical_key="k", content={}, source_event_ids=["e"], extraction_reason="r", confidence=1.5)


def test_rule_extractor_covers_completeness_categories():
    events = [
        {"id": "e1", "event_type": "user.note", "payload": {"text": "记住：这个项目用 SQLite"}},
        {"id": "e2", "event_type": "decision.accepted", "payload": {"key": "db", "content": {"text": "用 SQLite"}}},
        {"id": "e3", "event_type": "task.blocked", "payload": {"key": "current_task", "content": {"goal": "x", "next": "y"}}},
        {"id": "e4", "event_type": "test.failed", "payload": {"path": "t.py", "message": "boom"}},
        {"id": "e5", "event_type": "file.read", "payload": {"path": "a.py"}},  # not a category
    ]
    candidates = rule_based_extractor(events)
    categories = {c.category for c in candidates}
    assert "explicit_remember" in categories
    assert "decision" in categories
    assert "task_state" in categories
    assert "failure_lesson" in categories
    # Explicit-remember has highest confidence; failure is low confidence.
    assert next(c for c in candidates if c.category == "explicit_remember").confidence >= 0.9
    assert next(c for c in candidates if c.category == "failure_lesson").confidence < 0.5
    # Every candidate cites a source event and a reason.
    for c in candidates:
        assert c.source_event_ids and c.extraction_reason


def test_extraction_creates_proposals_not_active_state(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "decision.accepted", {"key": "db", "content": {"text": "用 SQLite"}})
        extractor = MemoryExtractor(store)
        report = extractor.scan(session_id="s")
        assert len(report.candidates) == 1
        # A proposal was created (candidate for review), but NO active state.
        assert store.active_state("high") == []
        pending = store.list_proposals(pending_only=True)
        assert len(pending) == 1
        assert pending[0]["proposed_by"] == "extractor"
        # The extraction run itself is audited.
        assert any(e.event_type == "memory.extracted" for e in store.list_events("s", limit=10))
    finally:
        store.close()


def test_extraction_deduplicates_against_existing_proposals(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "decision.accepted", {"key": "db", "content": {"text": "用 SQLite"}})
        extractor = MemoryExtractor(store)
        extractor.scan(session_id="s")
        # Second scan must not duplicate the proposal for the same source+key.
        report2 = extractor.scan(session_id="s")
        assert report2.candidates == ()
        assert len(store.list_proposals(pending_only=True)) == 1
    finally:
        store.close()


def test_no_candidate_is_a_reasoned_audited_result(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "file.read", {"path": "a.py"})  # no category matches
        extractor = MemoryExtractor(store)
        report = extractor.scan(session_id="s")
        assert report.candidates == ()
        assert "未发现" in report.notes
        event = next(e for e in store.list_events("s", limit=10) if e.event_type == "memory.extracted")
        assert event.payload["candidate_count"] == 0
    finally:
        store.close()


def test_extractor_is_injectable(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "user.note", {"text": "anything"})
        def llm_extractor(events):
            # A real LLM extractor plugs in behind the same protocol.
            return [
                ExtractionCandidate(
                    layer="high",
                    logical_key="llm_found",
                    content={"text": "LLM 提炼的法典"},
                    source_event_ids=[events[0]["id"]],
                    extraction_reason="LLM 判断这是长期约束",
                    confidence=0.8,
                    category="llm",
                )
            ]
        extractor = MemoryExtractor(store, extractor=llm_extractor)
        report = extractor.scan(session_id="s")
        assert report.candidates[0].logical_key == "llm_found"
        assert store.list_proposals(pending_only=True)[0]["logical_key"] == "llm_found"
    finally:
        store.close()


def test_extraction_respects_review_gate_end_to_end(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "decision.accepted", {"key": "db", "content": {"text": "用 SQLite"}})
        extractor = MemoryExtractor(store)
        extractor.scan(session_id="s")
        # The candidate only becomes canon after a HUMAN review, not by extraction.
        proposal = store.list_proposals(pending_only=True)[0]
        store.review_proposal(proposal["id"], "accept", "user")
        assert store.active_state("high")[0]["content"] == {"text": "用 SQLite"}
    finally:
        store.close()
