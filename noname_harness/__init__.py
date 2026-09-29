"""A small, local-first prototype for evidence-backed agent handoffs."""

from .models import EvidenceInput, Event, ModelCapability, ModelProfile
from .curator import CuratorService
from .taste import TasteService
from .taste_cards import TasteCardService
from .plugins import Plugin, PluginContribution, PluginError, PluginManifest, PluginRuntime
from .recipes import Recipe, RoleSpec, resolve_recipe
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
    "TasteCardService",
    "Plugin",
    "PluginContribution",
    "PluginManifest",
    "PluginRuntime",
    "PluginError",
    "ModelCapability",
    "ModelProfile",
    "Recipe",
    "RoleSpec",
    "resolve_recipe",
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
