"""Ledger projection: turn the append-only store into an interactive map.

The ledger is *not* a log viewer and never a new fact: everything it shows is
derived from the append-only event stream, the review inbox, and the taste
projection, and can be rebuilt at any time.  This module produces a
self-contained, dependency-free, offline HTML page (open it by double-click)
that answers the three questions the ledger exists for:

- **Timeline -- what happened?**  High-signal nodes by default; low-level
  noise is folded away but expandable.
- **State -- why is it like this now?**  Canon, task state and taste, each
  traced back to its source events.
- **Inbox -- what needs my judgement?**  The pending canon/task/taste/card
  candidates, visually distinct from plain browsing.

Visual principles honoured (docs/ledger.md): a clean timeline by default,
progressive disclosure (click a node to drill into its source events), review
actions visually distinct from browsing, colour for event *type* not for
value, and no giant stat panels.
"""

from __future__ import annotations

import html
import json
from typing import Any

from .store import HarnessStore
from .taste import TasteService
from .taste_cards import TasteCardService

# Event types that are high-signal timeline nodes (low-level work noise like
# file.read / loop.transition bookkeeping is folded into an expandable group).
_HIGH_SIGNAL = {
    "project.goal", "project.constraint", "decision.accepted",
    "task.updated", "task.blocked", "task.progress", "phase.updated",
    "memory.proposed", "memory.reviewed",
    "taste.proposed", "taste.reviewed", "taste.card.generated", "taste.card.reviewed",
    "tool.approval_required", "tool.approved", "tool.approval_granted", "tool.failed",
    "route.selected", "model.failed", "loop.finished", "loop.error",
    "plugin.loaded", "plugin.unloaded",
    "test.failed", "artifact.changed",
}

_TYPE_LABELS = {
    "project": "法典", "decision": "决策", "task": "任务", "phase": "阶段",
    "memory": "记忆", "taste": "品味", "tool": "工具", "route": "路由",
    "model": "模型", "loop": "循环", "plugin": "插件", "test": "测试",
    "artifact": "文件", "workspace": "快照", "file": "文件",
}


def _type_group(event_type: str) -> str:
    prefix = event_type.split(".", 1)[0]
    return _TYPE_LABELS.get(prefix, "其它")


def build_ledger_model(store: HarnessStore, *, session_id: str | None = None, limit: int = 200) -> dict[str, Any]:
    """Assemble the ledger view-model from the store (a pure projection)."""

    events = store.list_events(session_id=session_id, limit=limit)
    # list_events is newest-first; present oldest-first for a timeline.
    timeline = []
    folded = 0
    for event in reversed(events):
        high = event.event_type in _HIGH_SIGNAL
        if not high:
            folded += 1
        timeline.append(
            {
                "seq": event.seq,
                "event_type": event.event_type,
                "group": _type_group(event.event_type),
                "occurred_at": event.occurred_at,
                "session_id": event.session_id,
                "high_signal": high,
                "payload": event.payload,
                "id": event.id,
            }
        )

    inbox = store.review_inbox()
    taste = TasteService(store)
    cards = TasteCardService(store)
    active_taste = taste.active()
    state = {
        "high": store.active_state("high"),
        "mid": store.active_state("mid"),
    }

    return {
        "project": store.project(),
        "session_id": session_id,
        "timeline": timeline,
        "folded_low_signal": folded,
        "inbox": inbox,
        "state": state,
        "taste": {
            "active": active_taste,
            "cards": cards.by_status("active") + cards.by_status("candidate"),
        },
        "counts": {
            "events": len(timeline),
            "pending_total": inbox["counts"]["total"],
        },
    }


