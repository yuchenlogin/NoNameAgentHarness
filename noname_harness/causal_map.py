"""Causal map: why did this happen -- a projection, never a new fact.

The causal map answers "为什么这样做": pick a result, and see what it depends
on -- the user instruction, the canon it cites, the evidence it selected, the
model recipe and route reason, the tool approval behind it.  It lets a person
tell whether an error came from evidence, memory, routing, the model, or a
tool.

Everything here is derived from the append-only event stream and existing
provenance (source_event_ids, origin_proposal_id, approved_by, recipe ids) --
it can be rebuilt at any time and stores nothing new.

**Taste boundary (non-negotiable):**  taste may appear in the causal map ONLY
labelled as "影响了排序/表达" (a soft influence on ordering/expression) --
never as a factual justification.  This is what lets the map distinguish "an
attitude shaped the phrasing" from "a fact supported the conclusion".
"""

from __future__ import annotations

import html
import json
from typing import Any

from .store import HarnessStore
from .taste import TasteService


def build_causal_model(store: HarnessStore, *, session_id: str | None = None, limit: int = 100) -> dict[str, Any]:
    """Assemble the causal-map view-model from the store (a pure projection)."""

    events = store.list_events(session_id=session_id, limit=limit)
    by_id = {event.id: event for event in events}

    def event_label(event_id: str) -> dict[str, Any]:
        event = by_id.get(event_id)
        if event is None:
            return {"id": event_id[:12], "label": event_id[:12], "kind": "event", "missing": True}
        payload_text = json.dumps(event.payload, ensure_ascii=False, default=str)
        if len(payload_text) > 80:
            payload_text = payload_text[:80] + "…"
        return {
            "id": event.id[:12],
            "label": f"{event.event_type}",
            "detail": payload_text,
            "kind": "event",
            "missing": False,
        }

    results: list[dict[str, Any]] = []

    # Results: reviewed canon/task-state revisions (why is this canon true now?).
    for revision in store.active_state("high") + store.active_state("mid"):
        dependencies = [
            {**event_label(event_id), "role": "用户指令/证据"}
            for event_id in revision["source_event_ids"]
        ]
        dependencies.append(
            {
                "id": revision.get("origin_proposal_id") or "",
                "label": "审核提案",
                "kind": "review",
                "role": f"由 {revision.get('approved_by', '?')} 批准",
            }
        )
        results.append(
            {
                "id": revision["id"],
                "title": f"{revision['layer']}/{revision['logical_key']}",
                "kind": "canon",
                "summary": json.dumps(revision["content"], ensure_ascii=False, default=str)[:100],
                "dependencies": dependencies,
            }
        )

    # Results: context assemblies (why was this context assembled this way?).
    for event in events:
        if event.event_type != "context.assembled":
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        dependencies = [
            {**event_label(event_id), "role": "选中的证据/记忆"}
            for event_id in payload.get("source_event_ids", [])
        ]
        if payload.get("recipe_id"):
            dependencies.append(
                {
                    "id": payload["recipe_id"],
                    "label": f"模型配方 {payload['recipe_id']}",
                    "kind": "recipe",
                    "role": "配方与路由理由",
                }
            )
        if payload.get("model_id"):
            dependencies.append(
                {"id": payload["model_id"], "label": f"模型 {payload['model_id']}", "kind": "model", "role": "目标模型"}
            )
        results.append(
            {
                "id": payload.get("package_id", event.id),
                "title": f"上下文包 · {payload.get('task', '')[:40]}",
                "kind": "package",
                "summary": f"配方 {payload.get('recipe_id') or '无'}",
                "dependencies": dependencies,
            }
        )

    # Results: gated tool executions (why did this tool run?).
    tool_events = [e for e in events if e.event_type.startswith("tool.")]
    for event in tool_events:
        if event.event_type != "tool.completed":
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        dependencies = []
        if payload.get("approval_token_id"):
            dependencies.append(
                {
                    "id": payload["approval_token_id"],
                    "label": "审批令牌",
                    "kind": "approval",
                    "role": "人工审批",
                }
            )
        dependencies.append(
            {"id": event.id[:12], "label": f"工具 {payload.get('name', '?')}", "kind": "tool", "role": "工具结果"}
        )
        results.append(
            {
                "id": event.id,
                "title": f"工具执行 · {payload.get('name', '?')}",
                "kind": "tool",
                "summary": f"{payload.get('elapsed_ms', '?')}ms",
                "dependencies": dependencies,
            }
        )

    # Taste (soft influence, never a factual justification).
    active_taste = TasteService(store).active()
    taste_note = {
        "count": len(active_taste),
        "label": "影响了排序/表达",
        "warning": "品味只影响态度层（排序/表达/取舍），不是事实依据",
    }

    return {
        "session_id": session_id,
        "results": results,
        "taste_note": taste_note,
        "counts": {"results": len(results), "events": len(events)},
    }


def render_causal_html(model: dict[str, Any]) -> str:
    """Render the causal map as an HTML fragment (progressive disclosure)."""

    def esc(value: Any) -> str:
        return html.escape(str(value))

    rows = []
    for result in model["results"]:
        dep_items = []
        for dep in result["dependencies"]:
            role = html.escape(dep.get("role", ""), quote=True)
            label = html.escape(dep.get("label", ""), quote=True)
            detail = html.escape(dep.get("detail", ""), quote=True)
            dep_items.append(
                f'<li class="dep dep-{dep["kind"]}">'
                f'<span class="dep-role">{role}</span> '
                f'<span class="dep-label">{label}</span>'
                f'<span class="dep-detail">{detail}</span>'
                f"</li>"
            )
        dep_html = "".join(dep_items) or '<li class="dep"><span class="meta">无显式依赖</span></li>'
        rows.append(
            f'<li class="causal-node">'
            f"<details>"
            f'<summary><span class="badge kind-{result["kind"]}">{html.escape(result["kind"], quote=True)}</span> '
            f"<strong>{esc(result['title'])}</strong> "
            f'<span class="meta">{esc(result["summary"])}</span></summary>'
            f'<ul class="deps">{dep_html}</ul>'
            f"</details>"
            f"</li>"
        )
    taste = model["taste_note"]
    taste_banner = (
        f'<p class="taste-note">品味（{taste["count"]} 条活跃）：<strong>{html.escape(taste["label"])}</strong>'
        f" · {html.escape(taste['warning'])}</p>"
    )
    return (
        '<section class="causal-map">'
        "<h2>因果图 · 为什么这样做</h2>"
        '<p class="meta">点击一个结果，看它依赖什么——识别错误来自证据、记忆、路由、模型还是工具。'
        "品味只标注影响，不是事实依据。</p>"
        f"{taste_banner}"
        f'<ul class="causal-list">{"".join(rows) or "<p class=empty>暂无可追溯的结果</p>"}</ul>'
        "</section>"
    )
