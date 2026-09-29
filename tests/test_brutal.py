"""Brute-force and adversarial-input tests across every public entry point.

The goal is not to test known bugs but to throw malformed, boundary and
hostile input at the whole surface and assert the invariants always hold:
append-only integrity, workspace containment, provenance, and no silent
corruption.
"""

from __future__ import annotations

import random
import string

import pytest

from noname_harness.context import render_markdown
from noname_harness.curator import CuratorService
from noname_harness.models import EvidenceInput
from noname_harness.store import HarnessStore, WorkspaceBoundaryError
from noname_harness.taste import TasteService


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "brutal project")
    return store, root


RNG = random.Random(20260929)


def rand_text(n):
    alphabet = string.printable + "中文日本語 emoji 🎉 \x00 \t\r\n`\"'\\;DROP TABLE--"
    return "".join(RNG.choice(alphabet) for _ in range(n))


# --- malformed event input ---------------------------------------------------

def test_append_event_rejects_blank_ids_and_types(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        for bad in ("", "   ", "\t\n"):
            with pytest.raises(ValueError):
                store.append_event(bad, "note", {})
            with pytest.raises(ValueError):
                store.append_event("s", bad, {})
    finally:
        store.close()


def test_append_event_accepts_and_roundtrips_hostile_payloads(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        for i in range(50):
            payload = {"text": rand_text(RNG.randint(0, 200)), "n": i}
            evidence = [EvidenceInput(rand_text(RNG.randint(0, 100)), None)]
            event = store.append_event("s", "fuzz.type", payload, evidence)
            loaded = store.get_event(event.id)
            assert loaded.payload == payload
            assert store.evidence_for_event(event.id)[0]["content"] == evidence[0].content
        assert store.verify_integrity()["ok"] is True
    finally:
        store.close()


def test_evidence_span_offset_boundaries(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        # equal offsets are allowed (empty span), negative and inverted are not
        store.append_event("s", "ok", {}, [EvidenceInput("abc", None, 5, 5)])
        store.append_event("s", "ok", {}, [EvidenceInput("abc", None, 0, 0)])
        with pytest.raises(ValueError):
            store.append_event("s", "bad", {}, [EvidenceInput("abc", None, -1, 5)])
        with pytest.raises(ValueError):
            store.append_event("s", "bad", {}, [EvidenceInput("abc", None, 5, -1)])
        with pytest.raises(ValueError):
            store.append_event("s", "bad", {}, [EvidenceInput("abc", None, 9, 3)])
    finally:
        store.close()


# --- workspace containment under adversarial paths ---------------------------

def test_workspace_boundary_resists_traversal(tmp_path):
    store, root = make_store(tmp_path)
    try:
        evil_paths = [
            "../outside.txt",
            "../../etc/passwd",
            str(tmp_path / "sibling.txt"),
            "subdir/../../outside.txt",
            "/etc/passwd",
            "/",
            "..",
        ]
        for path in evil_paths:
            with pytest.raises(WorkspaceBoundaryError):
                store.append_event(
                    "s", "artifact.changed", {"path": "x"},
                    [EvidenceInput("secret", f"file://{path}")],
                )
        # A legitimate nested path inside the workspace is fine.
        (root / "sub").mkdir()
        store.append_event(
            "s", "artifact.changed", {"path": "sub/f.txt"},
            [EvidenceInput("ok", "file://sub/f.txt")],
        )
    finally:
        store.close()


# --- SQL-injection via search and text fields --------------------------------

def test_search_is_immune_to_sql_injection(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "note", {"text": "ordinary"})
        hostile = [
            "'; DROP TABLE session_events;--",
            "\" OR 1=1 --",
            "%' UNION SELECT * FROM project--",
            "\\'; DELETE FROM evidence_spans;--",
        ]
        for query in hostile:
            # Must not raise and must not damage the tables.
            store.search_events(query)
        assert store.verify_integrity()["ok"] is True
        assert len(store.list_events("s", limit=10)) == 1
    finally:
        store.close()


def test_fts_special_characters_do_not_crash_search(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "note", {"text": "findme token"})
        if store._fts_available:
            # FTS5 query syntax chars must not crash (fall back to LIKE).
            for query in ['"unclosed', "AND OR NOT", "col:name", "a* b*", "NEAR/0"]:
                store.search_events(query)
        assert store.search_events("findme")
    finally:
        store.close()


# --- proposal / review hostile input -----------------------------------------

def test_proposal_requires_nonempty_key_and_valid_confidence(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        event = store.append_event("s", "note", {})
        with pytest.raises(ValueError):
            store.create_proposal("high", "  ", {"text": "x"}, [event.id])
        for bad_conf in (-0.1, 1.1, float("nan"), float("inf"), float("-inf"), True, "0.5"):
            with pytest.raises(ValueError):
                store.create_proposal("high", "k", {"text": "x"}, [event.id], confidence=bad_conf)
    finally:
        store.close()


def test_proposal_rejects_unknown_source_events(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        with pytest.raises(KeyError):
            store.create_proposal("high", "k", {"text": "x"}, ["evt_does_not_exist"])
    finally:
        store.close()


def test_review_unknown_proposal_and_invalid_action(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        with pytest.raises(KeyError):
            store.review_proposal("prp_ghost", "accept", "user")
        event = store.append_event("s", "note", {})
        proposal = store.create_proposal("high", "k", {"text": "x"}, [event.id])
        with pytest.raises(ValueError):
            store.review_proposal(proposal["id"], "explode", "user")
        with pytest.raises(ValueError):
            store.review_proposal(proposal["id"], "accept", "   ")
        with pytest.raises(ValueError):
            store.review_proposal(proposal["id"], "edit", "user")  # needs edited_content
    finally:
        store.close()


# --- package assembly invariants under random state --------------------------

def test_random_workload_always_yields_consistent_package(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        taste = TasteService(store)
        curator = CuratorService(store)
        # Randomly append events, curate, review, and record tastes; the package
        # invariants must hold after every step.
        for step in range(40):
            kind = RNG.choice(
                ["constraint", "task", "failure", "change", "authored", "adopted", "snapshot"]
            )
            if kind == "constraint":
                store.append_event(
                    "s", "project.constraint",
                    {"key": f"k{step}", "content": {"text": rand_text(20)}},
                )
            elif kind == "task":
                store.append_event(
                    "s", "task.updated",
                    {"key": "current_task", "content": {"goal": rand_text(10), "next": rand_text(10)}},
                )
            elif kind == "failure":
                store.append_event("s", "test.failed", {"path": "t.py", "message": rand_text(30)})
            elif kind == "change":
                store.append_event("s", "artifact.changed", {"path": "a.py"}, [EvidenceInput(rand_text(20), None)])
            elif kind == "authored":
                taste.record_authored({"judgement": rand_text(15)})
            elif kind == "adopted":
                m = store.append_event("s", "model.completed", {"note": rand_text(10)})
                c = taste.propose_adopted({"judgement": rand_text(10)}, source_event_ids=[m.id])
                if RNG.random() < 0.7:
                    taste.review(c["id"], "adopt", "user")
            else:
                store.append_workspace_snapshot("s")

            curator.scan("s")
            for proposal in store.list_proposals(pending_only=True):
                action = RNG.choice(["accept", "reject", "defer"])
                store.review_proposal(proposal["id"], action, "user")

            package = store.assemble_context_package(f"task {step}", session_id="s")
            # Invariants after every single step:
            prov = package["provenance"]
            assert prov["event_ids"]
            # every taste in the package is active and resolvable
            for track in ("authored", "adopted"):
                for item in package["preference"]["tracks"][track]:
                    assert item["status"] == "active"
                    for eid in item["source_event_ids"]:
                        assert eid in prov["event_ids"]
            # rendering never crashes on arbitrary content
            md = render_markdown(package)
            assert "## Preference" in md
            assert store.verify_integrity()["ok"] is True
    finally:
        store.close()


def test_package_rejects_empty_task_and_bad_low_limit(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        with pytest.raises(ValueError):
            store.assemble_context_package("   ", session_id="s")
        with pytest.raises(ValueError):
            store.assemble_context_package("task", session_id="s", low_limit=0)
        with pytest.raises(ValueError):
            store.assemble_context_package("task", session_id="s", low_limit=-5)
    finally:
        store.close()


# --- taste hostile input ------------------------------------------------------

def test_taste_rejects_invalid_scope_track_status(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        taste = TasteService(store)
        with pytest.raises(ValueError):
            taste.record_authored({"j": "x"}, scope="organization")
        m = store.append_event("s", "model.completed", {"note": "x"})
        with pytest.raises(ValueError):
            taste.propose_adopted({"j": "x"}, scope="galaxy", source_event_ids=[m.id])
        with pytest.raises(ValueError):
            taste.by_status("exploded")
        with pytest.raises(KeyError):
            taste.get("tst_ghost")
        with pytest.raises(KeyError):
            taste.review("tst_ghost", "adopt", "user")
    finally:
        store.close()


def test_taste_review_rejects_blank_reviewer_and_bad_action(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        taste = TasteService(store)
        record = taste.record_authored({"j": "x"})
        with pytest.raises(ValueError):
            taste.review(record["id"], "nuke", "user")
        with pytest.raises(ValueError):
            taste.review(record["id"], "pause", "  ")
    finally:
        store.close()


# --- huge payloads ------------------------------------------------------------

def test_large_evidence_content_roundtrips(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        big = "x" * 1_000_000 + "中文" * 1000 + "```\n## heading\n"
        event = store.append_event("s", "big", {"size": len(big)}, [EvidenceInput(big, None)])
        loaded = store.evidence_for_event(event.id)[0]
        assert loaded["content"] == big
        package = store.assemble_context_package("task", session_id="s")
        # The evidence rides the package intact (the package is the handoff medium).
        low = {item["event_id"]: item for item in package["layers"]["low"]}
        assert low[event.id]["evidence"][0]["content"] == big
        # Markdown indents fenced evidence lines, so compare de-indented.
        md = render_markdown(package)
        deindented = "\n".join(line[4:] if line.startswith("    ") else line for line in md.splitlines())
        assert big in deindented  # fence chosen longer than any backtick run
        assert store.verify_integrity()["ok"] is True
    finally:
        store.close()
