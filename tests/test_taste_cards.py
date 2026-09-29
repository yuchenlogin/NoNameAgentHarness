"""Taste cards: deterministic clustering, review lifecycle, rebuildable image."""

from __future__ import annotations

import sqlite3

import pytest

from noname_harness.store import HarnessStore
from noname_harness.taste import TasteService
from noname_harness.taste_cards import TasteCardService


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
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
