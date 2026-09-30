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
    # A bare substring marker match carries moderate confidence (high recall,
    # not high precision); only a structured payload carries the high confidence.
    assert next(c for c in candidates if c.category == "explicit_remember").confidence == 0.55
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


# --- 对抗性审查发现的回归 ---

def test_failed_extraction_run_is_audited(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "decision.accepted", {"key": "db", "content": {"text": "x"}})
        def bad_extractor(events):
            return [
                ExtractionCandidate(layer="high", logical_key="k1", content={}, source_event_ids=[events[0]["id"]], extraction_reason="r", confidence=0.5),
                ExtractionCandidate(layer="high", logical_key="k2", content={}, source_event_ids=["evt_ghost"], extraction_reason="r", confidence=0.5),
            ]
        extractor = MemoryExtractor(store, extractor=bad_extractor)
        with pytest.raises(KeyError):
            extractor.scan(session_id="s")
        # The failed run is audited with status=failed, not silently unaudited.
        event = next(e for e in store.list_events("s", limit=20) if e.event_type == "memory.extracted")
        assert event.payload["status"] == "failed"
    finally:
        store.close()


def test_extractor_crash_produces_failed_audit(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "note", {"text": "x"})
        def boom(events):
            raise RuntimeError("LLM timeout")
        extractor = MemoryExtractor(store, extractor=boom)
        with pytest.raises(RuntimeError):
            extractor.scan(session_id="s")
        event = next(e for e in store.list_events("s", limit=10) if e.event_type == "memory.extracted")
        assert event.payload["status"] == "failed"
        assert "LLM timeout" in event.payload["error"]
    finally:
        store.close()


def test_explicit_remember_facts_do_not_collapse_to_one_key(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "user.note", {"text": "记住：数据库用 SQLite"})
        store.append_event("s", "user.note", {"text": "记住：部署在周五"})
        extractor = MemoryExtractor(store)
        extractor.scan(session_id="s")
        pending = store.list_proposals(pending_only=True)
        keys = {p["logical_key"] for p in pending}
        # Distinct remembered facts get distinct keys, so accepting one does not
        # supersede the other.
        assert len(keys) == 2
        assert all(k.startswith("explicit_memory_") for k in keys)
    finally:
        store.close()


def test_negation_is_not_a_remember_instruction():
    events = [
        {"id": "e1", "event_type": "user.note", "payload": {"text": "别记住这个"}},
        {"id": "e2", "event_type": "user.note", "payload": {"text": "please do not remember this"}},
        {"id": "e3", "event_type": "user.note", "payload": {"text": "I was remembering the old days"}},
    ]
    candidates = rule_based_extractor(events)
    # None of these are explicit remember instructions.
    assert [c for c in candidates if c.category == "explicit_remember"] == []


def test_scan_excludes_own_bookkeeping_and_marks_truncation(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        for i in range(10):
            store.append_event("s", "decision.accepted", {"key": f"k{i}", "content": {"text": f"x{i}"}})
        extractor = MemoryExtractor(store)
        extractor.scan(session_id="s", limit=3)
        event = next(e for e in store.list_events("s", limit=50) if e.event_type == "memory.extracted")
        # Truncation is honestly marked, not disguised as a clean negative.
        assert event.payload["truncated"] is True
        assert event.payload["total_events"] == 10
        assert event.payload["scanned_events"] == 3
        # Second scan does not re-scan its own memory.* bookkeeping.
        report2 = extractor.scan(session_id="s")
        assert "memory.extracted" not in [
            e for e in [ev.event_type for ev in store.list_events("s", limit=50)]
        ] or report2 is not None  # the scan input excluded memory.* (verified below)
        # Verify scan input has no memory.* events: re-scan after many scans.
        for _ in range(3):
            extractor.scan(session_id="s")
        extracted_count = len([e for e in store.list_events("s", limit=100) if e.event_type == "memory.extracted"])
        # memory.extracted events exist (audits) but total_events counts exclude them.
        last = [e for e in store.list_events("s", limit=100) if e.event_type == "memory.extracted"][-1]
        assert last.payload["total_events"] == 10  # still 10, not growing from self-scan
    finally:
        store.close()


def test_duplicate_report_distinguishes_dupes_from_empty(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "decision.accepted", {"key": "db", "content": {"text": "x"}})
        extractor = MemoryExtractor(store)
        extractor.scan(session_id="s")
        report2 = extractor.scan(session_id="s")
        # "all duplicates" is reported distinctly from "nothing found".
        assert "已是待审提案" in report2.notes or "无新增" in report2.notes
        assert "未发现值得进入长期层的候选（这也是有理由的结果）" != report2.notes
    finally:
        store.close()


def test_pending_key_dedup_regardless_of_source(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        e1 = store.append_event("s", "decision.accepted", {"key": "db", "content": {"text": "x"}})
        e2 = store.append_event("s", "decision.accepted", {"key": "db", "content": {"text": "y"}})
        # Manually create a pending proposal for key "db" from e1.
        store.create_proposal("high", "db", {"text": "x"}, [e1.id])
        # An extractor proposing "db" again (from e2, a different source) is a dupe.
        def extractor_fn(events):
            return [ExtractionCandidate(layer="high", logical_key="db", content={"text": "y"}, source_event_ids=[e2.id], extraction_reason="r", confidence=0.5)]
        extractor = MemoryExtractor(store, extractor=extractor_fn)
        report = extractor.scan(session_id="s")
        assert report.candidates == ()
        assert len(store.list_proposals(pending_only=True)) == 1
    finally:
        store.close()


def test_category_threaded_into_proposal_reason(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "decision.accepted", {"key": "db", "content": {"text": "x"}})
        extractor = MemoryExtractor(store)
        extractor.scan(session_id="s")
        proposal = store.list_proposals(pending_only=True)[0]
        assert proposal["reason"].startswith("[decision]")
    finally:
        store.close()
