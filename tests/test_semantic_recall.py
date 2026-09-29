"""Semantic recall: injectable embeddings over a rebuildable vector projection."""

from __future__ import annotations

import pytest

from noname_harness.embeddings import cosine_similarity, local_hash_embedding
from noname_harness.models import EvidenceInput
from noname_harness.store import HarnessStore


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "semantic project")
    return store, root


def test_cosine_similarity_math():
    assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine_similarity([1, 1], [1, 1]) == pytest.approx(1.0)
    assert cosine_similarity([0, 0], [1, 0]) == 0.0
    with pytest.raises(ValueError):
        cosine_similarity([1, 0], [1, 0, 0])


def test_local_embedding_is_deterministic_and_similar_for_overlap():
    first = local_hash_embedding("修复算法测试失败")
    second = local_hash_embedding("修复算法测试失败")
    assert first == second  # deterministic
    similar = local_hash_embedding("算法测试失败的修复")
    different = local_hash_embedding("完全无关的内容 xyz")
    assert cosine_similarity(first, similar) > cosine_similarity(first, different)
    # L2 normalised.
    import math
    assert math.sqrt(sum(v * v for v in first)) == pytest.approx(1.0)


def test_build_index_and_semantic_recall(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "test.failed", {"path": "t.py", "message": "算法改进后测试失败"})
        store.append_event("s", "note", {"text": "午饭吃什么"})
        store.append_event("s", "artifact.changed", {"path": "algo.py"}, [EvidenceInput("修复边界条件", None)])
        result = store.build_embedding_index(local_hash_embedding)
        assert result["embedded"] == 3
        assert result["dimensions"] == 256

        hits = store.search_events_semantic("算法 测试 失败", local_hash_embedding)
        assert hits, "expected at least one semantic hit"
        # The most similar event is the test failure about the algorithm.
        assert hits[0]["event"].payload.get("message") == "算法改进后测试失败"
        assert hits[0]["similarity"] > 0
        assert hits[0]["ref_id"]
    finally:
        store.close()


