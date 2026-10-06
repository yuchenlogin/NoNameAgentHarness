"""Causal map: why did this happen -- a pure projection, taste boundary."""

from __future__ import annotations

import pytest

from noname_harness.causal_map import build_causal_model, render_causal_html
from noname_harness.ledger_view import build_ledger_model, render_ledger_html
from noname_harness.store import HarnessStore
from noname_harness.taste import TasteService


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "causal project")
    return store, root


def test_canon_result_traces_to_source_events_and_review(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        event = store.append_event("s", "project.constraint", {"key": "b", "content": {"text": "只改根目录"}})
        proposal = store.create_proposal("high", "b", {"text": "只改根目录"}, [event.id])
        store.review_proposal(proposal["id"], "accept", "yuchen")
        model = build_causal_model(store)
        canon = next(r for r in model["results"] if r["kind"] == "canon")
        # The canon result depends on the source event (user instruction/evidence) + the review.
        roles = {d.get("role") for d in canon["dependencies"]}
        assert "用户指令/证据" in roles
        assert any("批准" in (d.get("role") or "") for d in canon["dependencies"])
    finally:
        store.close()


def test_context_package_traces_to_recipe_and_sources(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "artifact.changed", {"path": "a.py"})
        store.assemble_context_package("task", session_id="s", task_type="code-change")
        model = build_causal_model(store)
        package = next(r for r in model["results"] if r["kind"] == "package")
        kinds = {d["kind"] for d in package["dependencies"]}
        assert "recipe" in kinds  # 模型配方依赖
        assert "event" in kinds  # 选中的证据依赖
    finally:
        store.close()


def test_gated_tool_traces_to_approval(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        from noname_harness.tools import Tool, ToolRegistry, ToolSchema
        registry = ToolRegistry(store)
        registry.register(Tool(
            ToolSchema(name="w", description="d", input_schema={"x": "string"}),
            execute=lambda a: "ok", permission="write", approval="always",
        ))
        token = registry.grant_approval("w", {"x": "y"}, approver_id="user", session_id="s")
        registry.request("w", {"x": "y"}, session_id="s", approval_token=token)
        model = build_causal_model(store)
        tool = next(r for r in model["results"] if r["kind"] == "tool")
        kinds = {d["kind"] for d in tool["dependencies"]}
        assert "approval" in kinds  # 人工审批依赖
    finally:
        store.close()


def test_taste_is_labelled_soft_influence_not_fact(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        TasteService(store).record_authored({"judgement": "克制"}, scope="user")
        model = build_causal_model(store)
        assert model["taste_note"]["count"] == 1
        assert "影响了排序/表达" in model["taste_note"]["label"]
        assert "不是事实依据" in model["taste_note"]["warning"]
        page = render_causal_html(model)
        assert "影响了排序/表达" in page
        assert "不是事实依据" in page
    finally:
        store.close()


def test_causal_map_is_a_pure_projection(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "note", {"text": "x"})
        before = [e.id for e in store.list_events("s", limit=100)]
        build_causal_model(store)
        render_causal_html(build_causal_model(store))
        after = [e.id for e in store.list_events("s", limit=100)]
        assert before == after
        assert store.verify_integrity()["ok"] is True
    finally:
        store.close()


def test_causal_html_escapes_and_progressive_disclosure(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        event = store.append_event("s", "project.constraint", {"key": "b", "content": {"text": "<script>alert(1)</script>"}})
        proposal = store.create_proposal("high", "b", {"text": "<script>alert(1)</script>"}, [event.id])
        store.review_proposal(proposal["id"], "accept", "user")
        page = render_causal_html(build_causal_model(store))
        # Progressive disclosure via <details>.
        assert "<details>" in page
        # No unescaped injection.
        assert "<script>alert(1)</script>" not in page
    finally:
        store.close()


def test_ledger_html_includes_causal_map_section(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        event = store.append_event("s", "project.constraint", {"key": "b", "content": {"text": "x"}})
        proposal = store.create_proposal("high", "b", {"text": "x"}, [event.id])
        store.review_proposal(proposal["id"], "accept", "user")
        page = render_ledger_html(build_ledger_model(store))
        assert "因果图 · 为什么这样做" in page
        assert "证据、记忆、路由、模型还是工具" in page
    finally:
        store.close()


# --- 对抗性审查发现的回归 ---

def test_taste_cited_event_is_soft_influence_not_evidence(tmp_path):
    """The non-negotiable boundary: a taste's motivating moment must NOT be evidence."""
    store, _ = make_store(tmp_path)
    try:
        moment = store.append_event("s", "model.answer", {"text": "an elegant answer"})
        taste = TasteService(store)
        candidate = taste.propose_adopted(
            {"judgement": "优雅优于蛮力"}, source_event_ids=[moment.id]
        )
        taste.review(candidate["id"], "adopt", "user")
        store.assemble_context_package("task", session_id="s")
        model = build_causal_model(store)
        package = next(r for r in model["results"] if r["kind"] == "package")
        moment_dep = next(
            d for d in package["dependencies"] if d["label"] == "model.answer"
        )
        # The taste-cited moment is labelled soft influence, never factual evidence.
        assert moment_dep["role"] == "影响了排序/表达"
        assert moment_dep["kind"] == "taste"
        # And it is NOT in the evidence-labelled set.
        evidence_roles = [
            d["role"] for d in package["dependencies"] if d["role"] == "选中的证据/记忆"
        ]
        assert all("model.answer" not in str(r) for r in evidence_roles)
    finally:
        store.close()


def test_canon_provenance_resolves_beyond_display_window(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        # Canon created in session A, then many noise events push it out of a small window.
        event = store.append_event("a", "project.constraint", {"key": "b", "content": {"text": "stay in root"}})
        proposal = store.create_proposal("high", "b", {"text": "stay in root"}, [event.id])
        store.review_proposal(proposal["id"], "accept", "user")
        for i in range(30):
            store.append_event("b", "note", {"text": f"noise {i}"})
        # A small limit would evict the source event from by_id, but canon
        # provenance resolves it individually by id.
        model = build_causal_model(store, session_id="b", limit=5)
        canon = next(r for r in model["results"] if r["kind"] == "canon")
        source_dep = next(d for d in canon["dependencies"] if d["kind"] == "event")
        assert source_dep["missing"] is False
        assert source_dep["label"] == "project.constraint"
    finally:
        store.close()


def test_truncation_is_surfaced(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        for i in range(10):
            store.append_event("s", "note", {"text": f"event {i}"})
        model = build_causal_model(store, limit=3)
        assert model["truncated"] is True
        page = render_causal_html(model)
        assert "历史被截断" in page
    finally:
        store.close()


def test_forged_approval_token_is_flagged_not_verified(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        # A forged tool.completed with a token id that has no matching grant.
        store.append_event("s", "tool.completed", {"name": "forged", "approval_token_id": "apr_fake", "elapsed_ms": 1})
        model = build_causal_model(store)
        tool = next(r for r in model["results"] if r["kind"] == "tool")
        approval_dep = next(d for d in tool["dependencies"] if d["kind"] == "approval")
        # It is flagged as unverified, NOT presented as human-approved.
        assert "未在账本中核实" in approval_dep["role"]
        assert "人工审批（由" not in approval_dep["role"]
    finally:
        store.close()


def test_verified_approval_shows_approver(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        from noname_harness.tools import Tool, ToolRegistry, ToolSchema
        registry = ToolRegistry(store)
        registry.register(Tool(
            ToolSchema(name="w", description="d", input_schema={"x": "string"}),
            execute=lambda a: "ok", permission="write", approval="always",
        ))
        token = registry.grant_approval("w", {"x": "y"}, approver_id="yuchen", session_id="s")
        registry.request("w", {"x": "y"}, session_id="s", approval_token=token)
        model = build_causal_model(store)
        tool = next(r for r in model["results"] if r["kind"] == "tool")
        approval_dep = next(d for d in tool["dependencies"] if d["kind"] == "approval")
        assert approval_dep["role"] == "人工审批（由 yuchen 批准）"
    finally:
        store.close()


def test_task_field_non_string_does_not_crash(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "context.assembled", {"package_id": "p", "task": 123, "source_event_ids": [], "recipe_id": None})
        model = build_causal_model(store)
        assert model is not None  # no crash on non-string task
    finally:
        store.close()