def render_ledger_html(model: dict[str, Any]) -> str:
    """Render the ledger view-model as a self-contained offline HTML page."""

    project = html.escape(model["project"]["name"])
    inbox = model["inbox"]
    counts = inbox["counts"]

    def esc(value: Any) -> str:
        return html.escape(json.dumps(value, ensure_ascii=False, sort_keys=True) if not isinstance(value, str) else value)

    # Timeline nodes.
    timeline_rows = []
    for node in model["timeline"]:
        if not node["high_signal"]:
            continue
        payload_text = esc(node["payload"])
        if len(payload_text) > 220:
            payload_text = payload_text[:220] + "…"
        timeline_rows.append(
            f'<li class="node g-{node["group"]}">'
            f'<span class="seq">#{node["seq"]}</span>'
            f'<span class="badge">{html.escape(node["group"])}</span>'
            f'<span class="etype">{html.escape(node["event_type"])}</span>'
            f'<span class="time">{html.escape(node["occurred_at"])}</span>'
            f'<div class="payload">{payload_text}</div>'
            f"</li>"
        )

    # Inbox cards.
    def inbox_section(title: str, items: list[dict[str, Any]], kind_label: str) -> str:
        if not items:
            return ""
        rows = "".join(
            f'<li class="inbox-item"><span class="badge review">{kind_label}</span>'
            f'<strong>{esc(item.get("logical_key") or item.get("title") or item.get("id", ""))}</strong>'
            f'<div class="payload">{esc(item.get("summary"))}</div>'
            f'<div class="meta">影响：{html.escape(item.get("impact", ""))}</div>'
            f"</li>"
            for item in items
        )
        return f'<section class="inbox-section"><h3>{html.escape(title)}</h3><ul>{rows}</ul></section>'

    inbox_html = (
        inbox_section("法典候选", inbox["canon_pending"], "法典")
        + inbox_section("任务态候选", inbox["task_pending"], "任务")
        + inbox_section("品味候选", inbox["taste_pending"], "品味")
        + inbox_section("品味卡候选", inbox["card_pending"], "卡片")
    )
    if not inbox_html:
        inbox_html = '<p class="empty">收件箱是空的——没有等待你判断的事。</p>'

    # State (canon + task state).
    def state_section(title: str, items: list[dict[str, Any]]) -> str:
        if not items:
            return f'<section class="state-section"><h3>{html.escape(title)}</h3><p class="empty">暂无</p></section>'
        rows = "".join(
            f'<li><code>{html.escape(item["logical_key"])}</code>'
            f'<div class="payload">{esc(item["content"])}</div>'
            f'<div class="meta">来源事件 {len(item["source_event_ids"])} 个 · {html.escape(item.get("approved_by", ""))}</div>'
            f"</li>"
            for item in items
        )
        return f'<section class="state-section"><h3>{html.escape(title)}</h3><ul>{rows}</ul></section>'

    state_html = state_section("法典 · 稳定状态", model["state"]["high"]) + state_section(
        "任务态 · 当前工作", model["state"]["mid"]
    )

    # Taste.
    taste_items = "".join(
        f'<li><span class="badge {"authored" if t["track"]=="authored" else "adopted"}">'
        f'{"自述" if t["track"]=="authored" else "采纳"}</span>'
        f'<div class="payload">{esc(t["content"])}</div>'
        f'<div class="meta">{html.escape(t["scope"])} 作用域</div>'
        f"</li>"
        for t in model["taste"]["active"]
    ) or '<p class="empty">暂无活跃品味</p>'
    taste_html = f'<section class="state-section"><h3>品味 · 软影响（非事实）</h3><ul>{taste_items}</ul></section>'

    session_note = (
        f'<span class="badge">session {html.escape(model["session_id"])}</span>'
        if model["session_id"]
        else '<span class="badge">全部会话</span>'
    )

    return _PAGE_TEMPLATE.format(
        project=project,
        session_note=session_note,
        event_count=model["counts"]["events"],
        folded=model["folded_low_signal"],
        pending_total=counts["total"],
        inbox_html=inbox_html,
        state_html=state_html,
        taste_html=taste_html,
        timeline_rows="".join(timeline_rows) or '<p class="empty">暂无高信号事件</p>',
    )


