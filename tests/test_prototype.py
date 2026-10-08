from __future__ import annotations

import json
import sqlite3

import pytest

from noname_harness.context import render_markdown
from noname_harness.curator import CuratorService
from noname_harness.handoff import infer_next_steps
from noname_harness.models import EvidenceInput
from noname_harness.store import SCHEMA_VERSION
from noname_harness.store import HarnessStore, WorkspaceBoundaryError


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "test project")
    return store, root


def test_events_and_evidence_are_append_only(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        event = store.append_event(
            "session-a",
            "test.failed",
            {"path": "src/algorithm.py", "message": "expected 3, got 4"},
            [EvidenceInput("assert result == 3", "file://src/algorithm.py", 12, 31)],
        )

        assert event.seq == 1
        assert store.list_events("session-a")[0].id == event.id
        evidence = store.evidence_for_event(event.id)
        assert len(evidence) == 1
        assert evidence[0]["content"] == "assert result == 3"
        with pytest.raises(ValueError):
            store.append_event(
                "session-a",
                "bad.span",
                {},
                [EvidenceInput("bad", "file://src/algorithm.py", 8, 2)],
            )
        assert all(item.event_type != "bad.span" for item in store.list_events("session-a", limit=10))

        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute(
                "UPDATE session_events SET event_type = 'changed' WHERE id = ?",
                (event.id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute(
                "DELETE FROM evidence_spans WHERE event_id = ?", (event.id,)
            )
        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute("UPDATE project SET name = 'tampered' WHERE id = 1")
        report = store.verify_integrity()
        assert report["ok"] is True
        assert report["bad_event_ids"] == []
        assert report["bad_evidence_ids"] == []
        assert report["unverified_event_ids"] == []
        assert report["unverified_evidence_ids"] == []
        # Rows written by this build are signed over their full provenance, not
        # just the payload (v8 closed exactly that hole).
        assert report["hash_coverage"]["events"] == {
            "v1_payload_only": 0,
            "v2_full_provenance": 1,
            "unknown_versions": [],
        }
        assert report["hash_coverage"]["evidence"] == {
            "v1_content_only": 0,
            "v2_full_provenance": 1,
            "unknown_versions": [],
        }
        assert store.search_events("expected 3")[0].id == event.id
        assert store.search_events("assert result")[0].id == event.id
        assert store.search_events("src/algorithm.py")[0].id == event.id
        if store._fts_available:
            store._connection.execute("DELETE FROM event_search")
            store._connection.commit()
            assert store.search_events("expected 3") == []
            assert store.rebuild_search_index() is True
            assert store.search_events("expected 3")[0].id == event.id
    finally:
        store.close()


def test_durable_state_requires_provenance_and_is_versioned(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        first_event = store.append_event(
            "session-a",
            "project.constraint",
            {"key": "workspace_boundary", "content": {"text": "stay in project root"}},
        )
        with pytest.raises(ValueError):
            store.create_proposal("high", "bad", {"text": "no source"}, [])
        with pytest.raises(ValueError):
            store.create_proposal(
                "high", "bad-confidence", {"text": "invalid"}, [first_event.id], confidence=float("nan")
            )

        first = store.create_proposal(
            "high",
            "workspace_boundary",
            {"text": "stay in project root"},
            [first_event.id],
            kind="constraint",
            confidence=0.99,
            reason="explicit project boundary",
        )
        assert first["reason"] == "explicit project boundary"
        store.review_proposal(first["id"], "accept", "user")
        assert store.active_state("high")[0]["content"] == {"text": "stay in project root"}
        audit_types = [event.event_type for event in store.list_events("session-a", limit=10)]
        assert "memory.proposed" in audit_types
        assert "memory.reviewed" in audit_types
        first_review = next(
            event for event in store.list_events("session-a", limit=10)
            if event.event_type == "memory.reviewed"
        )
        assert first_review.payload["revision_id"].startswith("stt_")

        second_event = store.append_event(
            "session-a",
            "project.constraint",
            {"key": "workspace_boundary", "content": {"text": "stay inside the configured root"}},
        )
        second = store.create_proposal(
            "high",
            "workspace_boundary",
            {"text": "stay inside the configured root"},
            [second_event.id],
            kind="constraint",
        )
        assert first["id"] in second["conflict_with_ids"] or second["conflict_with_ids"]
        store.review_proposal(second["id"], "edit", "user", edited_content={"text": "stay inside root"})
        active = store.active_state("high")
        assert len(active) == 1
        assert active[0]["content"] == {"text": "stay inside root"}
        revisions = store._connection.execute("SELECT COUNT(*) AS n FROM state_revisions").fetchone()["n"]
        assert revisions == 2
        latest_review = next(
            event
            for event in store.list_events("session-a", limit=20)
            if event.event_type == "memory.reviewed" and event.payload["proposal_id"] == second["id"]
        )
        assert latest_review.payload["revision_id"].startswith("stt_")
        assert latest_review.payload["supersedes_id"] == active[0]["supersedes_id"]
        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute(
                "UPDATE state_revisions SET kind = 'tampered' WHERE id = ?",
                (active[0]["id"],),
            )
    finally:
        store.close()


def test_curator_only_promotes_structured_high_or_mid_hints(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        task_event = store.append_event(
            "session-a",
            "task.updated",
            {
                "key": "current_task",
                "content": {"goal": "fix failing algorithm test", "progress": "reproduced"},
            },
        )
        failure_event = store.append_event(
            "session-a",
            "test.failed",
            {"path": "tests/test_algorithm.py", "message": "off by one"},
        )
        curator = CuratorService(store)
        proposals = curator.scan("session-a")
        assert len(proposals) == 1
        assert proposals[0]["layer"] == "mid"
        assert proposals[0]["source_event_ids"] == [task_event.id]
        assert curator.propose_from_event(task_event.id) is None
        assert curator.propose_from_event(failure_event.id) is None
    finally:
        store.close()


def test_next_step_candidates_do_not_revive_resolved_or_repeated_failures():
    events = [
        {
            "event_id": "change",
            "seq": 1,
            "occurred_at": "2026-08-28T00:00:01Z",
            "event_type": "artifact.changed",
            "payload": {"path": "src/algorithm.py"},
        },
        {
            "event_id": "failure-old",
            "seq": 2,
            "occurred_at": "2026-08-28T00:00:02Z",
            "event_type": "test.failed",
            "payload": {"path": "tests/test_algorithm.py"},
        },
        {
            "event_id": "failure-new",
            "seq": 3,
            "occurred_at": "2026-08-28T00:00:03Z",
            "event_type": "test.failed",
            "payload": {"path": "tests/test_algorithm.py"},
        },
        {
            "event_id": "passed",
            "seq": 4,
            "occurred_at": "2026-08-28T00:00:04Z",
            "event_type": "test.passed",
            "payload": {"path": "tests/test_algorithm.py"},
        },
    ]

    assert infer_next_steps(events) == []

    latest_failure = {
        "event_id": "failure-after-pass",
        "seq": 5,
        "occurred_at": "2026-08-28T00:00:05Z",
        "event_type": "test.failed",
        "payload": {"path": "tests/test_algorithm.py"},
    }
    candidates = infer_next_steps([*events, latest_failure])
    assert len(candidates) == 1
    assert candidates[0]["source_event_ids"] == ["failure-after-pass"]


def test_context_package_exposes_layers_and_respects_workspace_boundary(tmp_path):
    store, root = make_store(tmp_path)
    try:
        goal_event = store.append_event(
            "session-a",
            "project.goal",
            {"key": "goal", "content": {"text": "preserve handoff fidelity"}},
        )
        goal_proposal = store.create_proposal(
            "high", "goal", {"text": "preserve handoff fidelity"}, [goal_event.id], kind="goal"
        )
        store.review_proposal(goal_proposal["id"], "accept", "user")

        task_event = store.append_event(
            "session-a",
            "task.updated",
            {"key": "current_task", "content": {"goal": "fix test", "next": "inspect diff"}},
        )
        task_proposal = store.create_proposal(
            "mid", "current_task", {"goal": "fix test", "next": "inspect diff"}, [task_event.id]
        )
        store.review_proposal(task_proposal["id"], "accept", "user")
        store.append_event(
            "session-a",
            "artifact.changed",
            {"path": "src/algorithm.py", "change": "candidate improvement"},
            [EvidenceInput("diff --git a/src/algorithm.py b/src/algorithm.py", "file://src/algorithm.py")],
        )
        store.append_event(
            "session-a",
            "test.failed",
            {"path": "tests/test_algorithm.py", "message": "off by one"},
        )
        # An explicit snapshot may already exist; assembling a handoff should
        # still expose the current one without spending the work-event window
        # on a run of duplicate snapshot markers.
        store.append_workspace_snapshot("session-a")

        package = store.assemble_context_package("继续处理上次的测试问题", session_id="session-a")
        assert store.get_context_package(package["package_id"]) == package
        assert any(
            event.event_type == "context.assembled"
            and event.payload["package_id"] == package["package_id"]
            for event in store.list_events("session-a", limit=20)
        )
        assert package["layers"]["high"][0]["logical_key"] == "goal"
        assert package["layers"]["mid"][0]["logical_key"] == "current_task"
        next_step_texts = {item["text"] for item in package["next_step_candidates"]}
        assert "inspect diff" in next_step_texts
        assert "检查 tests/test_algorithm.py 的失败原因，并对照最近一次改动" in next_step_texts
        assert not any("运行覆盖 src/algorithm.py" in text for text in next_step_texts)
        assert [item["event_type"] for item in package["layers"]["low"]] == [
            "test.failed",
            "artifact.changed",
            "workspace.snapshot",
        ]
        assert package["assembly"]["workspace_snapshot_included"] is True
        assert package["assembly"]["low_event_limit"] == 20
        assert "outside the configured workspace root" in package["guardrails"]["workspace_policy"]
        markdown = render_markdown(package)
        assert "## High layer" in markdown
        assert "## Mid layer" in markdown
        assert "## Low layer" in markdown
        assert "## Next-step candidates" in markdown
        assert package["provenance"]["evidence_ids"]
        assert "diff --git a/src/algorithm.py b/src/algorithm.py" in markdown
        compact_package = store.assemble_context_package(
            "继续处理上次的测试问题", session_id="session-a", low_limit=1
        )
        assert goal_event.id in compact_package["provenance"]["event_ids"]
        assert task_event.id in compact_package["provenance"]["event_ids"]
        assert "preserve handoff fidelity" in markdown

        compact_low = [item["event_type"] for item in compact_package["layers"]["low"]]
        assert compact_low == ["test.failed", "workspace.snapshot"]
        assert markdown.index("artifact.changed") < markdown.index("test.failed")
        assert markdown.index("test.failed") < markdown.index("workspace.snapshot")

        fenced = render_markdown(
            {
                **package,
                "layers": {
                    **package["layers"],
                    "low": [
                        {
                            **package["layers"]["low"][1],
                            "evidence": [
                                {
                                    **package["layers"]["low"][1]["evidence"][0],
                                    "content": "a```b",
                                }
                            ],
                        }
                    ],
                },
            }
        )
        assert "    ````text" in fenced
        assert "    a```b" in fenced

        destination = store.write_context_package(root / ".noname" / "packages" / "handoff.json", package)
        assert destination.exists()
        assert json.loads(destination.read_text(encoding="utf-8"))["package_id"] == package["package_id"]
        with pytest.raises(WorkspaceBoundaryError):
            store.write_context_package(tmp_path / "outside.json", package)
    finally:
        store.close()


def test_project_database_cannot_attach_to_a_different_workspace(tmp_path):
    store, root = make_store(tmp_path)
    try:
        other = tmp_path / "other"
        other.mkdir()
        with pytest.raises(ValueError):
            store.initialize_project(other)
        with pytest.raises(WorkspaceBoundaryError):
            store.append_event(
                "session-a",
                "artifact.changed",
                {"path": "outside"},
                [EvidenceInput("secret", f"file://{other / 'secret.txt'}")],
            )
    finally:
        store.close()


def test_schema_v1_is_migrated_through_the_chain_to_latest(tmp_path):
    db = tmp_path / "legacy.db"
    connection = sqlite3.connect(db)
    connection.executescript(
        """
        CREATE TABLE harness_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO harness_meta(key, value) VALUES ('schema_version', '1');
        CREATE TABLE state_proposals (
            id TEXT PRIMARY KEY,
            layer TEXT NOT NULL,
            kind TEXT NOT NULL,
            logical_key TEXT NOT NULL,
            content_json TEXT NOT NULL,
            source_event_ids_json TEXT NOT NULL,
            conflict_with_ids_json TEXT NOT NULL,
            proposed_by TEXT NOT NULL,
            confidence REAL,
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
            for row in store._connection.execute("PRAGMA table_info(state_proposals)")
        }
        version = store._connection.execute(
            "SELECT value FROM harness_meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        assert "proposal_reason" in columns
        # A v1 database is upgraded step by step to the current version.
        assert version == str(SCHEMA_VERSION)
        # The taste layer introduced by v3 exists after the migration.
        taste_tables = {
            row["name"]
            for row in store._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'taste_%'"
            )
        }
        assert taste_tables == {"taste_records", "taste_reviews", "taste_cards"}
    finally:
        store.close()


def test_workspace_snapshot_is_read_only_and_becomes_low_layer_evidence(tmp_path):
    store, root = make_store(tmp_path)
    try:
        before = sorted(path.relative_to(root) for path in root.rglob("*"))
        event = store.append_workspace_snapshot("session-a")
        after = sorted(path.relative_to(root) for path in root.rglob("*"))
        assert before == after
        assert event.event_type == "workspace.snapshot"
        assert event.payload["workspace_root"] == str(root.resolve())
        assert event.payload["git_available"] is False
        assert store.evidence_for_event(event.id)[0]["artifact_uri"] == "git://status"
    finally:
        store.close()
