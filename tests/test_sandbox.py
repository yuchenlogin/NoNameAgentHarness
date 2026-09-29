"""Execution world / sandbox: physical containment + approval as second defence."""

from __future__ import annotations

import time

import pytest

from noname_harness.sandbox import Sandbox, SandboxError
from noname_harness.store import HarnessStore, WorkspaceBoundaryError
from noname_harness.tools import ToolApprovalRequired, ToolRegistry


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "sandbox project")
    return store, root


# --- file operations: physical workspace containment -------------------------

def test_read_write_inside_workspace_and_recorded(tmp_path):
    store, root = make_store(tmp_path)
    try:
        sandbox = Sandbox(store)
        (root / "src").mkdir()
        (root / "src" / "a.py").write_text("x = 1\n")
        out = sandbox.read_file("src/a.py", session_id="s")
        assert out["content"] == "x = 1\n"
        result = sandbox.write_file("src/b.py", "y = 2\n", session_id="s")
        assert result["path"] == "src/b.py"
        assert (root / "src" / "b.py").read_text() == "y = 2\n"
        # Both operations are evidence in the ledger.
        types = [e.event_type for e in store.list_events("s", limit=20)]
        assert "file.read" in types
        assert "artifact.changed" in types
    finally:
        store.close()


def test_file_ops_cannot_escape_workspace(tmp_path):
    store, root = make_store(tmp_path)
    try:
        sandbox = Sandbox(store)
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        for bad in ("../outside.txt", str(outside), "/etc/passwd", ".."):
            with pytest.raises(WorkspaceBoundaryError):
                sandbox.read_file(bad, session_id="s")
            with pytest.raises(WorkspaceBoundaryError):
                sandbox.write_file(bad, "x", session_id="s")
        # Nothing was written outside.
        assert outside.read_text() == "secret"
    finally:
        store.close()


def test_write_cannot_target_db_sidecars(tmp_path):
    store, root = make_store(tmp_path)
    try:
        sandbox = Sandbox(store)
        db = root / ".noname" / "harness.db"
        with pytest.raises(WorkspaceBoundaryError):
            sandbox.write_file(str(db), "corrupt", session_id="s")
        with pytest.raises(WorkspaceBoundaryError):
            sandbox.write_file(str(db) + "-wal", "corrupt", session_id="s")
    finally:
        store.close()


