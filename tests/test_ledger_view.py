"""Ledger projection + offline HTML rendering."""

from __future__ import annotations

import hashlib
import json
from html.parser import HTMLParser

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


# --- 对抗性审查发现的回归 ---

def test_low_signal_events_are_expandable_via_details_not_deleted(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        _seed(store)  # includes a file.read low-signal event
        model = build_ledger_model(store)
        page = render_ledger_html(model)
        # The low-signal event is present (inside a <details> fold), not deleted.
        assert "<details>" in page
        assert "file.read" in page
        assert "低层簿记事件" in page
    finally:
        store.close()


def test_class_attribute_is_escaped(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        _seed(store)
        model = build_ledger_model(store)
        # Inject a hostile group value; it must be escaped in the class/attribute context.
        model["timeline"][0]["group"] = 'x"><img src=x onerror=alert(1)>'
        page = render_ledger_html(model)
        assert 'onerror=alert(1)' not in page.replace("&quot;", '"').replace("&#x27;", "'") or "&lt;img" in page
        # The raw injection must not appear as a live attribute.
        assert 'class="badge">x"><img' not in page
    finally:
        store.close()


def test_all_sessions_view_prefixes_session_on_seq(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        store.append_event("alpha", "test.failed", {"path": "t.py", "message": "x"})
        store.append_event("beta", "test.failed", {"path": "t.py", "message": "x"})
        page = render_ledger_html(build_ledger_model(store))
        # Both sessions' #1 are disambiguated with a session prefix.
        assert "alpha#1" in page and "beta#1" in page
    finally:
        store.close()


def test_session_filter_labels_global_sections(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        _seed(store)
        page = render_ledger_html(build_ledger_model(store, session_id="s"))
        assert "全局视图，不受会话过滤影响" in page
    finally:
        store.close()


def test_truncation_is_surfaced_honestly(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        for i in range(5):
            store.append_event("s", "note", {"text": f"event {i}"})
        model = build_ledger_model(store, limit=2)
        page = render_ledger_html(model)
        assert model["truncated"] is True
        assert "历史被截断" in page
    finally:
        store.close()


def test_new_event_types_are_visible_by_default_fail_open(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        # A future event type not in any list must be shown (fail-open), not folded.
        store.append_event("s", "human.note", {"text": "a brand new event type"})
        model = build_ledger_model(store)
        node = next(n for n in model["timeline"] if n["event_type"] == "human.note")
        assert node["high_signal"] is True
    finally:
        store.close()


def test_review_projection_resolves_full_evidence_beyond_timeline_filter(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        long_text = "最初的完整讨论" + "证据" * 350
        source = store.append_event("source-session", "human.note", {"text": long_text})
        proposal = store.create_proposal("mid", "next-step", {"goal": "inspect sources"}, [source.id])
        store.append_event("other-session", "human.note", {"text": "different session"})
        before = [event.id for event in store.list_events(limit=100)]
        model = build_ledger_model(store, session_id="other-session", limit=1)
        item = model["inbox"]["task_pending"][0]
        assert item["record"] == store.get_proposal(proposal["id"])
        assert item["source_events"][0]["payload"]["text"] == long_text
        assert source.id not in [event["id"] for event in model["timeline"]]
        assert item["expected_version"] == hashlib.sha256(json.dumps(
            item["record"], sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("utf-8")).hexdigest()
        page = render_ledger_html(model)
        assert long_text in page
        assert source.id in page
        assert [event.id for event in store.list_events(limit=100)] == before
    finally:
        store.close()


def test_review_projection_shows_conflicts_current_version_and_defer_history(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        source = store.append_event("s", "human.note", {"text": "old decision"})
        old = store.create_proposal("high", "database", {"name": "SQLite"}, [source.id])
        store.review_proposal(old["id"], "accept", "first-reviewer")
        proposal = store.create_proposal("high", "database", {"name": "PostgreSQL"}, [source.id])
        initial = build_ledger_model(store)["inbox"]["canon_pending"][0]
        assert initial["replaces"][0]["content"] == {"name": "SQLite"}
        assert initial["replaces"][0]["id"] in initial["record"]["conflict_with_ids"]
        store.review_proposal(proposal["id"], "defer", "second-reviewer", reason="needs evidence")
        model = build_ledger_model(store)
        item = model["inbox"]["canon_pending"][0]
        assert item["expected_version"] != initial["expected_version"]
        page = render_ledger_html(model)
        assert "needs evidence" in page
        assert "second-reviewer" in page
        assert "采纳将替代当前版本" in page
        assert "存在 1 项冲突" in page
        assert "SQLite" in page
    finally:
        store.close()


class _ReviewParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self.elements = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.elements.append((tag, attributes))
        if tag == "form":
            self.forms.append({"attributes": attributes, "elements": []})
        elif self.forms and tag in {"button", "input", "textarea"}:
            self.forms[-1]["elements"].append((tag, attributes))


def _all_candidate_kinds(store):
    source = store.append_event("s", "human.note", {"text": "source context"})
    proposal = store.create_proposal("high", "boundary", {"text": "keep local"}, [source.id])
    taste = TasteService(store)
    candidate = taste.propose_adopted({"attitude": "check evidence"}, source_event_ids=[source.id])
    authored = taste.record_authored({"attitude": "keep restrained"}, source_event_ids=[source.id])
    card = TasteCardService(store).create_card(
        title="Restraint", attitude="Keep decisions grounded", track="authored", scope="user",
        taste_ids=[authored["id"]], representative_evidence=[source.id, "outside-ledger-reference"],
        tensions="speed versus care", influence="soft",
    )
    return proposal, candidate, card, source


def test_live_review_exposes_only_supported_signed_actions_and_versions(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        _all_candidate_kinds(store)
        model = build_ledger_model(store)
        page = render_ledger_html(model, review_context={"token": "local-secret"})
        parser = _ReviewParser()
        parser.feed(page)
        assert len(parser.forms) == 3
        expected = {
            "state_proposal": {"accept", "reject", "defer", "edit"},
            "taste_candidate": {"adopt", "retire"},
            "taste_card_candidate": {"accept", "retire", "edit"},
        }
        for form in parser.forms:
            attrs = form["attributes"]
            assert len(attrs["data-version"]) == 64
            actions = {attrs["value"] for tag, attrs in form["elements"] if tag == "button" and attrs.get("name") == "action"}
            assert actions == expected[attrs["data-kind"]]
            reviewer = next(attrs for tag, attrs in form["elements"] if attrs.get("name") == "reviewer_id")
            assert "required" in reviewer and not reviewer.get("value")
            assert any(attrs.get("name") == "reason" for _, attrs in form["elements"])
        assert "修改后的内容将直接成为已批准状态" in page
        assert "保存修改，继续待审" in page
        assert "X-NoName-Token" in page
        assert "expected_version: form.dataset.version" in page
        assert "response.status === 409" in page
        assert "保留草稿并刷新候选" in page
        assert "credentials: 'same-origin'" in page
        state_form = next(form for form in parser.forms if form["attributes"]["data-kind"] == "state_proposal")
        assert state_form["attributes"]["data-expected-head-id"] == ""
        assert "payload.expected_head_id = form.dataset.expectedHeadId" in page
    finally:
        store.close()


def test_live_form_includes_current_state_head_and_changes_with_other_proposal(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        source = store.append_event("s", "human.note", {"text": "database choice"})
        original = store.create_proposal("high", "database", {"name": "SQLite"}, [source.id])
        store.review_proposal(original["id"], "accept", "first-reviewer")
        pending = store.create_proposal("high", "database", {"name": "PostgreSQL"}, [source.id])
        first_model = build_ledger_model(store)
        first_item = first_model["inbox"]["canon_pending"][0]
        parser = _ReviewParser()
        parser.feed(render_ledger_html(first_model, review_context={"token": "token"}))
        old_head = parser.forms[0]["attributes"]["data-expected-head-id"]
        assert old_head == first_item["replaces"][0]["id"]

        other = store.create_proposal("high", "database", {"name": "DuckDB"}, [source.id])
        store.review_proposal(other["id"], "accept", "other-reviewer")
        second_model = build_ledger_model(store)
        second_item = second_model["inbox"]["canon_pending"][0]
        assert second_item["id"] == pending["id"]
        # The candidate is unchanged, so version alone cannot protect the
        # replacement shown to the reviewer.  The separately sent head does.
        assert second_item["expected_version"] == first_item["expected_version"]
        parser = _ReviewParser()
        parser.feed(render_ledger_html(second_model, review_context={"token": "token"}))
        assert parser.forms[0]["attributes"]["data-expected-head-id"] != old_head
        assert parser.forms[0]["attributes"]["data-expected-head-id"] == second_item["replaces"][0]["id"]
    finally:
        store.close()


def test_card_projection_includes_related_tastes_and_missing_source_warning(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        _, _, card, source = _all_candidate_kinds(store)
        model = build_ledger_model(store)
        item = model["inbox"]["card_pending"][0]
        assert item["record"] == TasteCardService(store).get(card["id"])
        assert item["related_taste"][0]["content"] == {"attitude": "keep restrained"}
        assert [event["id"] for event in item["source_events"]].count(source.id) == 1
        assert item["source_events"][-1] == {"id": "outside-ledger-reference", "unavailable": True}
        page = render_ledger_html(model)
        assert "关联品味 · 1 项" in page
        assert "无法解析的引用" in page
        assert "speed versus care" in page
    finally:
        store.close()


def test_static_snapshot_has_disclosures_and_clear_read_only_guidance(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        _all_candidate_kinds(store)
        page = render_ledger_html(build_ledger_model(store))
        parser = _ReviewParser()
        parser.feed(page)
        assert not parser.forms
        # Offline navigation/search is self-contained; there is no signing
        # client, credential or network request in a static export.
        assert sum(tag == "script" for tag, _ in parser.elements) == 1
        assert "fetch(" not in page and "X-NoName-Token" not in page
        assert "data-review-token" not in page
        assert "只读快照" in page
        assert "ledger-serve" in page and "--background" in page
        assert "完整候选内容与审核记录" in page
        assert "查看来源事件" in page
    finally:
        store.close()


def test_live_review_content_and_token_cannot_create_html_attributes_or_scripts(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        hostile = '</textarea><script>alert("pwn")</script><img src=x onerror=alert(1)>'
        source = store.append_event("s", "human.note", {"text": hostile})
        proposal = store.create_proposal("mid", hostile, {"text": hostile}, [source.id])
        token = '" onmouseover="alert(1)</script>'
        page = render_ledger_html(build_ledger_model(store), review_context={"token": token})
        parser = _ReviewParser()
        parser.feed(page)
        assert sum(tag == "script" for tag, _ in parser.elements) == 1
        assert not any(tag == "img" for tag, _ in parser.elements)
        assert not any(key.startswith("on") for _, attrs in parser.elements for key in attrs)
        body = next(attrs for tag, attrs in parser.elements if tag == "body")
        assert body["data-review-token"] == token
        assert parser.forms[0]["attributes"]["data-id"] == proposal["id"]
        assert "&lt;/textarea&gt;&lt;script&gt;" in page
    finally:
        store.close()


def test_current_reading_resolves_old_sources_and_never_promotes_dialogue(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        source = store.append_event("old-session", "codex.user", {"text": "Local-first because deployment must stay simple"})
        proposal = store.create_proposal("high", "database", {"text": "SQLite", "reason": "Local deployment"}, [source.id])
        store.review_proposal(proposal["id"], "accept", "test-user")
        latest = store.append_event("new-session", "codex.user", {"text": "An unconfirmed idea: use another database"})
        before = [e.id for e in store.list_events(limit=100)]
        model = build_ledger_model(store, session_id="new-session", limit=1)
        assert model["state"]["high"][0]["source_events"][0]["id"] == source.id
        assert model["timeline"][0]["id"] == latest.id
        assert model["timeline"][0]["high_signal"] is False
        assert model["state"]["high"][0]["content"]["text"] == "SQLite"
        assert [e.id for e in store.list_events(limit=100)] == before
    finally:
        store.close()


def test_readable_preserves_unknown_fields_and_orders_decision_before_reason():
    from noname_harness.ledger_readable import readable
    rendered = readable({"next_step": "Verify migration", "reason": "Shared access", "text": "PostgreSQL", "unknown_field": {"nested": [False, None, "<script>x</script>"]}})
    assert rendered.index("PostgreSQL") < rendered.index("Shared access") < rendered.index("Verify migration")
    assert "unknown field" in rendered and "nested" in rendered
    assert "否" in rendered and "未提供" in rendered
    assert "<script>" not in rendered and "&lt;script&gt;" in rendered


def test_daily_receipts_stay_archived_while_unknown_events_remain_visible(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        for event_type in ("codex.hook.SessionStart", "codex.hook.Stop", "dsh.tool.completed", "future.user-decision"):
            store.append_event("s", event_type, {"text": "record"})
        nodes = {node["event_type"]: node for node in build_ledger_model(store)["timeline"]}
        assert not nodes["codex.hook.Stop"]["high_signal"]
        assert not nodes["codex.hook.SessionStart"]["high_signal"]
        assert not nodes["dsh.tool.completed"]["high_signal"]
        assert nodes["future.user-decision"]["high_signal"]
    finally:
        store.close()


def test_friendly_edit_preserves_literal_hostile_keys_and_json_structure(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        source = store.append_event("s", "codex.note", {"text": "a source"})
        content = {"text": "Keep local", "reason": "<unsafe>", "__proto__": "literal user field"}
        store.create_proposal("mid", "checkpoint", content, [source.id])
        page = render_ledger_html(build_ledger_model(store), review_context={"token": "local"})
        parser = _ReviewParser()
        parser.feed(page)
        textareas = [attrs for tag, attrs in parser.elements if tag == "textarea"]
        assert {attrs["data-edit-field"] for attrs in textareas if "data-edit-field" in attrs} == set(content)
        assert "Object.defineProperty" in page
        assert "&lt;unsafe&gt;" in page and "<unsafe>" not in page
    finally:
        store.close()


def test_same_topic_pending_alternatives_are_visible_without_changing_review_state(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        source = store.append_event("s", "codex.user", {"text": "compare storage options"})
        first = store.create_proposal("high", "database", {"text": "SQLite"}, [source.id])
        second = store.create_proposal("high", "database", {"text": "PostgreSQL"}, [source.id])
        model = build_ledger_model(store)
        first_item, second_item = model["inbox"]["canon_pending"]
        assert first_item["same_topic_candidates"][0]["id"] == second["id"]
        assert second_item["same_topic_candidates"][0]["id"] == first["id"]
        assert not first_item["latest_for_topic"] and second_item["latest_for_topic"]
        page = render_ledger_html(model)
        assert "同一主题还有 1 条待确认内容" in page
        assert "先前的提议" in page and "较新的提议" in page
        assert len(store.list_proposals(pending_only=True)) == 2
        assert not store.active_state()
    finally:
        store.close()


@pytest.mark.parametrize("lifecycle", ["retired", "expired", "future"])
def test_non_effective_replacement_is_never_labelled_current(tmp_path, lifecycle):
    from noname_harness.ledger_page import _history
    store, _ = make_store(tmp_path)
    try:
        source = store.append_event("s", "codex.user", {"text": "an old decision"})
        old = store.create_proposal("high", "database", {"text": "SQLite"}, [source.id])
        if lifecycle == "retired":
            store.review_proposal(old["id"], "retire", "isolated-test")
        else:
            validity = ({"valid_from": "1999-01-01T00:00:00Z", "valid_to": "2000-01-01T00:00:00Z"}
                        if lifecycle == "expired" else {"valid_from": "2100-01-01T00:00:00Z"})
            store.review_proposal(old["id"], "accept", "isolated-test", **validity)
        store.create_proposal("high", "database", {"text": "PostgreSQL"}, [source.id])
        model = build_ledger_model(store)
        assert not model["state"]["high"]
        item = model["inbox"]["canon_pending"][0]
        assert item["replaces"] and not item["replacement_is_current"]
        page = render_ledger_html(model)
        assert "上一个历史版本 · 当前未生效" in page
        assert "当前没有生效版本" in page
        assert "采纳将替代当前版本" not in page
        history = _history(model["state_diff"], model["state"])
        assert '>当前有效<' not in history
    finally:
        store.close()


def test_confirmed_taste_card_stays_visible_and_stale_sources_are_disclosed(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        taste = TasteService(store).record_authored({"attitude": "Clarity"})
        card = TasteCardService(store).create_card(title="Careful clarity", attitude="Make decisions readable",
            track="authored", scope="user", taste_ids=[taste["id"]])
        TasteCardService(store).review(card["id"], "accept", "isolated-user")
        page = render_ledger_html(build_ledger_model(store))
        assert "已确认的品味卡" in page and "Careful clarity" in page
        TasteService(store).review(taste["id"], "retire", "isolated-user")
        page = render_ledger_html(build_ledger_model(store))
        assert "这张卡需要复核" in page
    finally:
        store.close()
