"""LLM extractor: conservative contract via ModelAdapter, fail-closed parsing."""

from __future__ import annotations

import json

import pytest

from noname_harness.adapters import LocalEchoAdapter
from noname_harness.extractor import MemoryExtractor
from noname_harness.llm_extractor import LLMExtractor, make_llm_extractor
from noname_harness.openai_adapter import OpenAIAdapter
from noname_harness.store import HarnessStore


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "llm extract project")
    return store, root


def _events(store, session="s"):
    store.append_event(session, "decision.accepted", {"key": "db", "content": {"text": "用 SQLite"}})
    store.append_event(session, "task.blocked", {"key": "current_task", "content": {"goal": "x", "next": "y"}})
    return [
        {"id": e.id, "event_id": e.id, "event_type": e.event_type, "payload": e.payload}
        for e in store.list_events(session, limit=50)
    ]


def test_llm_extractor_produces_candidates_via_adapter(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        events = _events(store)
        valid_id = events[0]["id"]
        response_json = json.dumps([
            {"layer": "high", "key": "db", "content": {"text": "用 SQLite"},
             "source_event_id": valid_id, "reason": "已确认的技术决策", "confidence": 0.85,
             "category": "decision"},
        ])
        adapter = LocalEchoAdapter(responder=lambda req: response_json)
        extractor = LLMExtractor(adapter)
        candidates = extractor(events)
        assert len(candidates) == 1
        assert candidates[0].logical_key == "db"
        assert candidates[0].category == "decision"
        assert candidates[0].source_event_ids == [valid_id]
    finally:
        store.close()


def test_hallucinated_source_event_is_discarded(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        events = _events(store)
        response_json = json.dumps([
            {"layer": "high", "key": "db", "content": {},
             "source_event_id": "evt_does_not_exist", "reason": "x", "confidence": 0.9,
             "category": "decision"},
        ])
        extractor = LLMExtractor(LocalEchoAdapter(responder=lambda req: response_json))
        # A hallucinated citation is discarded, never trusted.
        assert extractor(events) == []
    finally:
        store.close()


def test_malformed_response_fails_closed(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        events = _events(store)
        for bad in ["not json at all", '{"not": "a list"}', "[1, 2, 3]", "", "```json\n{bad}\n```"]:
            extractor = LLMExtractor(LocalEchoAdapter(responder=lambda req: bad))
            assert extractor(events) == [], f"should fail closed on: {bad!r}"
    finally:
        store.close()


def test_code_fence_is_stripped(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        events = _events(store)
        valid_id = events[0]["id"]
        response_json = '```json\n' + json.dumps([
            {"layer": "mid", "key": "current_task", "content": {"goal": "x"},
             "source_event_id": valid_id, "reason": "当前任务态", "confidence": 0.6,
             "category": "task_state"},
        ]) + '\n```'
        extractor = LLMExtractor(LocalEchoAdapter(responder=lambda req: response_json))
        candidates = extractor(events)
        assert len(candidates) == 1
    finally:
        store.close()


def test_invalid_fields_fail_closed(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        events = _events(store)
        valid_id = events[0]["id"]
        cases = [
            {"layer": "low", "key": "k", "content": {}, "source_event_id": valid_id, "reason": "r", "confidence": 0.5},  # bad layer
            {"layer": "high", "key": "k", "content": {}, "source_event_id": valid_id, "reason": "r", "confidence": "high"},  # bad confidence
            {"layer": "high", "key": "k", "content": {}, "source_event_id": valid_id, "reason": "r", "confidence": 5.0},  # confidence > 1
        ]
        for case in cases:
            extractor = LLMExtractor(LocalEchoAdapter(responder=lambda req, c=case: json.dumps([c])))
            assert extractor(events) == [], f"should reject: {case}"
    finally:
        store.close()


def test_llm_extractor_through_memory_extractor_gate(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        events = _events(store)
        valid_id = events[0]["id"]
        response_json = json.dumps([
            {"layer": "high", "key": "db", "content": {"text": "用 SQLite"},
             "source_event_id": valid_id, "reason": "已确认决策", "confidence": 0.85,
             "category": "decision"},
        ])
        adapter = LocalEchoAdapter(responder=lambda req: response_json)
        extractor = MemoryExtractor(store, extractor=LLMExtractor(adapter))
        report = extractor.scan(session_id="s")
        # The LLM candidate becomes a proposal for human review, never active state.
        assert len(report.candidates) == 1
        assert store.active_state("high") == []
        pending = store.list_proposals(pending_only=True)
        assert len(pending) == 1
        assert pending[0]["proposed_by"] == "extractor"
    finally:
        store.close()


def test_llm_extractor_works_with_openai_adapter_replay(tmp_path, monkeypatch):
    """End-to-end: OpenAIAdapter (replay transport) -> LLMExtractor -> gate."""
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    store, _ = make_store(tmp_path)
    try:
        events = _events(store)
        valid_id = events[0]["id"]
        llm_output = json.dumps([
            {"layer": "high", "key": "db", "content": {"text": "用 SQLite"},
             "source_event_id": valid_id, "reason": "技术决策", "confidence": 0.9,
             "category": "decision"},
        ])
        payload = {
            "id": "r", "model": "gpt-4o",
            "choices": [{"message": {"content": llm_output}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 20},
        }
        calls = []
        def transport(url, headers, body, timeout):
            calls.append(json.loads(body.decode()))
            return 200, json.dumps(payload).encode()
        adapter = OpenAIAdapter(transport=transport)
        extractor = MemoryExtractor(store, extractor=make_llm_extractor(adapter))
        report = extractor.scan(session_id="s")
        assert len(report.candidates) == 1
        # The model was sent the extraction system prompt + the event batch.
        sent = calls[0]
        assert sent["messages"][0]["role"] == "system"
        assert "提取" in sent["messages"][0]["content"] or "记忆" in sent["messages"][0]["content"]
        # And the candidate went through the review gate.
        assert store.active_state("high") == []
        assert len(store.list_proposals(pending_only=True)) == 1
    finally:
        store.close()
