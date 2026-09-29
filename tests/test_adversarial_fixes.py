"""Regression tests for the adversarial-review findings.

Each test pins down one hole the reviewers found, so it cannot silently reopen.
"""

from __future__ import annotations

import pytest

from noname_harness.models import EvidenceInput
from noname_harness.store import HarnessStore
from noname_harness.taste import TasteService


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "adversarial project")
    return store, root


def _adopted_candidate(store):
    moment = store.append_event("s", "model.completed", {"note": "elegant"})
    return TasteService(store).propose_adopted(
        {"judgement": "elegance"}, source_event_ids=[moment.id]
    )


# --- finding 1: edit/resume must not be a backdoor adopt ---------------------

def test_edit_on_candidate_cannot_activate_it(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        taste = TasteService(store)
        candidate = _adopted_candidate(store)
        with pytest.raises(ValueError):
            taste.review(candidate["id"], "edit", "user", edited_content={"judgement": "x"})
        assert taste.active() == []
        assert [t["id"] for t in taste.pending()] == [candidate["id"]]
    finally:
        store.close()


def test_resume_on_candidate_cannot_activate_it(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        taste = TasteService(store)
        candidate = _adopted_candidate(store)
        with pytest.raises(ValueError):
            taste.review(candidate["id"], "resume", "user")
        assert taste.active() == []
    finally:
        store.close()


def test_pause_then_resume_is_not_an_adopt_bypass(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        taste = TasteService(store)
        candidate = _adopted_candidate(store)
        # pause is illegal on a candidate now, so the two-step bypass is closed.
        with pytest.raises(ValueError):
            taste.review(candidate["id"], "pause", "user")
        assert taste.active() == []
    finally:
        store.close()


def test_retired_is_terminal(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        taste = TasteService(store)
        record = taste.record_authored({"judgement": "dark themes"})
        retired = taste.review(record["id"], "retire", "user")
        for action in ("resume", "edit", "pause", "adopt"):
            kwargs = {"edited_content": {"judgement": "y"}} if action == "edit" else {}
            with pytest.raises(ValueError):
                taste.review(retired["id"], action, "user", **kwargs)
        assert taste.active() == []
    finally:
        store.close()


# --- finding 2: stale-head review cannot fork the lineage --------------------

def test_reviewing_a_superseded_record_is_rejected(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        taste = TasteService(store)
        record = taste.record_authored({"judgement": "v1"})
        head = taste.review(record["id"], "edit", "user", edited_content={"judgement": "v2"})
        # The old id is no longer the head; acting on it must not fork the chain.
        with pytest.raises(ValueError):
            taste.review(record["id"], "pause", "user")
        # Only one head survives and it is still active.
        active = taste.active()
        assert len(active) == 1
        assert active[0]["id"] == head["id"]
        assert active[0]["content"]["judgement"] == "v2"
    finally:
        store.close()


# --- finding 3: bitemporal validity is behavioural, not write-only ----------

def _canon(store, key, text):
    event = store.append_event("s", "project.constraint", {"key": key, "content": {"text": text}})
    return store.create_proposal("high", key, {"text": text}, [event.id])


def test_expired_fact_drops_out_of_projection(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        proposal = _canon(store, "deploy_window", "deploy on weekdays")
        store.review_proposal(
            proposal["id"],
            "accept",
            "user",
            valid_from="2026-01-01T00:00:00Z",
            valid_to="2026-02-01T00:00:00Z",
        )
        # Inside the window the fact is projected; after it, it is not.
        assert len(store.active_state("high", as_of="2026-01-15T00:00:00Z")) == 1
        assert store.active_state("high", as_of="2026-03-01T00:00:00Z") == []
        # Default as_of (now, 2026-09) is past the window, so it is gone too.
        assert store.active_state("high") == []
    finally:
        store.close()


def test_not_yet_valid_fact_is_not_projected(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        proposal = _canon(store, "future_rule", "effective next year")
        store.review_proposal(
            proposal["id"], "accept", "user", valid_from="2027-01-01T00:00:00Z"
        )
        assert store.active_state("high") == []
        assert len(store.active_state("high", as_of="2027-06-01T00:00:00Z")) == 1
    finally:
        store.close()


def test_retire_closes_the_valid_interval_by_default(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        proposal = _canon(store, "rule", "a rule")
        store.review_proposal(proposal["id"], "accept", "user")
        assert len(store.active_state("high")) == 1
        store.review_proposal(proposal["id"], "retire", "user")
        # The retired revision carries a valid_to so as-of queries learn it ended.
        row = store._connection.execute(
            "SELECT valid_to, status FROM state_revisions WHERE status = 'retired'"
        ).fetchone()
        assert row is not None
        assert row["valid_to"] is not None
    finally:
        store.close()


def test_invalid_bitemporal_bounds_are_rejected(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        proposal = _canon(store, "rule", "x")
        with pytest.raises(ValueError):
            store.review_proposal(proposal["id"], "accept", "user", valid_from="yesterday")
        with pytest.raises(ValueError):
            store.review_proposal(proposal["id"], "accept", "user", valid_to="not-a-date")
        # Same instant in different offsets must not be misread as inverted.
        proposal2 = _canon(store, "rule2", "y")
        reviewed = store.review_proposal(
            proposal2["id"],
            "accept",
            "user",
            valid_from="2026-09-01T08:00:00+08:00",
            valid_to="2026-09-01T00:00:00Z",
        )
        assert reviewed is not None
    finally:
        store.close()


# --- finding 4 (rebirth): a real fresh session reopens the database ---------

def test_rebirth_survives_close_and_reopen(tmp_path):
    store, root = make_store(tmp_path)
    db_path = root / ".noname" / "harness.db"
    try:
        event = store.append_event(
            "old", "task.updated",
            {"key": "current_task", "content": {"goal": "g", "next": "n"}},
        )
        proposal = store.create_proposal(
            "mid", "current_task", {"goal": "g", "next": "n"}, [event.id]
        )
        store.review_proposal(proposal["id"], "accept", "user")
        TasteService(store).record_authored({"judgement": "small steps"})
        package = store.assemble_context_package("continue", session_id="old")
        package_id = package["package_id"]
    finally:
        store.close()

    # A genuinely fresh session: reopen the DB from disk and read the package
    # back, rather than trusting the in-memory dict.
    with HarnessStore(db_path) as fresh:
        loaded = fresh.get_context_package(package_id)
        assert loaded["layers"]["mid"][0]["content"]["goal"] == "g"
        assert loaded["preference"]["tracks"]["authored"][0]["content"]["judgement"] == "small steps"
        # New evidence appends cleanly onto the reopened ledger.
        fresh.append_event("new", "test.passed", {"path": "tests/test_x.py"})
        assert fresh.verify_integrity()["ok"] is True
