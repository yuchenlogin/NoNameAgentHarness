"""A small, local-first prototype for evidence-backed agent handoffs."""

from .models import EvidenceInput, Event
from .curator import CuratorService
from .taste import TasteService
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
    "CuratorService",
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
