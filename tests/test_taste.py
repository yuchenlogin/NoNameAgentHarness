"""Taste layer contracts: two tracks, reviewed activation, soft-influence only."""

from __future__ import annotations

import sqlite3

import pytest

from noname_harness.context import render_markdown
from noname_harness.models import EvidenceInput
from noname_harness.store import HarnessStore
from noname_harness.taste import TasteService


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "taste project")
    return store, root


def test_authored_taste_is_active_immediately_and_versioned(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        taste = TasteService(store)
        record = taste.record_authored(
            {"judgement": "prefer restrained, information-dense tools"},
            scope="user",
            reason="user wrote this down explicitly",
        )
        assert record["track"] == "authored"
        assert record["status"] == "active"
        assert record["origin"] == "authored"

        # Editing creates a new version that supersedes the old one.
        edited = taste.review(
            record["id"],
            "edit",
            "user",
            edited_content={"judgement": "prefer restrained tools, but accept complexity for research"},
        )
        assert edited["status"] == "active"
        assert edited["supersedes_id"] == record["id"]

        active = taste.active()
        assert len(active) == 1
        assert "accept complexity" in active[0]["content"]["judgement"]

        # Both versions remain in the append-only table.
        rows = store._connection.execute("SELECT COUNT(*) AS n FROM taste_records").fetchone()
        assert rows["n"] == 2
    finally:
        store.close()


def test_adopted_taste_requires_source_and_explicit_adoption(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        taste = TasteService(store)
        with pytest.raises(ValueError):
            taste.propose_adopted({"judgement": "no source"}, source_event_ids=[])

        moment = store.append_event(
            "session-a",
            "model.completed",
            {"note": "a surprisingly elegant solution"},
            [EvidenceInput("the model restructured the loop beautifully", None)],
        )
        candidate = taste.propose_adopted(
            {"judgement": "elegant restructuring over brute force"},
            source_event_ids=[moment.id],
            proposed_by="model",
            reason="this answer stood out",
        )
        assert candidate["track"] == "adopted"
        assert candidate["status"] == "candidate"
        assert candidate["source_event_ids"] == [moment.id]

        # A candidate is not active, so it must not appear in the active projection.
        assert taste.active() == []
        assert [item["id"] for item in taste.pending()] == [candidate["id"]]

        # Only adopted-track candidates can be adopted, and only by review.
        adopted = taste.review(candidate["id"], "adopt", "user")
        assert adopted["status"] == "active"
        assert adopted["track"] == "adopted"
        assert adopted["supersedes_id"] == candidate["id"]

        active = taste.active()
        assert len(active) == 1
        assert active[0]["track"] == "adopted"
        assert taste.pending() == []

        # A non-adopted record cannot be adopted twice.
        with pytest.raises(ValueError):
            taste.review(adopted["id"], "adopt", "user")
    finally:
        store.close()


def test_authored_taste_cannot_be_adopted(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        taste = TasteService(store)
        record = taste.record_authored({"judgement": "minimal chrome"})
        with pytest.raises(ValueError):
            taste.review(record["id"], "adopt", "user")
    finally:
        store.close()


def test_taste_lifecycle_pause_resume_retire(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        taste = TasteService(store)
        record = taste.record_authored({"judgement": "dark themes only"})

        paused = taste.review(record["id"], "pause", "user")
        assert paused["status"] == "paused"
        assert taste.active() == []

        resumed = taste.review(paused["id"], "resume", "user")
        assert resumed["status"] == "active"
        assert len(taste.active()) == 1

        retired = taste.review(resumed["id"], "retire", "user")
        assert retired["status"] == "retired"
        assert taste.active() == []

        # The whole lineage is preserved.
        rows = store._connection.execute("SELECT COUNT(*) AS n FROM taste_records").fetchone()
        assert rows["n"] == 4
    finally:
        store.close()


def test_taste_records_are_append_only(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        taste = TasteService(store)
        record = taste.record_authored({"judgement": "immutable"})
        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute(
                "UPDATE taste_records SET status = 'retired' WHERE id = ?", (record["id"],)
            )
        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute("DELETE FROM taste_records WHERE id = ?", (record["id"],))
    finally:
        store.close()


def test_taste_scope_filters_projection(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        taste = TasteService(store)
        taste.record_authored({"judgement": "global user vibe"}, scope="user")
        taste.record_authored({"judgement": "this project likes terse output"}, scope="project")
        assert len(taste.active()) == 2
        assert len(taste.active(scope="user")) == 1
        assert len(taste.active(scope="project")) == 1
    finally:
        store.close()


def test_context_package_has_independent_soft_influence_preference_section(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        taste = TasteService(store)
        taste.record_authored({"judgement": "restraint over decoration"}, scope="user")

        moment = store.append_event(
            "session-a", "model.completed", {"note": "nice"},
        )
        candidate = taste.propose_adopted(
            {"judgement": "adopted elegance"}, source_event_ids=[moment.id]
        )
        taste.review(candidate["id"], "adopt", "user")

        package = store.assemble_context_package("continue", session_id="session-a")

        # Preference is its own section, clearly marked as soft influence.
        assert "preference" in package
        preference = package["preference"]
        assert preference["influence"] == "soft"
        assert "not" in preference["note"].lower() or "must not" in preference["note"]
        assert len(preference["tracks"]["authored"]) == 1
        assert len(preference["tracks"]["adopted"]) == 1

        # Taste ids are part of provenance so the projection is traceable.
        assert len(package["provenance"]["taste_ids"]) == 2

        # Crucially, taste must NOT leak into the factual layers.
        factual_text = str(package["layers"])
        assert "restraint over decoration" not in factual_text
        assert "adopted elegance" not in factual_text

        # Markdown renders the section and labels it as not-a-fact.
        markdown = render_markdown(package)
        assert "soft influence" in markdown
        assert "not fact" in markdown
        assert "restraint over decoration" in markdown
        assert "adopted elegance" in markdown
        # The taste section appears before next-step candidates, after layers.
        assert markdown.index("## Preference") < markdown.index("## Next-step")
    finally:
        store.close()


def test_context_package_without_taste_renders_empty_preference(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        package = store.assemble_context_package("continue", session_id="session-a")
        assert package["preference"]["tracks"]["authored"] == []
        assert package["preference"]["tracks"]["adopted"] == []
        markdown = render_markdown(package)
        assert "_No active taste yet._" in markdown
    finally:
        store.close()


def test_taste_events_are_logged_to_ledger(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        taste = TasteService(store)
        moment = store.append_event("session-a", "model.completed", {"note": "x"})
        candidate = taste.propose_adopted(
            {"judgement": "y"}, source_event_ids=[moment.id]
        )
        taste.review(candidate["id"], "adopt", "user")
        event_types = [event.event_type for event in store.list_events("session-a", limit=20)]
        assert "taste.proposed" in event_types
        assert "taste.reviewed" in event_types
    finally:
        store.close()
