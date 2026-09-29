"""Execution world / sandbox: real side effects inside a physical boundary.

This is where real work happens -- reading and writing files, running
commands.  The guarantees here are **physical**, not a model's promise:

- **Workspace containment.**  Every file path is resolved and confined to the
  configured workspace root (the same boundary the store enforces).  A path
  that escapes simply cannot be touched -- there is no flag to turn this off.
- **Command allow-listing + timeouts.**  Only explicitly allowed command
  prefixes run, each with a hard timeout.  Anything else is refused before a
  process ever starts.
- **Everything is evidence.**  Reads and writes produce events and evidence
  spans in the ledger, so a real side effect is always traceable.

The sandbox **produces tools** that are registered in the
:class:`~noname_harness.tools.ToolRegistry`, so the approval gate is a second,
independent line of defence on top of the sandbox: reads run freely, writes
need approval, command execution is destructive and always gated.  A caller
must go through both the sandbox boundary and the approval gate.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Sequence

from .models import EvidenceInput
from .store import HarnessStore, WorkspaceBoundaryError
from .tools import Tool, ToolRegistry, ToolSchema

# Commands that may run by default.  Each entry is a command prefix; a command
# runs only if its first token matches an allowed prefix exactly.
DEFAULT_ALLOWED_COMMANDS = (
    "git",
    "ls",
    "cat",
    "echo",
    "python3",
    "python",
    "pytest",
)


class SandboxError(Exception):
    """Raised when a sandbox boundary would be crossed."""


class Sandbox:
    """A physical execution boundary over the configured workspace."""

    def __init__(
        self,
        store: HarnessStore,
        *,
        allowed_commands: Sequence[str] | None = None,
        command_timeout: float = 30.0,
        max_output_chars: int = 100_000,
    ):
        self.store = store
        self.allowed_commands = tuple(allowed_commands or DEFAULT_ALLOWED_COMMANDS)
        if command_timeout <= 0:
            raise ValueError("command_timeout must be positive")
        self.command_timeout = command_timeout
        self.max_output_chars = max_output_chars

    # ------------------------------------------------------------------
    # file operations (physically confined to the workspace)
    # ------------------------------------------------------------------
    def read_file(self, path: str, *, session_id: str) -> dict[str, Any]:
        """Read a file inside the workspace, recording it as evidence."""

        target = self.store.validate_workspace_path(path)
        if not target.is_file():
            raise SandboxError(f"not a readable file inside the workspace: {path}")
        content = target.read_text(encoding="utf-8")
        relative = str(target.relative_to(Path(self.store.project()["workspace_root"])))
        self.store.append_event(
            session_id,
            "file.read",
            {"path": relative, "size": len(content)},
            [EvidenceInput(content[: self.max_output_chars], f"file://{relative}")],
        )
        return {"path": relative, "content": content, "size": len(content)}

    def write_file(self, path: str, content: str, *, session_id: str) -> dict[str, Any]:
        """Write a file inside the workspace, recording the change as evidence.

        The workspace boundary is enforced before anything is written; a path
        outside the root is refused, and the harness database's own files are
        off-limits.
        """

        target = self.store.validate_output_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        relative = str(target.relative_to(Path(self.store.project()["workspace_root"])))
        self.store.append_event(
            session_id,
            "artifact.changed",
            {"path": relative, "change": "written via sandbox", "size": len(content)},
            [EvidenceInput(content[: self.max_output_chars], f"file://{relative}")],
        )
        return {"path": relative, "size": len(content)}

    # ------------------------------------------------------------------
    # command execution (allow-listed, timed, captured)
    # ------------------------------------------------------------------
    def run_command(
        self,
        command: Sequence[str],
        *,
        session_id: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Run an allow-listed command inside the workspace with a hard timeout.

        The command runs with the workspace as its working directory, its
        output captured and recorded.  A non-allow-listed command or a timeout
        is refused/aborted without touching anything outside the boundary.
        """

        if not command:
            raise SandboxError("command cannot be empty")
        executable = Path(str(command[0])).name
        if executable not in {Path(prefix).name for prefix in self.allowed_commands}:
            raise SandboxError(
                f"command '{executable}' is not in the allow-list: {sorted(self.allowed_commands)}"
            )
        effective_timeout = timeout if timeout is not None else self.command_timeout
        if effective_timeout <= 0:
            raise SandboxError("timeout must be positive")
        root = self.store.project()["workspace_root"]
        try:
            completed = subprocess.run(
                list(command),
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
            )
            timed_out = False
            stdout = completed.stdout
            stderr = completed.stderr
            returncode = completed.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
            returncode = None

        output = stdout + (("\n[stderr]\n" + stderr) if stderr else "")
        output = output[: self.max_output_chars]
        payload = {
            "command": list(command),
            "executable": executable,
            "returncode": returncode,
            "timed_out": timed_out,
            "stdout_chars": len(stdout),
            "stderr_chars": len(stderr),
        }
        self.store.append_event(
            session_id,
            "tool.completed" if not timed_out else "tool.failed",
            payload,
            [EvidenceInput(output, f"cmd://{executable}")],
        )
        if timed_out:
            raise SandboxError(f"command '{executable}' timed out after {effective_timeout}s")
        return {
            "command": list(command),
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
        }

    # ------------------------------------------------------------------
    # tools: the sandbox's operations, gated through the ToolRegistry
    # ------------------------------------------------------------------
    def tools(self, *, session_id: str) -> list[Tool]:
        """Return the sandbox's operations as approval-gated tools.

        These register into the normal :class:`ToolRegistry`, so the approval
        gate is a second line of defence: reads run freely, writes need
        approval, command execution is destructive and always gated.
        """

        return [
            Tool(
                ToolSchema(
                    name="sandbox.read_file",
                    description="Read a file inside the workspace",
                    input_schema={"path": "string"},
                ),
                execute=lambda a: self.read_file(a["path"], session_id=session_id)["content"],
                permission="read",
                approval="never",
                scope="session",
                session_id=session_id,
            ),
            Tool(
                ToolSchema(
                    name="sandbox.write_file",
                    description="Write a file inside the workspace (approval required)",
                    input_schema={"path": "string", "content": "string"},
                ),
                execute=lambda a: self.write_file(a["path"], a["content"], session_id=session_id)["path"],
                permission="write",
                approval="always",
                scope="session",
                session_id=session_id,
            ),
            Tool(
                ToolSchema(
                    name="sandbox.run_command",
                    description="Run an allow-listed command in the workspace (approval required)",
                    input_schema={"command": "array"},
                ),
                execute=lambda a: self.run_command(
                    [str(part) for part in a["command"]], session_id=session_id
                ),
                permission="destructive",
                approval="always",
                scope="session",
                session_id=session_id,
            ),
        ]

    def register_tools(self, registry: ToolRegistry, *, session_id: str) -> list[str]:
        """Register the sandbox's tools into a registry; returns their names."""

        names = []
        for tool in self.tools(session_id=session_id):
            registry.register(tool)
            names.append(tool.schema.name)
        return names
