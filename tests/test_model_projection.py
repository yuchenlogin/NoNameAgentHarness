"""Model-aware projection: the same facts, fitted to different target models.

The invariant under test: projecting for a constrained model may tighten the
low evidence window, but it must never alter reviewed canon, task state, taste
or provenance.  The facts are model-independent; only the evidence window is
shaped by the target's budget.
"""

from __future__ import annotations

import pytest

from noname_harness.models import ModelCapability, ModelProfile
from noname_harness.recipes import DEFAULT_RECIPES, Recipe, RoleSpec, resolve_recipe
from noname_harness.store import HarnessStore
from noname_harness.taste import TasteService


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "projection project")
    return store, root


def _seed(store):
    event = store.append_event(
        "s", "project.constraint", {"key": "boundary", "content": {"text": "stay in root"}}
    )
    proposal = store.create_proposal("high", "boundary", {"text": "stay in root"}, [event.id])
    store.review_proposal(proposal["id"], "accept", "user")
    task = store.append_event(
        "s", "task.updated", {"key": "current_task", "content": {"goal": "g", "next": "n"}}
    )
    tp = store.create_proposal("mid", "current_task", {"goal": "g", "next": "n"}, [task.id])
    store.review_proposal(tp["id"], "accept", "user")
    TasteService(store).record_authored({"judgement": "small steps"})
    # A run of work events so the low window has something to trim.
    for i in range(30):
        store.append_event("s", "artifact.changed", {"path": f"f{i}.py", "i": i})
        store.append_event("s", "test.failed", {"path": "t.py", "message": f"fail {i}"})


def test_projection_is_model_independent_in_facts(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        _seed(store)
        big = ModelProfile("big-model", ModelCapability(context_window=200_000), "high")
        small = ModelProfile("small-model", ModelCapability(context_window=8_000), "low")

        pkg_big = store.assemble_context_package("task", session_id="s", model=big)
        pkg_small = store.assemble_context_package("task", session_id="s", model=small)

        # Facts are identical regardless of target model.
        assert pkg_big["layers"]["high"] == pkg_small["layers"]["high"]
        assert pkg_big["layers"]["mid"] == pkg_small["layers"]["mid"]
        assert pkg_big["preference"] == pkg_small["preference"]

        # Only the low evidence window differs: the small model gets fewer.
        assert len(pkg_small["layers"]["low"]) < len(pkg_big["layers"]["low"])

        # The projection target is recorded for audit.
        assert pkg_big["assembly"]["projected_for_model"]["id"] == "big-model"
        assert pkg_small["assembly"]["projected_for_model"]["id"] == "small-model"
        assert pkg_small["assembly"]["low_event_limit"] < pkg_big["assembly"]["low_event_limit"]
    finally:
        store.close()


def test_provenance_is_preserved_for_constrained_projection(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        _seed(store)
        small = ModelProfile("tiny", ModelCapability(context_window=4_000), "low")
        package = store.assemble_context_package("task", session_id="s", model=small)
        # Even the tightest projection keeps provenance and a meaningful window.
        assert package["provenance"]["event_ids"]
        assert package["provenance"]["taste_ids"]
        assert package["assembly"]["low_event_limit"] >= 3
        # Reviewed state source events still resolve in provenance.
        for item in package["layers"]["high"] + package["layers"]["mid"]:
            for eid in item["source_event_ids"]:
                assert eid in package["provenance"]["event_ids"]
    finally:
        store.close()


def test_invalid_model_profile_rejected(tmp_path):
    with pytest.raises(ValueError):
        ModelProfile("  ", ModelCapability(), "medium")
    with pytest.raises(ValueError):
        ModelProfile("m", ModelCapability(), "extreme")
    with pytest.raises(ValueError):
        ModelProfile("m", ModelCapability(context_window=0), "medium")


def test_recipe_resolution_and_describe():
    recipe = resolve_recipe("code-change")
    assert recipe.id == "code-change-balanced"
    roles = [r["role"] for r in recipe.describe()["roles"]]
    assert roles == ["planner", "worker", "critic"]
    # The critic is declared independent from the worker.
    critic = recipe.describe()["roles"][2]
    assert critic["different_family_from"] == "worker"
    # Memory-write never lets one role both extract and check.
    mw = resolve_recipe("memory-write")
    assert mw.describe()["roles"][1]["different_family_from"] == "extractor"
    with pytest.raises(ValueError):
        resolve_recipe("not-a-task")


def test_recipe_is_recorded_in_package_and_ledger(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        _seed(store)
        package = store.assemble_context_package(
            "task", session_id="s", task_type="code-change"
        )
        assert package["assembly"]["recipe"]["id"] == "code-change-balanced"
        # The routing decision is in the ledger with its reason (task type).
        assembled = next(
            e for e in store.list_events("s", limit=20)
            if e.event_type == "context.assembled"
        )
        assert assembled.payload["recipe_id"] == "code-change-balanced"
        assert assembled.payload["task_type"] == "code-change"
    finally:
        store.close()


def test_custom_recipe_registry_can_override_defaults():
    custom = dict(DEFAULT_RECIPES)
    custom["question"] = Recipe(
        id="question-economy",
        task_type="question",
        roles=(RoleSpec("responder", "reasoning", "low"),),
    )
    assert resolve_recipe("question", registry=custom).id == "question-economy"
    # Defaults are untouched for other callers.
    assert resolve_recipe("question").id == "question-simple"
