"""Pin the cross-model invariants the reviewers found were violated."""

from __future__ import annotations

import pytest

from noname_harness.cli import main
from noname_harness.models import ModelCapability, ModelProfile
from noname_harness.store import HarnessStore


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "invariant project")
    return store, root


def _seed_untested_change(store):
    """An artifact change 25 events ago with no later test -- the reviewer's case."""

    store.append_event("s", "artifact.changed", {"path": "old.py"})
    for i in range(25):
        store.append_event("s", "artifact.changed", {"path": f"f{i}.py"})


def test_next_steps_are_model_independent(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        _seed_untested_change(store)
        big = ModelProfile("big", ModelCapability(context_window=200_000), "high")
        small = ModelProfile("small", ModelCapability(context_window=8_000), "low")
        pkg_big = store.assemble_context_package("task", session_id="s", model=big)
        pkg_small = store.assemble_context_package("task", session_id="s", model=small)
        # The advice handed to the next agent must not depend on the target model.
        assert pkg_big["next_step_candidates"] == pkg_small["next_step_candidates"]
        texts = {c["text"] for c in pkg_big["next_step_candidates"]}
        assert "运行覆盖 old.py 的相关测试" in texts
    finally:
        store.close()


def test_state_provenance_is_model_independent(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        event = store.append_event(
            "s", "project.constraint", {"key": "b", "content": {"text": "stay"}}
        )
        proposal = store.create_proposal("high", "b", {"text": "stay"}, [event.id])
        store.review_proposal(proposal["id"], "accept", "user")
        _seed_untested_change(store)
        big = ModelProfile("big", ModelCapability(context_window=200_000), "high")
        small = ModelProfile("small", ModelCapability(context_window=8_000), "low")
        pkg_big = store.assemble_context_package("task", session_id="s", model=big)
        pkg_small = store.assemble_context_package("task", session_id="s", model=small)
        # State provenance (reviewed canon etc.) is identical for every model.
        assert pkg_big["provenance"]["state_event_ids"] == pkg_small["provenance"]["state_event_ids"]
        assert event.id in pkg_small["provenance"]["state_event_ids"]
        # Evidence provenance may differ (it covers the carried low window), but
        # every state event id is present in both full provenance blocks.
        for eid in pkg_big["provenance"]["state_event_ids"]:
            assert eid in pkg_small["provenance"]["event_ids"]
    finally:
        store.close()


def test_projection_only_tightens_never_widens(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        for i in range(40):
            store.append_event("s", "artifact.changed", {"path": f"f{i}.py"})
        # A high budget must not widen beyond the caller's requested limit.
        high = ModelProfile("big", ModelCapability(context_window=200_000), "high")
        pkg = store.assemble_context_package("task", session_id="s", low_limit=5, model=high)
        assert pkg["assembly"]["low_event_limit"] <= 5
    finally:
        store.close()


def test_explicit_small_limit_is_not_inflated_by_floor(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        for i in range(40):
            store.append_event("s", "artifact.changed", {"path": f"f{i}.py"})
        small = ModelProfile("s", ModelCapability(context_window=8_000), "low")
        # An explicit --low-limit 2 must not be silently raised to the floor of 3.
        pkg = store.assemble_context_package("task", session_id="s", low_limit=2, model=small)
        # floor = min(3, requested) = 2; low budget quarters 2 -> max(2, 0) = 2.
        assert pkg["assembly"]["low_event_limit"] == 2
    finally:
        store.close()


def test_cli_stray_budget_flag_is_rejected(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    db = root / ".noname" / "harness.db"
    assert main(["init", "--db", str(db), "--root", str(root)]) == 0
    capsys.readouterr()
    # --budget without --model-id must fail fast, not be silently dropped.
    result = main([
        "package", "--db", str(db), "--session", "s", "--task", "t",
        "--budget", "low",
    ])
    assert result == 2
    err = capsys.readouterr().err
    assert "require --model-id" in err
