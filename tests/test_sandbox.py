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
        sandbox = Sandbox(store, command_timeout=1)
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
        sandbox = Sandbox(store)
        result = sandbox.run_command(
            ["python3", "-c", "import sys; sys.exit(3)"], session_id="s"
        )
        assert result["returncode"] == 3
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
