from slack_agent_runtime.plugins.api import (
    EventContext,
    PluginDefinition,
    PluginRoute,
)
from slack_agent_runtime.services.conversation_coordinator import ConversationCoordinator


def conversation_plugin(coordinator: ConversationCoordinator) -> PluginDefinition:
    async def mention(context: EventContext) -> None:
        await coordinator.handle(
            dict(context.event),
            context.say,
            client=context.client,
            workspace_id=context.workspace_id,
        )

    async def direct_message(context: EventContext) -> None:
        if context.event.get("channel_type") == "im":
            await coordinator.handle(
                dict(context.event),
                context.say,
                client=context.client,
                workspace_id=context.workspace_id,
            )

    return PluginDefinition(
        "conversation",
        (
            PluginRoute.event("app_mention", mention),
            PluginRoute.event("message", direct_message),
        ),
    )
