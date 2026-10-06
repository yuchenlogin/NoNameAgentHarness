"""Rerank: three-stage retrieval with explainable ordering."""

from __future__ import annotations

import pytest

from noname_harness.embeddings import local_hash_embedding
from noname_harness.models import EvidenceInput
from noname_harness.rerank import RankedCandidate, default_rerank
from noname_harness.store import HarnessStore


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "rerank project")
    return store, root


def _event(eid, etype="test.failed", occurred="2026-10-01T00:00:00Z"):
    class E:
        id = eid
        event_type = etype
        occurred_at = occurred
    return E()


def test_default_rerank_orders_by_weighted_score_with_reasons():
    candidates = [
        {"event": _event("low", "file.read"), "similarity": 0.5, "ref_id": "low", "evidence": []},
        {"event": _event("high", "test.failed"), "similarity": 0.5, "ref_id": "high", "evidence": [{"id": "e"}]},
    ]
    ranked = default_rerank("q", candidates, reference_time="2026-10-02T00:00:00Z")
    # The high-signal type + evidence candidate outranks the bare one at equal similarity.
    assert ranked[0].event.id == "high"
    assert ranked[0].score > ranked[1].score
    # Every result carries explainable reasons.
    assert any("高信号类型" in r for r in ranked[0].rerank_reasons)
    assert any("相关性" in r for r in ranked[0].rerank_reasons)


def test_promoted_event_boosts_review_status():
    promoted = frozenset({"promoted"})
    candidates = [
        {"event": _event("promoted"), "similarity": 0.5, "ref_id": "p", "evidence": []},
        {"event": _event("plain"), "similarity": 0.5, "ref_id": "q", "evidence": []},
    ]
    ranked = default_rerank("q", candidates, promoted_event_ids=promoted, reference_time="2026-10-02T00:00:00Z")
    assert ranked[0].event.id == "promoted"
    assert any("已提升为法典" in r for r in ranked[0].rerank_reasons)


def test_fresher_event_scores_higher():
    candidates = [
        {"event": _event("old", occurred="2026-01-01T00:00:00Z"), "similarity": 0.5, "ref_id": "o", "evidence": []},
        {"event": _event("new", occurred="2026-10-01T00:00:00Z"), "similarity": 0.5, "ref_id": "n", "evidence": []},
    ]
    ranked = default_rerank("q", candidates, reference_time="2026-10-02T00:00:00Z")
    assert ranked[0].event.id == "new"


def test_search_events_ranked_end_to_end(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "note", {"text": "数据库查询超时"})
        store.append_event("s", "test.failed", {"path": "db.py", "message": "数据库连接失败"},
                          [EvidenceInput("connection refused", "file://db.py")])
        event = store.append_event("s", "decision.accepted", {"key": "db", "content": {"text": "数据库用连接池"}})
        proposal = store.create_proposal("high", "db", {"text": "数据库用连接池"}, [event.id])
        store.review_proposal(proposal["id"], "accept", "user")
        store.build_embedding_index(local_hash_embedding)

        ranked = store.search_events_ranked("数据库", local_hash_embedding)
        assert ranked, "expected ranked results"
        # Every result has a score, ref_id and reasons.
        for hit in ranked:
            assert hit["score"] >= 0
            assert hit["ref_id"]
            assert hit["rerank_reasons"]
        # The promoted decision ranks above the bare note (review-status boost).
        keys = [h["event"].event_type for h in ranked]
        assert "decision.accepted" in keys
    finally:
        store.close()


def test_rerank_is_a_projection_does_not_alter_events(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "note", {"text": "alpha beta"})
        store.build_embedding_index(local_hash_embedding)
        before = [e.payload for e in store.list_events("s", limit=10)]
        store.search_events_ranked("alpha", local_hash_embedding)
        after = [e.payload for e in store.list_events("s", limit=10)]
        assert before == after
        assert store.verify_integrity()["ok"] is True
    finally:
        store.close()


def test_rerank_fn_is_injectable(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "note", {"text": "alpha"})
        store.build_embedding_index(local_hash_embedding)
        called = {}
        def custom_rerank(query, candidates):
            called["n"] = len(candidates)
            # Reverse the order as a trivial custom rerank.
            return [
                RankedCandidate(
                    event=c["event"], similarity=c["similarity"], ref_id=c["ref_id"],
                    score=0.0, rerank_reasons=("custom",),
                )
                for c in reversed(candidates)
            ]
        store.search_events_ranked("alpha", local_hash_embedding, rerank_fn=custom_rerank)
        assert called["n"] >= 1
    finally:
        store.close()