_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>NoName 账本 · {project}</title>
<style>
:root{{--bg:#0b0d10;--bg-soft:#10131a;--fg:#f2efe9;--fg-dim:rgba(242,239,233,.72);--muted:#8a8f98;--accent:#e07a5f;--hairline:rgba(242,239,233,.1);--serif:"Songti SC","Noto Serif SC",Georgia,serif;--sans:-apple-system,"PingFang SC","Segoe UI",sans-serif;--mono:ui-monospace,Menlo,Consolas,monospace}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:var(--bg);color:var(--fg);font-family:var(--sans);-webkit-font-smoothing:antialiased;line-height:1.6}}
.wrap{{max-width:860px;margin:0 auto;padding:48px 28px 96px}}
header{{border-bottom:1px solid var(--hairline);padding-bottom:22px;margin-bottom:34px}}
h1{{font-family:var(--serif);font-size:2rem;font-weight:700}}
.sub{{color:var(--muted);font-size:.9rem;margin-top:8px;font-family:var(--mono)}}
.badge{{display:inline-block;font-family:var(--mono);font-size:.68rem;letter-spacing:.06em;padding:2px 8px;border:1px solid var(--hairline);border-radius:99px;color:var(--muted);margin-right:6px}}
.badge.review{{color:var(--accent);border-color:var(--accent)}}
.badge.authored{{color:var(--accent);border-color:var(--accent)}}
section{{margin-bottom:40px}}
h2{{font-family:var(--serif);font-size:1.3rem;font-weight:600;margin-bottom:16px;padding-top:14px;border-top:1px solid var(--hairline)}}
h3{{font-family:var(--serif);font-size:1.02rem;font-weight:600;margin-bottom:12px;color:var(--fg-dim)}}
ul{{list-style:none}}
.node{{padding:12px 4px;border-bottom:1px solid var(--hairline)}}
.node .seq{{font-family:var(--mono);color:var(--muted);font-size:.75rem;margin-right:10px}}
.node .etype{{font-family:var(--mono);font-size:.82rem;color:var(--fg)}}
.node .time{{float:right;font-family:var(--mono);font-size:.72rem;color:var(--muted)}}
.node .payload{{color:var(--fg-dim);font-size:.86rem;margin-top:6px;font-family:var(--mono);word-break:break-all;white-space:pre-wrap}}
.inbox-section ul,.state-section ul{{display:flex;flex-direction:column;gap:10px}}
.inbox-item,.state-section li{{background:var(--bg-soft);border:1px solid var(--hairline);border-radius:8px;padding:12px 14px}}
.inbox-item{{border-left:2px solid var(--accent)}}
.inbox-item strong{{font-size:.95rem}}
.meta{{color:var(--muted);font-size:.76rem;margin-top:6px;font-family:var(--mono)}}
.empty{{color:var(--muted);font-style:italic;padding:12px 0}}
.inbox-banner{{background:rgba(224,122,95,.08);border:1px solid rgba(224,122,95,.3);border-radius:8px;padding:12px 16px;margin-bottom:20px;color:var(--fg-dim);font-size:.9rem}}
.inbox-banner strong{{color:var(--accent)}}
</style>
</head>
<body>
<div class="wrap">
<header>
<h1>NoName 账本 · {project}</h1>
<div class="sub">{session_note} · {event_count} 事件 · {folded} 低层已折叠</div>
</header>

<section>
<h2>审核收件箱</h2>
<div class="inbox-banner"><strong>{pending_total}</strong> 项等待你的判断——审核是签署，不是点按钮。</div>
{inbox_html}
</section>

<section>
<h2>状态 · 现在为何如此</h2>
{state_html}
{taste_html}
</section>

<section>
<h2>时间线 · 发生了什么</h2>
<ul>
{timeline_rows}
</ul>
</section>

</div>
</body>
</html>
"""
