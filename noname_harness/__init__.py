"""A small, local-first prototype for evidence-backed agent handoffs."""

from .models import EvidenceInput, Event, ModelCapability, ModelProfile
from .curator import CuratorService
from .taste import TasteService
from .taste_cards import TasteCardService
from .plugins import Plugin, PluginContribution, PluginError, PluginManifest, PluginRuntime
from .sandbox import Sandbox, SandboxError
from .router import RouteDecision, Router
from .openai_adapter import OpenAIAdapter, load_openai_adapter
from .anthropic_adapter import AnthropicAdapter, load_anthropic_adapter
from .embeddings import local_hash_embedding, cosine_similarity
from .card_images import GeneratedImage, local_typographic_image, card_image_for
from .extractor import ExtractionCandidate, MemoryExtractor, rule_based_extractor
from .llm_extractor import LLMExtractor, make_llm_extractor
from .adapters import (
    AdapterDriver,
    LocalEchoAdapter,
    ModelAdapterError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
)
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
    "Sandbox",
    "SandboxError",
    "RouteDecision",
    "Router",
    "AdapterDriver",
    "OpenAIAdapter",
    "AnthropicAdapter",
    "load_anthropic_adapter",
    "local_hash_embedding",
    "cosine_similarity",
    "GeneratedImage",
    "local_typographic_image",
    "card_image_for",
    "ExtractionCandidate",
    "MemoryExtractor",
    "rule_based_extractor",
    "LLMExtractor",
    "make_llm_extractor",
    "load_openai_adapter",
    "LocalEchoAdapter",
    "ModelAdapterError",
    "ModelMessage",
    "ModelRequest",
    "ModelResponse",
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
