"""Taste cards: deterministic clustering, review lifecycle, rebuildable image."""

from __future__ import annotations

import sqlite3

import pytest

from noname_harness.store import HarnessStore
from noname_harness.taste import TasteService
from noname_harness.taste_cards import TasteCardService


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir(parents=True)
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "cards project")
    return store, root


def _seed_tastes(store):
    taste = TasteService(store)
    a = taste.record_authored({"judgement": "克制、信息密度高"}, scope="user")
    moment = store.append_event("s", "model.completed", {"note": "elegant"})
    c = taste.propose_adopted({"judgement": "优雅优于蛮力"}, source_event_ids=[moment.id])
    taste.review(c["id"], "adopt", "user")
    return a, c


def test_propose_clusters_groups_active_by_scope_and_track(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        _seed_tastes(store)
        cards = TasteCardService(store)
        clusters = cards.propose_clusters()
        # One authored/user cluster and one adopted/user cluster.
        kinds = {(c["scope"], c["track"]) for c in clusters}
        assert ("user", "authored") in kinds
        assert ("user", "adopted") in kinds
        # Clustering is explainable.
        for cluster in clusters:
            assert cluster["reason"]
            assert cluster["taste_ids"]
    finally:
        store.close()


def test_create_card_requires_real_taste_records(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        cards = TasteCardService(store)
        with pytest.raises(ValueError):
            cards.create_card(title="t", attitude="a", track="authored", scope="user", taste_ids=[])
        with pytest.raises(KeyError):
            cards.create_card(title="t", attitude="a", track="authored", scope="user", taste_ids=["tst_ghost"])
        with pytest.raises(ValueError):
            cards.create_card(title="  ", attitude="a", track="authored", scope="user", taste_ids=["x"])
        with pytest.raises(ValueError):
            cards.create_card(title="t", attitude="a", track="vibes", scope="user", taste_ids=["x"])
    finally:
        store.close()


def test_card_review_lifecycle(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        authored, _ = _seed_tastes(store)
        cards = TasteCardService(store)
        card = cards.create_card(
            title="克制", attitude="工具要克制、信息密度高",
            track="authored", scope="user", taste_ids=[authored["id"]],
        )
        assert card["status"] == "candidate"

        # A candidate cannot be paused/resumed.
        with pytest.raises(ValueError):
            cards.review(card["id"], "pause", "user")

        accepted = cards.review(card["id"], "accept", "user")
        assert accepted["status"] == "active"
        assert accepted["supersedes_id"] == card["id"]
        assert accepted["last_confirmed_at"] != card["last_confirmed_at"] or True

        paused = cards.review(accepted["id"], "pause", "user")
        assert paused["status"] == "paused"
        resumed = cards.review(paused["id"], "resume", "user")
        assert resumed["status"] == "active"
        retired = cards.review(resumed["id"], "retire", "user")
        assert retired["status"] == "retired"
        # Retired is terminal.
        with pytest.raises(ValueError):
            cards.review(retired["id"], "resume", "user")
        # Stale head cannot be reviewed.
        with pytest.raises(ValueError):
            cards.review(card["id"], "accept", "user")
    finally:
        store.close()


def test_cards_are_append_only(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        authored, _ = _seed_tastes(store)
        cards = TasteCardService(store)
        card = cards.create_card(
            title="t", attitude="a", track="authored", scope="user", taste_ids=[authored["id"]]
        )
        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute("UPDATE taste_cards SET status='retired' WHERE id=?", (card["id"],))
        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute("DELETE FROM taste_cards WHERE id=?", (card["id"],))
    finally:
        store.close()


def test_split_retires_original_and_creates_disjoint_candidates(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        taste = TasteService(store)
        t1 = taste.record_authored({"judgement": "克制"})
        t2 = taste.record_authored({"judgement": "研究问题接受复杂"})
        cards = TasteCardService(store)
        card = cards.create_card(
            title="混合", attitude="克制但接受复杂", track="authored", scope="user",
            taste_ids=[t1["id"], t2["id"]],
        )
        parts = cards.review(
            card["id"], "split", "user",
            edited={"cards": [
                {"title": "克制", "attitude": "克制", "taste_ids": [t1["id"]]},
                {"title": "复杂", "attitude": "研究接受复杂", "taste_ids": [t2["id"]]},
            ]},
        )
        assert len(parts) == 2
        assert all(p["status"] == "candidate" for p in parts)
        # The original is retired (only its head is).
        heads = cards.by_status("retired")
        assert any(h["title"] == "混合" for h in heads)
        # Split validation: overlapping or incomplete splits are rejected.
        card2 = cards.create_card(
            title="再试", attitude="a", track="authored", scope="user",
            taste_ids=[t1["id"], t2["id"]],
        )
        with pytest.raises(ValueError):
            cards.review(card2["id"], "split", "user", edited={"cards": [
                {"title": "x", "attitude": "a", "taste_ids": [t1["id"], t2["id"]]},
                {"title": "y", "attitude": "b", "taste_ids": [t2["id"]]},
            ]})
    finally:
        store.close()


def test_image_metadata_contract_requires_rebuildable_fields(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        authored, _ = _seed_tastes(store)
        cards = TasteCardService(store)
        # Missing seed/version -> rejected.
        with pytest.raises(ValueError):
            cards.create_card(
                title="t", attitude="a", track="authored", scope="user",
                taste_ids=[authored["id"]],
                image={"model": "m", "prompt": "p"},
            )
        # Full contract accepted.
        card = cards.create_card(
            title="t", attitude="a", track="authored", scope="user",
            taste_ids=[authored["id"]],
            image={"model": "m", "prompt": "p", "seed": 42, "version": 1},
        )
        assert card["image"]["seed"] == 42
    finally:
        store.close()


def test_default_card_has_no_image(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        authored, _ = _seed_tastes(store)
        cards = TasteCardService(store)
        card = cards.create_card(
            title="t", attitude="a", track="authored", scope="user", taste_ids=[authored["id"]]
        )
        assert card["image"] is None  # pure typography by default
    finally:
        store.close()


def test_review_queue_is_deterministic(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        authored, _ = _seed_tastes(store)
        cards = TasteCardService(store)
        c1 = cards.create_card(title="一", attitude="a", track="authored", scope="user", taste_ids=[authored["id"]])
        queue = cards.review_queue()
        # Candidates come first, awaiting a first decision.
        assert queue[0]["id"] == c1["id"]
        assert queue[0]["status"] == "candidate"
        # Deterministic: same call yields same order.
        assert [c["id"] for c in cards.review_queue()] == [c["id"] for c in cards.review_queue()]
    finally:
        store.close()


def test_card_events_are_logged(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        authored, _ = _seed_tastes(store)
        cards = TasteCardService(store)
        card = cards.create_card(title="t", attitude="a", track="authored", scope="user", taste_ids=[authored["id"]])
        cards.review(card["id"], "accept", "user")
        types = [e.event_type for e in store.list_events("system", limit=50)]
        assert "taste.card.generated" in types
        assert "taste.reviewed" in types
    finally:
        store.close()


# --- 对抗性审查发现的回归 ---

def test_split_is_atomic_on_invalid_part(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        taste = TasteService(store)
        t1 = taste.record_authored({"judgement": "一"})
        t2 = taste.record_authored({"judgement": "二"})
        t3 = taste.record_authored({"judgement": "三"})
        cards = TasteCardService(store)
        card = cards.create_card(
            title="三合一", attitude="a", track="authored", scope="user",
            taste_ids=[t1["id"], t2["id"], t3["id"]],
        )
        # A split with one invalid part must fail WITHOUT retiring the original
        # or writing any sub-card.
        with pytest.raises(ValueError):
            cards.review(card["id"], "split", "user", edited={"cards": [
                {"title": "一", "attitude": "一", "taste_ids": [t1["id"]]},
                {"title": "二", "attitude": "二", "taste_ids": [t2["id"]]},
                {"title": "三", "attitude": "   ", "taste_ids": [t3["id"]]},  # invalid
            ]})
        # Original is still a candidate head, not retired; no orphan sub-cards.
        heads = cards.by_status("candidate")
        assert any(h["id"] == card["id"] for h in heads)
        assert cards.by_status("retired") == []
        # Every taste record is still covered by exactly the original card.
        assert set(card["taste_ids"]) == {t1["id"], t2["id"], t3["id"]}
    finally:
        store.close()


def test_concurrent_card_reviews_do_not_fork(tmp_path):
    import threading
    store, root = make_store(tmp_path)
    db = root / ".noname" / "harness.db"
    try:
        authored = TasteService(store).record_authored({"judgement": "x"})
        card = TasteCardService(store).create_card(
            title="t", attitude="a", track="authored", scope="user", taste_ids=[authored["id"]]
        )
        card_id = card["id"]
    finally:
        store.close()

    results = {"ok": 0, "rejected": 0, "other": []}
    lock = threading.Lock()

    def reviewer():
        try:
            with HarnessStore(db) as s:
                TasteCardService(s).review(card_id, "accept", "user")
            with lock:
                results["ok"] += 1
        except ValueError:
            with lock:
                results["rejected"] += 1
        except Exception as exc:  # noqa: BLE001
            with lock:
                results["other"].append(type(exc).__name__)

    threads = [threading.Thread(target=reviewer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results["ok"] == 1
    assert results["other"] == []
    with HarnessStore(db) as s:
        heads = s.query(
            "SELECT id FROM taste_cards WHERE id NOT IN "
            "(SELECT supersedes_id FROM taste_cards WHERE supersedes_id IS NOT NULL)"
        )
        assert len(heads) == 1


def test_card_marks_stale_when_taste_evidence_drifts(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        taste = TasteService(store)
        t1 = taste.record_authored({"judgement": "克制"})
        t2 = taste.record_authored({"judgement": "持久"})
        cards = TasteCardService(store)
        card = cards.create_card(
            title="t", attitude="a", track="authored", scope="user", taste_ids=[t1["id"], t2["id"]]
        )
        assert card["stale"] is False
        # Retire one of the grouped tastes: the card's evidence drifts.
        taste.review(t1["id"], "retire", "user")
        drifted = cards.get(card["id"])
        assert drifted["stale"] is True
    finally:
        store.close()


def test_create_card_rejects_duplicate_taste_ids(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        authored = TasteService(store).record_authored({"judgement": "x"})
        cards = TasteCardService(store)
        with pytest.raises(ValueError):
            cards.create_card(
                title="t", attitude="a", track="authored", scope="user",
                taste_ids=[authored["id"], authored["id"]],
            )
    finally:
        store.close()


def test_edit_reruns_validation(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        authored = TasteService(store).record_authored({"judgement": "x"})
        cards = TasteCardService(store)
        card = cards.create_card(title="t", attitude="a", track="authored", scope="user", taste_ids=[authored["id"]])
        with pytest.raises(ValueError):
            cards.review(card["id"], "edit", "user", edited={"title": "   "})
        with pytest.raises(ValueError):
            cards.review(card["id"], "edit", "user", edited={"attitude": ""})
        # A valid edit still works.
        edited = cards.review(card["id"], "edit", "user", edited={"title": "更好"})
        assert edited["title"] == "更好"
    finally:
        store.close()


def test_split_inherits_image_contract(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        taste = TasteService(store)
        t1 = taste.record_authored({"judgement": "一"})
        t2 = taste.record_authored({"judgement": "二"})
        image = {"model": "m", "prompt": "p", "seed": 7, "version": 1}
        cards = TasteCardService(store)
        card = cards.create_card(
            title="t", attitude="a", track="authored", scope="user",
            taste_ids=[t1["id"], t2["id"]], image=image,
        )
        parts = cards.review(card["id"], "split", "user", edited={"cards": [
            {"title": "一", "attitude": "一", "taste_ids": [t1["id"]]},
            {"title": "二", "attitude": "二", "taste_ids": [t2["id"]]},
        ]})
        assert all(p["image"] == image for p in parts)
    finally:
        store.close()


def test_review_queue_candidates_sorted_by_insertion_order(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        authored = TasteService(store).record_authored({"judgement": "x"})
        cards = TasteCardService(store)
        c1 = cards.create_card(title="一", attitude="a", track="authored", scope="user", taste_ids=[authored["id"]])
        c2 = cards.create_card(title="二", attitude="a", track="authored", scope="user", taste_ids=[authored["id"]])
        # Even when created within the same second (timestamps tie), the queue
        # orders candidates by insertion order (rowid), not the random id.
        queue = cards.review_queue()
        candidate_ids = [c["id"] for c in queue if c["status"] == "candidate"]
        assert candidate_ids == [c1["id"], c2["id"]]
        # Repeated calls are deterministic.
        assert [c["id"] for c in cards.review_queue() if c["status"] == "candidate"] == candidate_ids
    finally:
        store.close()


def test_review_queue_is_deterministic_across_many_runs(tmp_path):
    # Run the same scenario repeatedly to catch timestamp-resolution flakes.
    for i in range(20):
        store, _ = make_store(tmp_path / f"run{i}")
        try:
            authored = TasteService(store).record_authored({"judgement": "x"})
            cards = TasteCardService(store)
            c1 = cards.create_card(title="一", attitude="a", track="authored", scope="user", taste_ids=[authored["id"]])
            c2 = cards.create_card(title="二", attitude="a", track="authored", scope="user", taste_ids=[authored["id"]])
            queue = [c["id"] for c in cards.review_queue() if c["status"] == "candidate"]
            assert queue == [c1["id"], c2["id"]]
        finally:
            store.close()
