from __future__ import annotations

from slack_agent_runtime.plugins import (
    CommandContext,
    PluginDefinition,
    PluginRoute,
)


def create_plugin() -> PluginDefinition:
    async def echo(context: CommandContext) -> None:
        previous = await context.host.get_plugin_state(context.thread_key, "echo")
        count = (
            previous.get("count", 0)
            if isinstance(previous, dict) and isinstance(previous.get("count"), int)
            else 0
        )
        await context.host.set_plugin_state(context.thread_key, "echo", {"count": count + 1})
        message = context.prompt.partition(" ")[2].strip() or "(nothing to echo)"
        await context.say(text=f"Echo: {message}", thread_ts=context.thread_ts)

    return PluginDefinition(
        name="echo",
        routes=(PluginRoute.command("echo", echo),),
    )
