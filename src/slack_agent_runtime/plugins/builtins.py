"""Factories for behavior shipped with the bot."""

from slack_agent_runtime.plugins.api import Plugin
from slack_agent_runtime.plugins.commands import commands_plugin
from slack_agent_runtime.plugins.conversation import conversation_plugin
from slack_agent_runtime.services.conversation_coordinator import (
    ConversationCoordinator,
    ConversationDependencies,
)


def builtin_plugins(
    coordinator: ConversationCoordinator,
    dependencies: ConversationDependencies,
    *,
    additional: tuple[Plugin, ...] = (),
) -> tuple[Plugin, ...]:
    """Return built-ins in deterministic startup and routing order."""

    return (
        commands_plugin(dependencies.commands, dependencies.repository),
        conversation_plugin(coordinator),
        dependencies.feedback_plugin,
        dependencies.assistant_plugin,
        *additional,
    )


__all__ = ["builtin_plugins"]
