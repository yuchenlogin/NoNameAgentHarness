from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

from noname_harness import HarnessStore
from noname_harness.codex_bridge import hook, main, open_store, status, workspace_root
from noname_harness.codex_install import install, hook_config, atomic_write


def project(tmp_path, name="project"):
    root = tmp_path / name
    root.mkdir()
    with HarnessStore(root / ".noname/harness.db") as store:
        store.initialize_project(root, name)
    return root


def capture(root, event, *, turn="t1", session="s1", **extra):
    return hook({"cwd": str(root), "session_id": session, "turn_id": turn,
                 "hook_event_name": event, **extra})


def test_disabled_status_and_hook_do_not_create_database(tmp_path):
    assert status(tmp_path)["enabled"] is False
    assert capture(tmp_path, "UserPromptSubmit", prompt="uncaptured") is None
    assert not (tmp_path / ".noname").exists()


def test_capture_recall_review_and_session_receipts(tmp_path, capsys):
    root = project(tmp_path)
    capture(root, "UserPromptSubmit", prompt="保留证据与账本")
    capture(root, "Stop", last_assistant_message="已完成内核验证")
    assert main(["--root", str(root), "remember", "--session", "s1",
                 "--key", "continuity", "--text", "保留证据", "--layer", "high"]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["active"] is False
    assert status(root)["accepted_state_count"] == 0
    assert main(["--root", str(root), "review", "--proposal-id", saved["proposal"]["id"],
                 "--action", "accept", "--reviewer", "user"]) == 0
    capsys.readouterr()
    output = capture(root, "SessionStart", session="s2", source="startup")
    assert "保留证据" in output["hookSpecificOutput"]["additionalContext"]
    state = status(root, "s2")
    assert state["current_session_hooks_observed"] == ["SessionStart"]
    assert state["accepted_state_count"] == 1
    assert state["integrity"]["ok"]
    assert status(root, "uncaptured")["current_session_hooks_observed"] == []


def test_hook_retries_atomic_and_idempotent(tmp_path):
    root = project(tmp_path)
    for _ in range(3):
        capture(root, "UserPromptSubmit", prompt="原始输入")
        capture(root, "Stop", last_assistant_message="原始答复")
    with open_store(root) as store:
        events = store.list_events("s1")
        assert len(events) == 4
        assert [e.event_type for e in store.list_work_events("s1")] == ["codex.assistant", "codex.user"]
        assert store.verify_integrity()["ok"]


def test_same_text_different_turn_is_not_lost(tmp_path):
    root = project(tmp_path)
    capture(root, "UserPromptSubmit", prompt="再试一次", turn="first")
    capture(root, "UserPromptSubmit", prompt="再试一次", turn="second")
    with open_store(root) as store:
        assert len(store.list_work_events()) == 2


def test_pending_proposal_not_promoted_by_startup(tmp_path, capsys):
    root = project(tmp_path)
    assert main(["--root", str(root), "remember", "--session", "s1",
                 "--key", "maybe", "--text", "这是候选，不能当事实"]) == 0
    capsys.readouterr()
    capture(root, "SessionStart", session="s2")
    assert status(root)["accepted_state_count"] == 0
    assert status(root)["pending_review"]["total"] == 1


def test_cross_project_isolation(tmp_path):
    first, second = project(tmp_path, "first"), project(tmp_path, "second")
    capture(first, "UserPromptSubmit", prompt="第一个项目专属")
    output = capture(second, "SessionStart")
    assert "第一个项目专属" not in output["hookSpecificOutput"]["additionalContext"]
    assert status(second)["event_count"] == 2  # startup receipt + context audit


def test_symlink_database_outside_workspace_rejected(tmp_path):
    first = project(tmp_path, "first")
    second = tmp_path / "second"
    second.mkdir()
    (second / ".noname").symlink_to(first / ".noname", target_is_directory=True)
    with pytest.raises(ValueError, match="must stay inside"):
        capture(second, "SessionStart")
    assert status(first)["event_count"] == 0


def test_copied_database_root_mismatch_rejected(tmp_path):
    import shutil
    first = project(tmp_path, "first")
    second = tmp_path / "second"
    second.mkdir()
    shutil.copytree(first / ".noname", second / ".noname")
    with pytest.raises(ValueError, match="different workspace"):
        open_store(second)


def test_git_subdirectory_finds_checkout_ledger(tmp_path):
    root = project(tmp_path)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    nested = root / "src"
    nested.mkdir()
    assert workspace_root(nested) == root
    opted = nested / ".noname/harness.db"
    with HarnessStore(opted) as store:
        store.initialize_project(nested, "nested")
    assert workspace_root(nested) == nested


def test_hook_invalid_input_reports_failure_without_active_receipt(tmp_path, monkeypatch, capsys):
    root = project(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "cwd": str(root), "hook_event_name": "Stop", "session_id": 12})))
    assert main(["hook"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert "requires cwd and session_id" in output["systemMessage"]
    assert "decision" not in output
    assert status(root)["event_count"] == 0


def test_text_file_is_data_and_search_is_json(tmp_path, capsys):
    root = project(tmp_path)
    content = '保留账本 $(touch /tmp/never-execute) `echo unsafe`\n第二行'
    note = tmp_path / "note.txt"
    note.write_text(content, encoding="utf-8")
    assert main(["--root", str(root), "remember", "--session", "s1", "--key", "literal",
                 "--text-file", str(note)]) == 0
    capsys.readouterr()
    assert main(["--root", str(root), "search", "账本"]) == 0
    results = json.loads(capsys.readouterr().out)
    assert any(e["payload"].get("text") == content for e in results)


def test_large_startup_memory_has_explicit_fallback(tmp_path):
    root = project(tmp_path)
    capture(root, "UserPromptSubmit", prompt="巨大输入" * 20000)
    context = capture(root, "SessionStart")["hookSpecificOutput"]["additionalContext"]
    data = json.loads(context.rsplit("\n", 1)[-1])
    assert data["omitted_counts"]["low"] == 1
    assert len(context.encode("utf-8")) <= 12000


def test_stop_without_message_records_honest_receipt(tmp_path):
    root = project(tmp_path)
    assert capture(root, "Stop", last_assistant_message=None) == {}
    with open_store(root) as store:
        events = store.list_events()
        assert len(events) == 1
        assert events[0].payload["captured_text"] is False
    assert status(root)["recent_hook_receipts"][0]["captured_text"] is False


def test_failed_startup_does_not_report_successful_receipt(tmp_path, monkeypatch):
    root = project(tmp_path)
    def fail(*args, **kwargs):
        raise RuntimeError("package failed")
    monkeypatch.setattr(HarnessStore, "assemble_context_package", fail)
    with pytest.raises(RuntimeError, match="package failed"):
        capture(root, "SessionStart")
    assert status(root)["event_count"] == 0


def test_invalid_checkpoint_key_does_not_append_evidence(tmp_path, capsys):
    root = project(tmp_path)
    assert main(["--root", str(root), "remember", "--session", "s1", "--key", " ",
                 "--text", "a note"]) == 2
    assert "key cannot be empty" in capsys.readouterr().err
    assert status(root)["event_count"] == 0


def test_mismatched_database_rejected_before_store_migration(tmp_path, monkeypatch):
    import shutil
    first = project(tmp_path, "first")
    second = tmp_path / "second"
    second.mkdir()
    shutil.copytree(first / ".noname", second / ".noname")
    def must_not_migrate(*args):
        raise AssertionError("must reject workspace before migrations")
    monkeypatch.setattr(HarnessStore, "_ensure_schema", must_not_migrate)
    with pytest.raises(ValueError, match="different workspace"):
        open_store(second)


def test_install_preserves_other_hooks_and_is_idempotent(tmp_path):
    home, skills = tmp_path / "codex", tmp_path / "skills"
    home.mkdir()
    old = {"description": "existing", "hooks": {"SessionStart": [{
        "matcher": "resume", "hooks": [{"type": "command", "command": "echo existing"}]}]}}
    (home / "hooks.json").write_text(json.dumps(old), encoding="utf-8")
    first = install(home, skills, Path(sys.executable))
    installed = json.loads((home / "hooks.json").read_text())
    assert installed["hooks"]["SessionStart"][0] == old["hooks"]["SessionStart"][0]
    assert install(home, skills, Path(sys.executable)) == first
    assert json.loads((home / "hooks.json").read_text()) == installed
    assert (skills / "noname-status").is_symlink()
    assert (home / "hooks.json.before-noname").exists()
    # The actual launcher imports the repo with -I and preserves CWD.
    output = subprocess.run([str(home / "bin/noname-codex"), "status"], cwd=tmp_path,
                            capture_output=True, text=True, check=True)
    assert json.loads(output.stdout)["enabled"] is False
    install(home, skills, Path(sys.executable), remove=True)
    assert json.loads((home / "hooks.json").read_text()) == old
    assert not (home / "bin/noname-codex").exists()
    assert not (skills / "noname-status").exists()


def test_install_name_collision_makes_no_partial_changes(tmp_path):
    home, skills = tmp_path / "codex", tmp_path / "skills"
    conflict = skills / "noname-status"
    conflict.mkdir(parents=True)
    (conflict / "SKILL.md").write_text("user-owned skill")
    with pytest.raises(ValueError, match="already exists"):
        install(home, skills, Path(sys.executable))
    assert not (home / "bin/noname-codex").exists()
    assert not (home / "hooks.json").exists()
    assert (conflict / "SKILL.md").read_text() == "user-owned skill"


def test_install_refuses_modified_prompt_and_symlinked_launcher(tmp_path):
    home, skills = tmp_path / "codex", tmp_path / "skills"
    install(home, skills, Path(sys.executable))
    prompt = home / "prompts/noname-status.md"
    prompt.write_text("user modification")
    before = (home / "hooks.json").read_text()
    with pytest.raises(ValueError, match="existing file differs"):
        install(home, skills, Path(sys.executable), remove=True)
    assert (home / "hooks.json").read_text() == before
    assert prompt.read_text() == "user modification"


def test_hook_config_preserves_unrelated_handler_in_shared_group():
    old = {"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "ours hook"},
        {"type": "command", "command": "other"}]}]}}
    updated = hook_config(old, "ours hook", remove=True)
    assert updated["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "other"}]}]
    assert len(old["hooks"]["Stop"][0]["hooks"]) == 2


@pytest.mark.parametrize("event", ["UserPromptSubmit", "Stop", "SessionStart"])
def test_capture_failure_never_controls_codex_turn(tmp_path, monkeypatch, capsys, event):
    import noname_harness.codex_bridge as bridge
    def unavailable(*args):
        raise OSError("ledger unavailable")
    monkeypatch.setattr(bridge, "open_store", unavailable)
    root = project(tmp_path)
    payload = {"cwd": str(root), "session_id": "s1", "turn_id": "t1", "hook_event_name": event}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert main(["hook"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert set(output) == {"systemMessage"}
    assert "ledger unavailable" in output["systemMessage"]


def test_large_history_does_not_displace_small_accepted_state(tmp_path):
    root = project(tmp_path)
    with open_store(root) as store:
        event = store.append_event("s1", "project.decision", {"text": "使用独立项目账本"})
        proposal = store.create_proposal("high", "isolation", {"text": "使用独立项目账本"}, [event.id])
        store.review_proposal(proposal["id"], "accept", "user")
    capture(root, "UserPromptSubmit", prompt="非常大的原始对话" * 20000)
    context = capture(root, "SessionStart")["hookSpecificOutput"]["additionalContext"]
    data = json.loads(context.rsplit("\n", 1)[-1])
    assert data["layers"]["high"][0]["content"]["text"] == "使用独立项目账本"
    assert data["omitted_counts"]["high"] == 0
    assert data["omitted_counts"]["low"] == 1
    assert len(context.encode("utf-8")) <= 12000


def test_atomic_write_preserves_intervening_user_edit(tmp_path):
    path = tmp_path / "hooks.json"
    path.write_text("original")
    expected = path.read_bytes()
    path.write_text("new user edit")
    with pytest.raises(ValueError, match="changed during installation"):
        atomic_write(path, "our stale replacement", expected)
    assert path.read_text() == "new user edit"
    assert not list(tmp_path.glob(".noname-*"))


def test_atomic_write_preserves_existing_mode(tmp_path):
    path = tmp_path / "hooks.json"
    path.write_text("original")
    path.chmod(0o600)
    atomic_write(path, "replacement", path.read_bytes())
    assert path.read_text() == "replacement"
    assert path.stat().st_mode & 0o777 == 0o600


def test_two_installers_preserve_shared_configuration(tmp_path):
    import os
    home, skills = tmp_path / "codex", tmp_path / "skills"
    home.mkdir()
    unrelated = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo unrelated"}]}]}}
    (home / "hooks.json").write_text(json.dumps(unrelated))
    env = {**os.environ, "CODEX_HOME": str(home)}
    argv = [sys.executable, "-m", "noname_harness.codex_install", "--skills-home", str(skills)]
    workers = [subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    for worker in workers:
        stdout, stderr = worker.communicate(timeout=15)
        assert worker.returncode == 0, stderr.decode()
    config = json.loads((home / "hooks.json").read_text())
    stop_commands = [h["command"] for g in config["hooks"]["Stop"] for h in g["hooks"]]
    assert stop_commands.count("echo unrelated") == 1
    assert stop_commands.count(str(home / "bin/noname-codex") + " hook") == 1
    assert len(list(skills.iterdir())) == 8


def test_exported_custom_codex_home_is_used_in_startup_guidance(tmp_path, monkeypatch):
    root = project(tmp_path)
    home = tmp_path / "custom codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    context = capture(root, "SessionStart")["hookSpecificOutput"]["additionalContext"]
    assert str(home / "bin/noname-codex") in context


def test_preflight_snapshots_do_not_accept_new_user_file(tmp_path, monkeypatch):
    import noname_harness.codex_install as installer
    home, skills = tmp_path / "codex", tmp_path / "skills"
    prompt = home / "prompts/noname-status.md"
    original_write = installer.atomic_write
    def edit_after_preflight(path, text, expected):
        original_write(path, text, expected)
        if path == home / "bin/noname-codex":
            prompt.parent.mkdir(parents=True, exist_ok=True)
            prompt.write_text("new user-owned prompt")
    monkeypatch.setattr(installer, "atomic_write", edit_after_preflight)
    with pytest.raises(ValueError, match="changed during installation"):
        install(home, skills, Path(sys.executable))
    assert prompt.read_text() == "new user-owned prompt"
    assert not (home / "hooks.json").exists()


def test_failed_hook_unregistration_keeps_working_dependencies(tmp_path, monkeypatch):
    import noname_harness.codex_install as installer
    home, skills = tmp_path / "codex", tmp_path / "skills"
    install(home, skills, Path(sys.executable))
    hook_path = home / "hooks.json"
    original_write = installer.atomic_write
    def edit_before_publication(path, text, expected):
        if path == hook_path:
            newer = json.loads(hook_path.read_text())
            newer["description"] = "concurrent user edit"
            hook_path.write_text(json.dumps(newer))
        original_write(path, text, expected)
    monkeypatch.setattr(installer, "atomic_write", edit_before_publication)
    with pytest.raises(ValueError, match="changed during installation"):
        install(home, skills, Path(sys.executable), remove=True)
    assert (home / "bin/noname-codex").exists()
    assert (home / "prompts/noname-status.md").exists()
    assert (skills / "noname-status").is_symlink()
    assert json.loads(hook_path.read_text())["description"] == "concurrent user edit"


def test_checkpoint_retries_and_accepted_noop_do_not_fill_inbox(tmp_path, capsys):
    root = project(tmp_path)
    argv = ["--root", str(root), "remember", "--session", "s1", "--key", "storage",
            "--text", "Use SQLite", "--reason", "Keep work offline", "--next-step", "Implement add/list"]
    assert main(argv) == 0
    first = json.loads(capsys.readouterr().out)
    count = status(root)["event_count"]
    assert main(argv) == 0
    retry = json.loads(capsys.readouterr().out)
    assert retry["status"] == "already_pending"
    assert retry["proposal"]["id"] == first["proposal"]["id"]
    assert status(root)["event_count"] == count
    with open_store(root) as store:
        store.review_proposal(first["proposal"]["id"], "accept", "simulated-user")
    count = status(root)["event_count"]
    assert main(argv) == 0
    same = json.loads(capsys.readouterr().out)
    assert same["status"] == "unchanged"
    assert same["proposal"] is None
    assert same["active"] is True
    assert status(root)["pending_review"]["total"] == 0
    assert status(root)["event_count"] == count


def test_correction_preview_and_review_preserve_history(tmp_path, capsys):
    root = project(tmp_path)
    base = ["--root", str(root), "remember", "--session", "s1", "--key", "storage"]
    assert main([*base, "--text", "SQLite"]) == 0
    first = json.loads(capsys.readouterr().out)
    with open_store(root) as store:
        store.review_proposal(first["proposal"]["id"], "accept", "simulated-user")
        old = store.active_state("mid")[0]
    assert main([*base, "--text", "PostgreSQL", "--reason", "Shared service now needs concurrency"]) == 0
    correction = json.loads(capsys.readouterr().out)
    assert correction["change"]["before"] == {"text": "SQLite"}
    assert correction["change"]["after"]["text"] == "PostgreSQL"
    assert correction["change"]["supersedes_on_accept"] == old["id"]
    with open_store(root) as store:
        assert store.active_state("mid")[0]["content"] == {"text": "SQLite"}
        store.review_proposal(correction["proposal"]["id"], "accept", "simulated-user")
        assert len(store.active_state("mid")) == 1
        history = store.state_history("mid", "storage")
        assert len(history) == 2
        assert history[-1]["supersedes_id"] == old["id"]
        assert store.verify_integrity()["ok"]


def test_distinct_pending_corrections_never_silently_reject_older_candidate(tmp_path, capsys):
    root = project(tmp_path)
    base = ["--root", str(root), "remember", "--session", "s1", "--key", "storage"]
    assert main([*base, "--text", "SQLite"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert main([*base, "--text", "PostgreSQL"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["change"]["related_pending_ids"] == [first["proposal"]["id"]]
    assert status(root)["pending_review"]["total"] == 2
    with open_store(root) as store:
        assert store.get_proposal(first["proposal"]["id"])["reviews"] == []
        assert store.active_state() == []


def test_repeated_checkpoint_from_concurrent_bridge_processes_is_one_proposal(tmp_path):
    root = project(tmp_path)
    argv = [sys.executable, "-m", "noname_harness.codex_bridge", "--root", str(root),
            "remember", "--session", "s1", "--key", "checkpoint", "--text", "Tests passed"]
    workers = [subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
    outputs = []
    for worker in workers:
        stdout, stderr = worker.communicate(timeout=15)
        assert worker.returncode == 0, stderr.decode()
        outputs.append(json.loads(stdout))
    assert {item["status"] for item in outputs} == {"proposed", "already_pending"}
    assert len({item["proposal"]["id"] for item in outputs}) == 1
    assert status(root)["pending_review"]["total"] == 1
    assert status(root)["integrity"]["ok"]


@pytest.mark.parametrize("field", ["--reason", "--next-step", "--session"])
def test_empty_checkpoint_metadata_does_not_append_events(tmp_path, capsys, field):
    root = project(tmp_path)
    argv = ["--root", str(root), "remember", "--session", "s1", "--key", "checkpoint", "--text", "Done"]
    assert main([*argv, field, " "]) == 2
    assert "cannot be empty" in capsys.readouterr().err
    assert status(root)["event_count"] == 0
