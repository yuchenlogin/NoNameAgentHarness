"""Migration fidelity: a real database with real data must survive upgrades.

Reviewer finding: the migration tests used hand-built two-table fixtures.  This
suite builds a database that genuinely resembles an older version (with data,
triggers and taste/state rows), then opens it with the current code and asserts
nothing is lost and every guard still works.
"""

from __future__ import annotations

import sqlite3

import pytest

from noname_harness.store import HarnessStore
from noname_harness.taste import TasteService


def _build_v3_database(db_path):
    """Recreate a realistic v3 database: taste layer present, no bitemporal.

    We simulate a v3 file by building the full current schema, then stripping
    the v4/v5 additions and rewinding the version marker.  This is far closer
    to a field database than a hand-written two-table fixture.
    """

    from pathlib import Path

    root = Path(db_path).parent.parent
    store = HarnessStore(db_path)
    store.initialize_project(root, "legacy project")
    # Seed real data while at the current version.
    event = store.append_event(
        "legacy", "project.constraint",
        {"key": "boundary", "content": {"text": "stay in root"}},
    )
    proposal = store.create_proposal(
        "high", "boundary", {"text": "stay in root"}, [event.id]
    )
    store.review_proposal(proposal["id"], "accept", "user")
    TasteService(store).record_authored({"judgement": "legacy vibe"})
    store.close()

    # Now rewind to v3: drop the v4/v5 artifacts and reset the marker.
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        DROP TRIGGER IF EXISTS taste_records_supersede_guard;
        DROP TRIGGER IF EXISTS state_revisions_supersede_guard;
        DROP TRIGGER IF EXISTS taste_records_append_only_update;
        DROP TRIGGER IF EXISTS taste_records_append_only_delete;
        DROP TRIGGER IF EXISTS taste_reviews_append_only_update;
        DROP TRIGGER IF EXISTS taste_reviews_append_only_delete;
        """
    )
    # Recreate state_revisions without the bitemporal columns, preserving rows.
    connection.executescript(
        """
        CREATE TABLE state_revisions_v3 (
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
        INSERT INTO state_revisions_v3
            SELECT id, layer, logical_key, kind, content_json, status,
                   source_event_ids_json, origin_proposal_id, supersedes_id,
                   approved_by, created_at
            FROM state_revisions;
        DROP TABLE state_revisions;
        ALTER TABLE state_revisions_v3 RENAME TO state_revisions;
        UPDATE harness_meta SET value = '3' WHERE key = 'schema_version';
        """
    )
    connection.commit()
    connection.close()


def test_real_v3_database_migrates_without_losing_data(tmp_path):
    db = tmp_path / "legacy" / ".noname" / "harness.db"
    db.parent.mkdir(parents=True)
    _build_v3_database(db)

    store = HarnessStore(db)
    try:
        version = store.query_one(
            "SELECT value FROM harness_meta WHERE key = 'schema_version'"
        )["value"]
        assert version == "5"

        # Bitemporal columns were added to existing rows (NULL bounds).
        columns = {row["name"] for row in store.query("PRAGMA table_info(state_revisions)")}
        assert {"valid_from", "valid_to"} <= columns

        # The pre-migration canon survived and is still projected.
        active = store.active_state("high")
        assert any(item["logical_key"] == "boundary" for item in active)

        # The pre-migration taste survived.
        tastes = TasteService(store).active()
        assert any(t["content"].get("judgement") == "legacy vibe" for t in tastes)

        # Append-only triggers were re-created and still block tampering.
        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute("DELETE FROM taste_records")

        # The supersede INSERT guard is active again on the migrated database.
        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute(
                "INSERT INTO taste_records (id, track, scope, content_json, status, "
                "source_event_ids_json, supersedes_id, origin, actor_id, recorded_at) "
                "VALUES ('z','authored','user','{}','active','[]','ghost','authored','u','2026-01-01')"
            )
        # A failed raw execute leaves the connection mid-transaction; roll it
        # back so subsequent store operations can begin their own transaction.
        store._connection.rollback()

        # The whole migrated ledger still verifies.
        assert store.verify_integrity()["ok"] is True

        # And the database remains fully usable: a new review works end to end.
        proposal = store.list_proposals(pending_only=True)
        event = store.append_event(
            "legacy", "task.updated",
            {"key": "current_task", "content": {"goal": "resume", "next": "go"}},
        )
        new_proposal = store.create_proposal(
            "mid", "current_task", {"goal": "resume", "next": "go"}, [event.id]
        )
        store.review_proposal(
            new_proposal["id"], "accept", "user", valid_from="2026-09-01T00:00:00Z"
        )
        assert store.active_state("mid")[0]["valid_from"] == "2026-09-01T00:00:00Z"
    finally:
        store.close()
