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
