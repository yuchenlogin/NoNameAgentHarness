"""A small, local-first prototype for evidence-backed agent handoffs."""

from .models import EvidenceInput, Event
from .curator import CuratorService
from .context import render_markdown
from .store import HarnessStore, WorkspaceBoundaryError

__all__ = [
    "CuratorService",
    "EvidenceInput",
    "Event",
    "HarnessStore",
    "WorkspaceBoundaryError",
    "render_markdown",
]
