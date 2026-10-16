"""Generate an isolated, clearly labelled ledger for UX preview.

Run from the repository: .venv/bin/python -m examples.ledger_demo
All signatures are simulated in a new temporary directory, never your ledger.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from noname_harness.ledger_server import start_background_ledger
from noname_harness.ledger_view import build_ledger_model, render_ledger_html
from noname_harness.store import HarnessStore
from noname_harness.taste import TasteService
from noname_harness.taste_cards import TasteCardService


def main():
    root = Path(tempfile.mkdtemp(prefix="noname-ledger-demo-"))
    db = root / ".noname/harness.db"
    with HarnessStore(db) as store:
        store.initialize_project(root, "Paper Studio · 演示项目")
        (root / "empty.html").write_text(render_ledger_html(build_ledger_model(store)), encoding="utf-8")
        user = store.append_event("demo-design", "codex.user", {
            "text": "首版只在本地使用，数据先存在 SQLite。让作品成为焦点，工具退到背景。",
            "origin": "isolated_demo", "simulated": True,
        })
        saved = store.create_proposal("high", "database", {
            "text": "首版使用 SQLite 保存项目数据。", "reason": "先服务本地单人工作，减少部署成本。",
        }, [user.id])
        store.review_proposal(saved["id"], "accept", "演示用户（模拟签署）")
        task = store.create_proposal("mid", "checkpoint", {
            "text": "完成阅读与审核体验的第一轮实现。", "reason": "让用户先看到决定，再按需查看证据。",
            "next_step": "验证窄屏阅读，并确认修正后的决定能带入新会话。",
        }, [user.id])
        store.review_proposal(task["id"], "accept", "演示用户（模拟签署）")
        taste = TasteService(store)
        authored = taste.record_authored({
            "attitude": "少而准确，让作品保持焦点。", "example": "先展示决定与理由，技术细节按需展开。",
        }, scope="project", actor_id="演示用户", source_event_ids=[user.id])
        feedback = store.append_event("demo-correction", "codex.user", {
            "text": "接下来要支持多人协作。我们改用 PostgreSQL，旧决定留作历史。",
            "origin": "isolated_demo", "simulated": True,
        })
        store.create_proposal("high", "database", {
            "text": "数据存储改用 PostgreSQL。", "reason": "开始支持多人协作，需要共享数据库。",
            "next_step": "先验证迁移路径，再接入新的连接配置。",
        }, [feedback.id], reason="用户明确修正了先前决定。")
        taste.propose_adopted({
            "attitude": "在重要判断前，先看清它会改变什么。", "example": "在确认按钮上方对照当前与新版本。",
        }, source_event_ids=[feedback.id])
        TasteCardService(store).create_card(
            title="给作品留白", attitude="把注意力留给重要内容。", track="authored", scope="project",
            taste_ids=[authored["id"]], representative_evidence=[user.id],
            tensions="简洁与可追溯之间的分寸", influence="soft",
        )
        for i in range(4):
            store.append_event("demo-design", "dsh.tool.completed", {
                "text": f"tool test -> 演示验证 {i + 1} 已完成；这条输出是档案，不是长期规则。",
            })
        store.assemble_context_package("继续 Paper Studio 的工作", low_limit=3)
        page = root / "ledger.html"
        page.write_text(render_ledger_html(build_ledger_model(store)), encoding="utf-8")
        assert store.verify_integrity()["ok"]
    server = start_background_ledger(db, idle_timeout=1800)
    print(json.dumps({"root": str(root), "db": str(db), "snapshot": str(page),
                      "empty": str(root / "empty.html"), "url": server["url"], "simulated": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