def test_read_missing_file_raises(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        sandbox = Sandbox(store)
        with pytest.raises(SandboxError):
            sandbox.read_file("does/not/exist.txt", session_id="s")
    finally:
        store.close()


# --- command execution: allow-list + timeout + capture -----------------------

def test_allowlisted_command_runs_and_captures(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        sandbox = Sandbox(store)
        result = sandbox.run_command(["echo", "hello"], session_id="s")
        assert result["returncode"] == 0
        assert "hello" in result["stdout"]
        types = [e.event_type for e in store.list_events("s", limit=10)]
        assert "tool.completed" in types
    finally:
        store.close()


def test_non_allowlisted_command_is_refused_before_running(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        sandbox = Sandbox(store)
        with pytest.raises(SandboxError):
            sandbox.run_command(["rm", "-rf", "/"], session_id="s")
        with pytest.raises(SandboxError):
            sandbox.run_command(["curl", "http://evil"], session_id="s")
        # Nothing ran.
        assert not any(
            e.event_type == "tool.completed" for e in store.list_events("s", limit=10)
        )
    finally:
        store.close()


def test_command_timeout_is_enforced(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        # Interpreters are opt-in (they weaken the boundary to approval-only);
        # a deployment that enables them still gets real timeout enforcement.
        sandbox = Sandbox(store, allowed_commands=("python3",), command_timeout=1)
        started = time.monotonic()
        with pytest.raises(SandboxError):
            sandbox.run_command(
                ["python3", "-c", "import time; time.sleep(30)"], session_id="s"
            )
        elapsed = time.monotonic() - started
        # Aborted near the 1s timeout, not after the full 30s sleep.
        assert elapsed < 10
        types = [e.event_type for e in store.list_events("s", limit=10)]
        assert "tool.failed" in types
    finally:
        store.close()


def test_command_failure_is_captured_not_raised(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        # git is allow-listed by default; a failing git command returns non-zero.
        sandbox = Sandbox(store)
        result = sandbox.run_command(["git", "status", "--porcelain=v"], session_id="s")
        assert result["returncode"] != 0
    finally:
        store.close()


# --- tools: approval gate as second line of defence --------------------------

def test_sandbox_tools_are_approval_gated(tmp_path):
    store, root = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        sandbox = Sandbox(store)
        names = sandbox.register_tools(registry, session_id="s")
        assert set(names) == {"sandbox.read_file", "sandbox.write_file", "sandbox.run_command"}

        (root / "a.txt").write_text("hi")
        # Read runs freely.
        assert "hi" in registry.request("sandbox.read_file", {"path": "a.txt"}, session_id="s")["output"]
        # Write needs approval.
        with pytest.raises(ToolApprovalRequired):
            registry.request("sandbox.write_file", {"path": "b.txt", "content": "x"}, session_id="s")
        assert not (root / "b.txt").exists()
        # Command execution is destructive and always gated.
        with pytest.raises(ToolApprovalRequired):
            registry.request("sandbox.run_command", {"command": ["echo", "x"]}, session_id="s")
        # With a token, the gated call runs.
        token = registry.grant_approval(
            "sandbox.write_file", {"path": "b.txt", "content": "x"}, approver_id="u", session_id="s"
        )
        registry.request(
            "sandbox.write_file", {"path": "b.txt", "content": "x"},
            session_id="s", approval_token=token,
        )
        assert (root / "b.txt").read_text() == "x"
    finally:
        store.close()


def test_sandbox_tools_confined_even_through_registry(tmp_path):
    store, root = make_store(tmp_path)
    try:
        registry = ToolRegistry(store)
        sandbox = Sandbox(store)
        sandbox.register_tools(registry, session_id="s")
        # Even with an approval token, a write outside the workspace is refused
        # by the sandbox boundary -- the two defences are independent.
        token = registry.grant_approval(
            "sandbox.write_file", {"path": "../evil.txt", "content": "x"},
            approver_id="u", session_id="s",
        )
        from noname_harness.tools import ToolError
        with pytest.raises(ToolError):
            registry.request(
                "sandbox.write_file", {"path": "../evil.txt", "content": "x"},
                session_id="s", approval_token=token,
            )
    finally:
        store.close()


# --- 对抗性审查发现的回归 ---

def test_interpreters_not_in_default_allowlist(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        sandbox = Sandbox(store)
        # An interpreter in the default allow-list would be arbitrary code
        # execution with the harness's full privileges -- it must be opt-in.
        for interpreter in ("python3", "python", "pytest", "sh", "bash", "node"):
            with pytest.raises(SandboxError):
                sandbox.run_command([interpreter, "-c", "x"], session_id="s")
    finally:
        store.close()


def test_command_with_path_separator_is_refused(tmp_path):
    store, root = make_store(tmp_path)
    try:
        sandbox = Sandbox(store)
        # A workspace-local fake 'git' must not run under the trusted name.
        fake = root / "git"
        fake.write_text("#!/bin/sh\necho PWNED\n")
        fake.chmod(0o755)
        for cmd in ("./git", "/usr/bin/git", "sub/dir/git"):
            with pytest.raises(SandboxError):
                sandbox.run_command([cmd, "status"], session_id="s")
    finally:
        store.close()


def test_write_refuses_symlinked_final_component(tmp_path):
    store, root = make_store(tmp_path)
    try:
        # A static symlink to an outside file is already refused at validation
        # (resolve escapes the workspace).  O_NOFOLLOW is the second barrier for
        # the TOCTOU window where the swap happens *after* validation.
        sandbox = Sandbox(store)
        outside = tmp_path / "outside.txt"
        outside.write_text("original")
        link = root / "link.txt"
        link.symlink_to(outside)
        with pytest.raises((WorkspaceBoundaryError, SandboxError)):
            sandbox.write_file("link.txt", "PWNED", session_id="s")
        assert outside.read_text() == "original"

        # Directly exercise the O_NOFOLLOW write: a symlink pointing *inside*
        # the workspace (passes validation) is still refused at open time.
        target_inside = root / "real.txt"
        target_inside.write_text("real")
        inside_link = root / "inside_link.txt"
        inside_link.symlink_to(target_inside)
        with pytest.raises(SandboxError):
            sandbox._write_text_nofollow(inside_link, "PWNED")
        assert target_inside.read_text() == "real"
    finally:
        store.close()


def test_db_journal_and_hardlink_protected(tmp_path):
    store, root = make_store(tmp_path)
    try:
        sandbox = Sandbox(store)
        db = root / ".noname" / "harness.db"
        # -journal sidecar refused by name.
        with pytest.raises(WorkspaceBoundaryError):
            sandbox.write_file(str(db) + "-journal", "x", session_id="s")
        # A hardlink to the database is refused by inode, not name.
        hardlink = root / "innocent.txt"
        import os
        os.link(db, hardlink)
        with pytest.raises(WorkspaceBoundaryError):
            sandbox.write_file("innocent.txt", "corrupt", session_id="s")
    finally:
        store.close()


def test_binary_file_read_does_not_crash_and_is_evidence(tmp_path):
    store, root = make_store(tmp_path)
    try:
        sandbox = Sandbox(store)
        (root / "bin.dat").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe binary \x00")
        result = sandbox.read_file("bin.dat", session_id="s")
        assert result["encoding"] == "hex"
        # The binary content is in the ledger (as hex), not a silent gap.
        event = next(e for e in store.list_events("s", limit=10) if e.event_type == "file.read")
        assert event.payload["encoding"] == "hex"
        evidence = store.evidence_for_event(event.id)[0]["content"]
        assert "89504e47" in evidence  # PNG magic in hex
        assert store.verify_integrity()["ok"] is True
    finally:
        store.close()


def test_truncation_is_marked_in_payload(tmp_path):
    store, root = make_store(tmp_path)
    try:
        sandbox = Sandbox(store, max_output_chars=100)
        (root / "big.txt").write_text("x" * 5000)
        sandbox.read_file("big.txt", session_id="s")
        event = next(e for e in store.list_events("s", limit=10) if e.event_type == "file.read")
        assert event.payload["truncated"] is True
        assert event.payload["evidence_chars"] == 100
    finally:
        store.close()


def test_nonzero_exit_records_tool_failed(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        sandbox = Sandbox(store)
        result = sandbox.run_command(["git", "definitely-not-a-command"], session_id="s")
        assert result["returncode"] != 0
        event = next(e for e in store.list_events("s", limit=10) if e.event_type.startswith("tool."))
        assert event.event_type == "tool.failed"
    finally:
        store.close()
