"""End-to-end rebirth: can a fresh session take over from a handoff package?

This is the vertical slice the whole harness exists to prove.  It simulates
the documented rebirth flow:

1. an old session records an algorithm change, a failing test and progress;
2. a handoff package is assembled;
3. a *fresh* session is given only that package plus one new task sentence;
4. the new agent must be able to answer: what is the goal, what was tried,
   where is the problem, what is next, and which boundaries must not be
   crossed.

The "new agent" here is a reader over the package, not an LLM.  That keeps
the test deterministic while still checking that the package carries every
fact a real model would need.
"""

from __future__ import annotations

from noname_harness.context import render_markdown
from noname_harness.models import EvidenceInput
from noname_harness.store import HarnessStore
from noname_harness.taste import TasteService


def make_old_session(tmp_path):
    """Build the old session's evidence and reviewed durable state."""

    root = tmp_path / "project"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "algorithm.py").write_text("def count(xs):\n    return len(xs) + 1\n")
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "rebirth project")

    # High-layer canon: the project boundary, reviewed and accepted.
    boundary_event = store.append_event(
        "old",
        "project.constraint",
        {"key": "workspace_boundary", "content": {"text": "只能修改项目根目录内的内容"}},
    )
    boundary = store.create_proposal(
        "high",
        "workspace_boundary",
        {"text": "只能修改项目根目录内的内容"},
        [boundary_event.id],
        kind="constraint",
    )
    store.review_proposal(boundary["id"], "accept", "user")

    # Mid-layer task state: the current goal and the next step, accepted.
    task_event = store.append_event(
        "old",
        "task.updated",
        {
            "key": "current_task",
            "content": {
                "goal": "让 count 对空列表返回 0",
                "progress": "已复现失败",
                "next": "检查 algorithm.py 的 +1 偏移",
            },
        },
    )
    task = store.create_proposal(
        "mid",
        "current_task",
        {"goal": "让 count 对空列表返回 0", "progress": "已复现失败", "next": "检查 algorithm.py 的 +1 偏移"},
        [task_event.id],
    )
    store.review_proposal(task["id"], "accept", "user")

    # Low-layer work evidence: the change and the failing test.
    store.append_event(
        "old",
        "artifact.changed",
        {"path": "src/algorithm.py", "change": "尝试让计数更稳健"},
        [EvidenceInput("+    return len(xs) + 1", "file://src/algorithm.py")],
    )
    store.append_event(
        "old",
        "test.failed",
        {"path": "tests/test_algorithm.py", "message": "count([]) 期望 0，得到 1"},
        [EvidenceInput("assert count([]) == 0  # got 1", "file://tests/test_algorithm.py")],
    )

    # A taste the user holds, which must ride along as soft influence only.
    TasteService(store).record_authored(
        {"judgement": "改动要小而可回退"}, scope="project"
    )
    return store, root


def test_handoff_package_lets_a_fresh_session_take_over(tmp_path):
    store, _ = make_old_session(tmp_path)
    try:
        package = store.assemble_context_package(
            "继续修复 count 的测试失败", session_id="old"
        )
        md = render_markdown(package)

        # 1. What is the goal?  (mid layer)
        mid = {item["logical_key"]: item for item in package["layers"]["mid"]}
        assert mid["current_task"]["content"]["goal"] == "让 count 对空列表返回 0"

        # 2. What was tried?  (low layer evidence)
        low_types = [item["event_type"] for item in package["layers"]["low"]]
        assert "artifact.changed" in low_types
        assert "test.failed" in low_types

        # 3. Where is the problem?  (failing test evidence survives in markdown)
        assert "count([]) 期望 0，得到 1" in md
        assert "len(xs) + 1" in md

        # 4. What is next?  (advisory next-step candidates carry provenance)
        next_texts = {c["text"] for c in package["next_step_candidates"]}
        assert "检查 algorithm.py 的 +1 偏移" in next_texts
        for candidate in package["next_step_candidates"]:
            assert candidate["source_event_ids"], "next steps must cite sources"

        # 5. Which boundaries must not be crossed?  (canon + guardrails)
        high = {item["logical_key"]: item for item in package["layers"]["high"]}
        assert high["workspace_boundary"]["content"]["text"] == "只能修改项目根目录内的内容"
        assert package["guardrails"]["workspace_root"]
        assert "outside the configured workspace root" in package["guardrails"]["workspace_policy"]

        # Taste rides along, but only as soft influence, never as fact.
        assert package["preference"]["influence"] == "soft"
        authored = package["preference"]["tracks"]["authored"]
        assert authored[0]["content"]["judgement"] == "改动要小而可回退"
        assert "改动要小而可回退" not in str(package["layers"])

        # The fresh session needs nothing else: provenance is complete.
        assert package["provenance"]["event_ids"]
        assert package["provenance"]["evidence_ids"]
    finally:
        store.close()


def test_rebirth_session_appends_new_evidence_without_reexplaining(tmp_path):
    """A new session continues the ledger instead of restarting it."""

    store, _ = make_old_session(tmp_path)
    try:
        package = store.assemble_context_package("继续修复", session_id="old")
        old_event_count = len(store.list_events("old", limit=100))

        # The fresh session reads the package, fixes the bug and records it.
        store.append_event(
            "new",
            "artifact.changed",
            {"path": "src/algorithm.py", "change": "去掉 +1 偏移"},
            [EvidenceInput("-    return len(xs) + 1\n+    return len(xs)", "file://src/algorithm.py")],
        )
        store.append_event(
            "new",
            "test.passed",
            {"path": "tests/test_algorithm.py", "message": "count([]) == 0 通过"},
        )

        # The old session's evidence is untouched and still intact.
        assert len(store.list_events("old", limit=100)) == old_event_count
        # The new session only holds its own two events; continuity came from
        # the package, not from re-recording history.
        new_types = [e.event_type for e in store.list_events("new", limit=10)]
        assert new_types == ["test.passed", "artifact.changed"]
        # Integrity across both sessions still verifies.
        assert store.verify_integrity()["ok"] is True
    finally:
        store.close()
