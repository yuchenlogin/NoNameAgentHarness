"""State Diff: version evolution of canon and taste cards."""

from __future__ import annotations

import pytest

from noname_harness.ledger_view import build_ledger_model, render_ledger_html
from noname_harness.state_diff import build_state_diff_model, render_state_diff_html
from noname_harness.store import HarnessStore
from noname_harness.taste import TasteService
from noname_harness.taste_cards import TasteCardService


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "diff project")
    return store, root


def _canon(store, key, text, session="s"):
    event = store.append_event(session, "project.constraint", {"key": key, "content": {"text": text}})
    proposal = store.create_proposal("high", key, {"text": text}, [event.id])
    return proposal


def test_canon_version_evolution_shows_add_supersede_retire(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        # v1: add. v2: supersede with edited content. v3: retire.
        p1 = _canon(store, "rule", "v1 rule")
        store.review_proposal(p1["id"], "accept", "user")
        p2 = _canon(store, "rule", "v1 rule")
        store.review_proposal(p2["id"], "edit", "user", edited_content={"text": "v2 rule"})
        store.review_proposal(p2["id"], "retire", "user")

        model = build_state_diff_model(store)
        diff = next(d for d in model["canon_diffs"] if d["logical_key"] == "rule")
        assert diff["version_count"] == 3
        # v1 is marked as added, v2 as superseded, v3 as retired.
        assert diff["versions"][0]["delta_from_previous"] is None  # v1 is the first
        assert "新增" not in str(diff["versions"][0].get("delta_from_previous"))
        assert diff["versions"][1]["delta_from_previous"] == "内容被新版本取代"
        assert diff["versions"][2]["status"] == "retired"
        assert diff["versions"][2]["delta_from_previous"] == "失效（retired，保留历史）"
        # The head is the last version.
        assert diff["versions"][-1]["is_head"] is True
    finally:
        store.close()


def test_state_history_returns_full_chain_oldest_first(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        p1 = _canon(store, "rule", "v1")
        store.review_proposal(p1["id"], "accept", "user")
        p2 = _canon(store, "rule", "v1")
        store.review_proposal(p2["id"], "edit", "user", edited_content={"text": "v2"})
        history = store.state_history("high", "rule")
        assert len(history) == 2
        assert history[0]["content"] == {"text": "v1"}
        assert history[1]["content"] == {"text": "v2"}
        assert history[1]["supersedes_id"] == history[0]["id"]
    finally:
        store.close()


def test_bitemporal_validity_is_shown(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        proposal = _canon(store, "window", "deploy on weekdays")
        store.review_proposal(
            proposal["id"], "accept", "user",
            valid_from="2026-01-01T00:00:00Z", valid_to="2026-12-31T00:00:00Z",
        )
        model = build_state_diff_model(store)
        version = model["canon_diffs"][0]["versions"][0]
        assert version["valid_from"] == "2026-01-01T00:00:00Z"
        assert version["valid_to"] == "2026-12-31T00:00:00Z"
    finally:
        store.close()


def test_taste_card_evolution_chain(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        taste = TasteService(store)
        record = taste.record_authored({"judgement": "克制"}, scope="user")
        cards = TasteCardService(store)
        card = cards.create_card(title="克制", attitude="工具克制", track="authored", scope="user", taste_ids=[record["id"]])
        cards.review(card["id"], "accept", "user")
        cards.review(cards.by_status("active")[0]["id"], "pause", "user")
        model = build_state_diff_model(store)
        assert model["card_chains"], "expected a taste-card chain"
        chain = model["card_chains"][0]
        statuses = [v["status"] for v in chain["versions"]]
        assert "candidate" in statuses
        assert "active" in statuses
        assert "paused" in statuses
        assert chain["head_status"] == "paused"
    finally:
        store.close()


def test_state_diff_is_a_pure_projection(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        proposal = _canon(store, "rule", "x")
        store.review_proposal(proposal["id"], "accept", "user")
        before = [e.id for e in store.list_events("s", limit=100)]
        build_state_diff_model(store)
        render_state_diff_html(build_state_diff_model(store))
        after = [e.id for e in store.list_events("s", limit=100)]
        assert before == after
        assert store.verify_integrity()["ok"] is True
    finally:
        store.close()


def test_rendered_html_escapes_and_shows_versions(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        p1 = _canon(store, "rule", "<script>alert(1)</script>")
        store.review_proposal(p1["id"], "accept", "user")
        p2 = _canon(store, "rule", "x")
        store.review_proposal(p2["id"], "retire", "user")
        page = render_state_diff_html(build_state_diff_model(store))
        assert "版本演进" in page
        assert "v1" in page and "v2" in page
        assert "失效" in page
        assert "<script>alert(1)</script>" not in page
    finally:
        store.close()


def test_ledger_html_includes_state_diff_section(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        proposal = _canon(store, "rule", "x")
        store.review_proposal(proposal["id"], "accept", "user")
        page = render_ledger_html(build_ledger_model(store))
        assert "版本演进 · 现在为何如此" in page
    finally:
        store.close()
