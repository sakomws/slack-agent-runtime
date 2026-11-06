"""Built-in commands and Slack approvals for direct MCP tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

from slack_agent_runtime.domain.commands import HelpEntry
from slack_agent_runtime.plugins.api import ActionContext, CommandContext, PluginRoute
from slack_agent_runtime.slack.ui import ACTION_MCP_APPROVE, ACTION_MCP_DENY

if TYPE_CHECKING:
    from slack_agent_runtime.domain.policy import InMemoryPolicy
    from slack_agent_runtime.mcp.approval import ApprovalBroker
    from slack_agent_runtime.mcp.manager import McpManager


class McpPlugin:
    name = "mcp"
    help_entries = (
        HelpEntry("MCP tools", "tools", "list tools and their approval behavior"),
        HelpEntry("MCP tools", "mcp status", "show MCP connection and tool counts"),
        HelpEntry("MCP tools", "mcp servers", "list connected MCP servers"),
    )

    def __init__(
        self,
        manager: McpManager,
        approvals: ApprovalBroker,
        *,
        policy: InMemoryPolicy | None,
    ) -> None:
        self._manager = manager
        self._approvals = approvals
        self._policy = policy
        self.routes = (
            PluginRoute.command("tools", self._command),
            PluginRoute.command("mcp", self._command),
            PluginRoute.action(ACTION_MCP_APPROVE, self._action),
            PluginRoute.action(ACTION_MCP_DENY, self._action),
        )

    async def start(self) -> None:
        await self._manager.start()

    async def close(self) -> None:
        self._approvals.cancel_all()
        await self._manager.close()

    async def _command(self, context: CommandContext) -> None:
        normalized = " ".join(context.prompt.lower().split())
        if normalized == "tools":
            tools = self._manager.tools
            if not tools:
                text = "No MCP tools are available."
            else:
                entries = [
                    (
                        f"• `{tool.server_name}/{tool.tool_name}` — "
                        + ("automatic read-only" if tool.auto_approved else "approval required")
                    )
                    for tool in tools
                ]
                text = "*MCP tools*\n" + "\n".join(entries)
        elif normalized == "mcp status":
            text = (
                f"MCP is connected to {len(self._manager.server_names)} server(s) "
                f"with {len(self._manager.tools)} available tool(s)."
            )
        elif normalized == "mcp servers":
            servers = self._manager.server_names
            text = (
                "*MCP servers*\n" + "\n".join(f"• `{server}`" for server in servers)
                if servers
                else "No MCP servers are configured."
            )
        else:
            text = "Use `tools`, `mcp status`, or `mcp servers`."
        await context.say(text=text, thread_ts=context.thread_ts)

    async def _action(self, context: ActionContext) -> None:
        if self._policy is not None:
            decision = self._policy.check_admin(context.user_id)
            if not decision.allowed:
                await context.say(text=decision.message, thread_ts=context.action.thread_ts)
                return
        approved = context.action.action_id == ACTION_MCP_APPROVE
        resolved = self._approvals.resolve(
            context.action.value,
            approved=approved,
            thread_key=(context.action.channel, context.action.thread_ts),
        )
        status = (
            ("approved" if approved else "denied") if resolved else "already resolved or expired"
        )
        await context.say(
            text=f"MCP tool request {status}.",
            thread_ts=context.action.thread_ts,
        )
