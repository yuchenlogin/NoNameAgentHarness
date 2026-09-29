"""Ledger projection + offline HTML rendering."""

from __future__ import annotations

import json

import pytest

from noname_harness.ledger_view import build_ledger_model, render_ledger_html
from noname_harness.store import HarnessStore
from noname_harness.taste import TasteService
from noname_harness.taste_cards import TasteCardService


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "ledger project")
    return store, root


def _seed(store):
    event = store.append_event(
        "s", "project.constraint", {"key": "boundary", "content": {"text": "stay in root"}}
    )
    proposal = store.create_proposal("high", "boundary", {"text": "stay in root"}, [event.id])
    store.review_proposal(proposal["id"], "accept", "user")
    TasteService(store).record_authored({"judgement": "克制"}, scope="user")
    store.append_event("s", "test.failed", {"path": "t.py", "message": "boom"})
    store.append_event("s", "file.read", {"path": "a.py", "size": 10})  # low signal


def test_ledger_model_is_a_pure_projection(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        _seed(store)
        model = build_ledger_model(store)
        assert model["project"]["name"] == "ledger project"
        assert model["timeline"]
        # High-signal nodes are marked; low-signal (file.read) is folded.
        assert any(n["high_signal"] for n in model["timeline"])
        assert model["folded_low_signal"] >= 1
        # State and taste are projected.
        assert model["state"]["high"][0]["logical_key"] == "boundary"
        assert model["taste"]["active"][0]["track"] == "authored"
        # The projection writes nothing new to the store.
        assert store.verify_integrity()["ok"] is True
    finally:
        store.close()


def test_ledger_model_includes_inbox(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        event = store.append_event("s", "project.constraint", {"key": "b", "content": {"text": "x"}})
        store.create_proposal("high", "b", {"text": "x"}, [event.id])  # pending
        model = build_ledger_model(store)
        assert model["inbox"]["counts"]["canon"] == 1
        assert model["counts"]["pending_total"] >= 1
    finally:
        store.close()


def test_rendered_html_is_self_contained_and_escapes(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        _seed(store)
        # Adversarial content that must not break out of the HTML.
        store.append_event("s", "task.updated", {"key": "t", "content": {"goal": "<script>alert(1)</script>"}})
        model = build_ledger_model(store)
        page = render_ledger_html(model)
        assert page.startswith("<!DOCTYPE html>")
        assert "NoName 账本" in page
        # No unescaped script injection.
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page
        # Sections are present.
        assert "审核收件箱" in page
        assert "时间线" in page
        assert "状态" in page
        # Offline: no external resource references.
        assert "http://" not in page and "https://" not in page.replace("W3C", "")
    finally:
        store.close()


def test_rendered_html_shows_pending_review_visually_distinct(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        event = store.append_event("s", "project.constraint", {"key": "b", "content": {"text": "x"}})
        store.create_proposal("high", "b", {"text": "x"}, [event.id])
        model = build_ledger_model(store)
        page = render_ledger_html(model)
        assert "法典候选" in page
        assert "等待你的判断" in page
    finally:
        store.close()


def test_ledger_cli_generates_html_file(tmp_path, capsys):
    from noname_harness.cli import main

    root = tmp_path / "project"
    root.mkdir()
    db = root / ".noname" / "harness.db"
    assert main(["init", "--db", str(db), "--root", str(root)]) == 0
    capsys.readouterr()
    assert main([
        "event", "--db", str(db), "--session", "s", "--type", "project.constraint",
        "--payload", '{"key":"b","content":{"text":"x"}}',
    ]) == 0
    capsys.readouterr()
    out = root / "ledger.html"
    assert main(["ledger-html", "--db", str(db), "--out", str(out)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["events"] >= 1
    content = out.read_text(encoding="utf-8")
    assert "NoName 账本" in content
    # Refuses to overwrite without --overwrite.
    assert main(["ledger-html", "--db", str(db), "--out", str(out)]) == 2
    capsys.readouterr()
