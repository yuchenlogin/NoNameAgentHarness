"""A small, local-first prototype for evidence-backed agent handoffs."""

from .models import EvidenceInput, Event
from .curator import CuratorService
from .taste import TasteService
from .tools import Tool, ToolRegistry, ToolSchema
from .context import render_markdown
from .store import HarnessStore, WorkspaceBoundaryError

__all__ = [
    "CuratorService",
    "TasteService",
    "Tool",
    "ToolRegistry",
    "ToolSchema",
    "EvidenceInput",
    "Event",
    "HarnessStore",
    "WorkspaceBoundaryError",
    "render_markdown",
]
