"""A small, local-first prototype for evidence-backed agent handoffs."""

from .models import EvidenceInput, Event
from .curator import CuratorService
from .taste import TasteService
from .agent_loop import AgentLoop, AgentLoopError, LoopResult, SessionDriver
from .tools import (
    ApprovalToken,
    Tool,
    ToolApprovalRequired,
    ToolError,
    ToolRegistry,
    ToolSchema,
    ToolShadowingError,
    ToolValidationError,
)
from .context import render_markdown
from .store import HarnessStore, WorkspaceBoundaryError

__all__ = [
    "AgentLoop",
    "AgentLoopError",
    "CuratorService",
    "LoopResult",
    "SessionDriver",
    "TasteService",
    "ApprovalToken",
    "Tool",
    "ToolApprovalRequired",
    "ToolError",
    "ToolRegistry",
    "ToolSchema",
    "ToolShadowingError",
    "ToolValidationError",
    "EvidenceInput",
    "Event",
    "HarnessStore",
    "WorkspaceBoundaryError",
    "render_markdown",
]
