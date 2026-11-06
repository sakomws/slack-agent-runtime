"""Stable public API for building and loading bot plugins."""

from slack_agent_runtime.domain.commands import HelpEntry
from slack_agent_runtime.plugins.api import (
    ENTRY_POINT_GROUP,
    ActionContext,
    ActionHandler,
    CommandContext,
    CommandHandler,
    EventContext,
    EventHandler,
    HandlerResult,
    Plugin,
    PluginDefinition,
    PluginHost,
    PluginRoute,
    ThreadContext,
    ViewContext,
    ViewHandler,
)
from slack_agent_runtime.plugins.registry import (
    PluginRegistry,
    load_plugin_registry,
)

__all__ = [
    "ENTRY_POINT_GROUP",
    "ActionContext",
    "ActionHandler",
    "CommandContext",
    "CommandHandler",
    "EventContext",
    "EventHandler",
    "HandlerResult",
    "HelpEntry",
    "Plugin",
    "PluginDefinition",
    "PluginHost",
    "PluginRegistry",
    "PluginRoute",
    "ThreadContext",
    "ViewContext",
    "ViewHandler",
    "load_plugin_registry",
]
