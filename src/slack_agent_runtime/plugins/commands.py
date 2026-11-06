from slack_agent_runtime.domain.commands import COMMAND_IDENTIFIERS
from slack_agent_runtime.plugins.api import (
    CommandContext,
    PluginDefinition,
    PluginRoute,
)
from slack_agent_runtime.services.command_executor import CommandExecutor
from slack_agent_runtime.services.thread_repository import ThreadRepository


def commands_plugin(executor: CommandExecutor, repository: ThreadRepository) -> PluginDefinition:
    async def handle(context: CommandContext) -> None:
        state = await repository.state(context.thread_key)
        handled = await executor.handle(
            context.prompt,
            state=state,
            thread_key=context.thread_key,
            thread_ts=context.thread_ts,
            say=context.say,
            client=context.client,
            user_id=context.user_id,
        )
        if not handled:
            raise RuntimeError("Registered command was not recognized")

    return PluginDefinition(
        "commands",
        tuple(PluginRoute.command(name, handle) for name in COMMAND_IDENTIFIERS),
    )