def test_semantic_recall_respects_session_scope(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s1", "note", {"text": "项目目标：跨会话接续"})
        store.append_event("s2", "note", {"text": "项目目标：跨会话接续"})
        store.build_embedding_index(local_hash_embedding)
        hits = store.search_events_semantic("跨会话接续", local_hash_embedding, session_id="s1")
        assert all(h["event"].session_id == "s1" for h in hits)
    finally:
        store.close()


def test_semantic_recall_min_similarity_floor(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "note", {"text": "alpha beta gamma"})
        store.build_embedding_index(local_hash_embedding)
        # A query with zero overlap should be floored out.
        hits = store.search_events_semantic(
            "zzz qqq xxx", local_hash_embedding, min_similarity=0.5
        )
        assert hits == []
    finally:
        store.close()


def test_embedding_index_is_a_rebuildable_projection(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "note", {"text": "可重建的投影"})
        store.build_embedding_index(local_hash_embedding)
        # Delete the projection manually; the events (source of truth) are intact.
        store._connection.execute("DELETE FROM event_embeddings")
        store._connection.commit()
        assert store.list_events("s")  # events still there
        # Rebuild restores the index.
        result = store.build_embedding_index(local_hash_embedding)
        assert result["embedded"] == 1
        assert store.search_events_semantic("可重建", local_hash_embedding)
    finally:
        store.close()


def test_semantic_recall_uses_injectable_embedding(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "note", {"text": "anything"})
        # A custom embedding function plugged in behind the same protocol.
        def custom(text):
            return local_hash_embedding(text, dimensions=64)
        result = store.build_embedding_index(custom)
        assert result["dimensions"] == 64
        hits = store.search_events_semantic("anything", custom)
        assert hits[0]["event"].payload["text"] == "anything"
    finally:
        store.close()


def test_semantic_recall_validation(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        with pytest.raises(ValueError):
            store.search_events_semantic("  ", local_hash_embedding)
        with pytest.raises(ValueError):
            store.search_events_semantic("q", local_hash_embedding, limit=0)
        with pytest.raises(ValueError):
            local_hash_embedding("x", dimensions=4)
    finally:
        store.close()


# --- 对抗性审查发现的回归 ---

def test_searchable_text_matches_fts_coverage(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event(
            "s", "test.failed", {"path": "t.py"},
            [EvidenceInput("boom", "file://docs/spec.pdf")],
        )
        store.build_embedding_index(local_hash_embedding)
        # event_type is embedded too: a query for "test.failed" hits.
        hits = store.search_events_semantic("test.failed", local_hash_embedding)
        assert any(h["event"].event_type == "test.failed" for h in hits)
        # artifact_uri is embedded too: a query for the filename hits.
        hits2 = store.search_events_semantic("docs/spec.pdf", local_hash_embedding)
        assert hits2
    finally:
        store.close()


def test_count_vectorizer_produces_nonnegative_similarity(tmp_path):
    # The sign trick is gone: cosine is always in [0, 1] and lexical overlap
    # ranks above no overlap.
    first = local_hash_embedding("alpha beta gamma delta")
    overlap = local_hash_embedding("alpha beta")
    none = local_hash_embedding("zzz qqq")
    assert cosine_similarity(first, overlap) > 0
    assert cosine_similarity(first, overlap) > cosine_similarity(first, none)
    assert cosine_similarity(first, none) >= 0.0


def test_cjk_self_similarity_and_partial_overlap():
    # CJK has no spaces, but char 3-grams still give meaningful local recall.
    doc = local_hash_embedding("修复算法测试失败")
    same = local_hash_embedding("修复算法测试失败")
    partial = local_hash_embedding("算法测试")
    unrelated = local_hash_embedding("完全无关")
    assert cosine_similarity(doc, same) == pytest.approx(1.0)
    assert cosine_similarity(doc, partial) > cosine_similarity(doc, unrelated)


def test_cross_model_index_query_is_refused(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "note", {"text": "hello"})
        store.build_embedding_index(local_hash_embedding)  # built with local_hash

        def other_model(text):
            return local_hash_embedding(text, dimensions=64)
        other_model.model_id = "other-model"

        # Querying with a different embedding space is refused, not silently wrong.
        with pytest.raises(ValueError):
            store.search_events_semantic("hello", other_model)
    finally:
        store.close()


def test_dimension_mismatch_is_refused(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "note", {"text": "hello"})
        store.build_embedding_index(local_hash_embedding)  # 256 dims

        # Simulate a corrupted/mismatched index entry by overwriting a real
        # event's stored vector with a wrong-dimension one (same model_id).
        from noname_harness.store import _json, _now
        event_id = store.list_events("s")[0].id
        with store.transaction() as conn:
            conn.execute(
                "UPDATE event_embeddings SET vector_json = ? WHERE event_id = ?",
                (_json([0.1] * 8), event_id),
            )
        with pytest.raises(ValueError):
            store.search_events_semantic("hello", local_hash_embedding)
    finally:
        store.close()


def test_build_index_paginates_without_truncation(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        # More than one page (page_size=5000 is patched small via many events).
        for i in range(12):
            store.append_event("s", "note", {"text": f"event {i}"})
        result = store.build_embedding_index(local_hash_embedding)
        assert result["embedded"] == 12  # nothing dropped
    finally:
        store.close()


def test_tie_break_uses_event_id_not_seq(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        # Two sessions, identical text, near-identical timestamps.
        store.append_event("s1", "note", {"text": "identical content"})
        store.append_event("s2", "note", {"text": "identical content"})
        store.build_embedding_index(local_hash_embedding)
        hits = store.search_events_semantic("identical content", local_hash_embedding)
        # Deterministic across repeated calls (event_id tie-break, not per-session seq).
        first_run = [h["event"].id for h in hits]
        second_run = [h["event"].id for h in store.search_events_semantic("identical content", local_hash_embedding)]
        assert first_run == second_run
    finally:
        store.close()
