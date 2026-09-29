"""Bitemporal state, invalidation and the review inbox projection."""

from __future__ import annotations

import sqlite3

import pytest

from noname_harness.store import HarnessStore
from noname_harness.taste import TasteService


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "memory project")
    return store, root


def _canon(store, key, content):
    event = store.append_event(
        "session-a", "project.constraint", {"key": key, "content": content}
    )
    proposal = store.create_proposal("high", key, content, [event.id], kind="constraint")
    return proposal


def test_bitemporal_validity_is_recorded_and_projected(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        proposal = _canon(store, "deploy_window", {"text": "deploy only on weekdays"})
        reviewed = store.review_proposal(
            proposal["id"],
            "accept",
            "user",
            valid_from="2026-09-01T00:00:00Z",
            valid_to="2026-12-31T23:59:59Z",
        )
        active = store.active_state("high")
        assert active[0]["valid_from"] == "2026-09-01T00:00:00Z"
        assert active[0]["valid_to"] == "2026-12-31T23:59:59Z"
        # recorded_at (system time) is independent of valid time.
        assert active[0]["created_at"] != active[0]["valid_from"]
    finally:
        store.close()


def test_valid_to_cannot_precede_valid_from(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        proposal = _canon(store, "window", {"text": "x"})
        with pytest.raises(ValueError):
            store.review_proposal(
                proposal["id"],
                "accept",
                "user",
                valid_from="2026-12-31T00:00:00Z",
                valid_to="2026-01-01T00:00:00Z",
            )
    finally:
        store.close()


def test_retire_invalidates_without_hard_delete(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        proposal = _canon(store, "old_rule", {"text": "outdated constraint"})
        store.review_proposal(proposal["id"], "accept", "user")
        assert len(store.active_state("high")) == 1

        # Retiring writes a new retired revision; the active projection drops
        # it but history is preserved.
        store.review_proposal(proposal["id"], "retire", "user", reason="no longer true")
        assert store.active_state("high") == []
        rows = store._connection.execute(
            "SELECT status, COUNT(*) AS n FROM state_revisions GROUP BY status"
        ).fetchall()
        statuses = {row["status"]: row["n"] for row in rows}
        assert statuses.get("active") == 1
        assert statuses.get("retired") == 1
    finally:
        store.close()


def test_review_inbox_aggregates_canon_task_and_taste(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        # One high-layer (canon) proposal and one mid-layer (task) proposal.
        canon = _canon(store, "boundary", {"text": "stay in root"})
        task_event = store.append_event(
            "session-a",
            "task.updated",
            {"key": "current_task", "content": {"goal": "fix", "next": "inspect"}},
        )
        store.create_proposal(
            "mid", "current_task", {"goal": "fix", "next": "inspect"}, [task_event.id]
        )

        # One adopted taste candidate awaiting adoption.
        moment = store.append_event("session-a", "model.completed", {"note": "elegant"})
        TasteService(store).propose_adopted(
            {"judgement": "elegance"}, source_event_ids=[moment.id]
        )

        inbox = store.review_inbox()
        assert inbox["counts"] == {"canon": 1, "task": 1, "taste": 1, "total": 3}
        assert inbox["canon_pending"][0]["id"] == canon["id"]
        assert inbox["canon_pending"][0]["impact"] == "project canon (long-term)"
        assert inbox["task_pending"][0]["layer"] == "mid"
        assert inbox["taste_pending"][0]["track"] == "adopted"
        # Every inbox entry carries provenance for a judgement call.
        for group in ("canon_pending", "task_pending", "taste_pending"):
            for item in inbox[group]:
                assert item["source_event_ids"]
                assert "impact" in item
    finally:
        store.close()


def test_review_inbox_empties_after_decisions(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        canon = _canon(store, "boundary", {"text": "stay in root"})
        assert store.review_inbox()["counts"]["total"] == 1
        store.review_proposal(canon["id"], "accept", "user")
        assert store.review_inbox()["counts"]["total"] == 0
    finally:
        store.close()


def test_schema_v3_to_v4_adds_bitemporal_columns(tmp_path):
    db = tmp_path / "v3.db"
    connection = sqlite3.connect(db)
    connection.executescript(
        """
        CREATE TABLE harness_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO harness_meta(key, value) VALUES ('schema_version', '3');
        CREATE TABLE state_revisions (
            id TEXT PRIMARY KEY,
            layer TEXT NOT NULL,
            logical_key TEXT NOT NULL,
            kind TEXT NOT NULL,
            content_json TEXT NOT NULL,
            status TEXT NOT NULL,
            source_event_ids_json TEXT NOT NULL,
            origin_proposal_id TEXT,
            supersedes_id TEXT,
            approved_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()

    store = HarnessStore(db)
    try:
        columns = {
            row["name"]
            for row in store._connection.execute("PRAGMA table_info(state_revisions)")
        }
        assert "valid_from" in columns
        assert "valid_to" in columns
        version = store._connection.execute(
            "SELECT value FROM harness_meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        assert version == "5"
    finally:
        store.close()
