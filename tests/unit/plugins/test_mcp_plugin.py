from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from slack_agent_runtime.domain.policy import InMemoryPolicy, PolicyConfig
from slack_agent_runtime.mcp.approval import ApprovalBroker
from slack_agent_runtime.mcp.manager import McpTool
from slack_agent_runtime.plugins.api import ActionContext, CommandContext, ThreadContext
from slack_agent_runtime.plugins.mcp import McpPlugin
from slack_agent_runtime.slack.payloads import SlackAction
from slack_agent_runtime.slack.ui import ACTION_MCP_APPROVE, ACTION_MCP_DENY


def _tool(*, automatic: bool) -> McpTool:
    return McpTool(
        "mcp__server__lookup",
        "server",
        "lookup",
        "Lookup",
        {},
        read_only=True,
        auto_approved=automatic,
    )


def _command_context(prompt: str, say: AsyncMock) -> CommandContext:
    return CommandContext(
        prompt=prompt,
        thread=ThreadContext("model", False, True, False, MappingProxyType({}), 0),
        thread_key=("C1", "T1"),
        thread_ts="T1",
        say=say,
        client=None,
        user_id="U1",
        workspace_id="W1",
        event={},
        host=AsyncMock(),
    )


def _action_context(
    action_id: str,
    value: str,
    say: AsyncMock,
    *,
    user_id: str = "admin",
) -> ActionContext:
    action = SlackAction(action_id, "C1", "T1", "M1", value)
    return ActionContext(action, {}, say, AsyncMock(), "W1", user_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("tools", "*MCP tools*\n• `server/lookup` — automatic read-only"),
        ("  MCP   STATUS ", "MCP is connected to 1 server(s) with 1 available tool(s)."),
        ("mcp servers", "*MCP servers*\n• `server`"),
        ("mcp unknown", "Use `tools`, `mcp status`, or `mcp servers`."),
    ],
)
async def test_mcp_commands_report_tools_servers_and_status(prompt: str, expected: str) -> None:
    manager = SimpleNamespace(
        tools=(_tool(automatic=True),),
        server_names=("server",),
        start=AsyncMock(),
        close=AsyncMock(),
    )
    plugin = McpPlugin(manager, ApprovalBroker(timeout_seconds=1), policy=None)
    say = AsyncMock()
    handler = next(route.handler for route in plugin.routes if route.identifier == "mcp")

    await handler(_command_context(prompt, say))

    say.assert_awaited_once_with(text=expected, thread_ts="T1")


@pytest.mark.asyncio
async def test_tools_command_describes_approval_requirement_and_empty_state() -> None:
    manager = SimpleNamespace(
        tools=(_tool(automatic=False),),
        server_names=(),
        start=AsyncMock(),
        close=AsyncMock(),
    )
    plugin = McpPlugin(manager, ApprovalBroker(timeout_seconds=1), policy=None)
    say = AsyncMock()
    handler = next(route.handler for route in plugin.routes if route.identifier == "tools")

    await handler(_command_context("tools", say))
    assert "approval required" in say.await_args.kwargs["text"]

    manager.tools = ()
    await handler(_command_context("tools", say))
    assert say.await_args.kwargs["text"] == "No MCP tools are available."


@pytest.mark.asyncio
async def test_approval_actions_resolve_matching_request_once() -> None:
    manager = SimpleNamespace(tools=(), server_names=(), start=AsyncMock(), close=AsyncMock())
    approvals = ApprovalBroker(timeout_seconds=1)
    plugin = McpPlugin(manager, approvals, policy=None)
    pending = approvals.create(_tool(automatic=False), requester_id="U1", thread_key=("C1", "T1"))
    say = AsyncMock()
    handler = next(
        route.handler for route in plugin.routes if route.identifier == ACTION_MCP_APPROVE
    )

    await handler(_action_context(ACTION_MCP_APPROVE, pending.id, say))
    assert await approvals.wait(pending.id) is True
    assert say.await_args.kwargs["text"] == "MCP tool request approved."

    await handler(_action_context(ACTION_MCP_APPROVE, pending.id, say))
    assert say.await_args.kwargs["text"] == "MCP tool request already resolved or expired."


@pytest.mark.asyncio
async def test_deny_action_and_admin_policy_are_enforced() -> None:
    manager = SimpleNamespace(tools=(), server_names=(), start=AsyncMock(), close=AsyncMock())
    approvals = ApprovalBroker(timeout_seconds=1)
    policy = InMemoryPolicy(PolicyConfig(admin_user_ids=frozenset({"admin"})))
    plugin = McpPlugin(manager, approvals, policy=policy)
    pending = approvals.create(_tool(automatic=False), requester_id="U1", thread_key=("C1", "T1"))
    say = AsyncMock()
    handler = next(route.handler for route in plugin.routes if route.identifier == ACTION_MCP_DENY)

    await handler(_action_context(ACTION_MCP_DENY, pending.id, say, user_id="not-admin"))
    assert "administrator" in say.await_args.kwargs["text"]
    assert approvals.resolve(
        pending.id,
        approved=False,
        thread_key=("C1", "T1"),
    )
    assert await approvals.wait(pending.id) is False

    second = approvals.create(_tool(automatic=False), requester_id="U1", thread_key=("C1", "T1"))
    await handler(_action_context(ACTION_MCP_DENY, second.id, say))
    assert await approvals.wait(second.id) is False
    assert say.await_args.kwargs["text"] == "MCP tool request denied."


@pytest.mark.asyncio
async def test_plugin_lifecycle_starts_manager_and_cancels_approvals_on_close() -> None:
    manager = SimpleNamespace(tools=(), server_names=(), start=AsyncMock(), close=AsyncMock())
    approvals = ApprovalBroker(timeout_seconds=10)
    plugin = McpPlugin(manager, approvals, policy=None)
    pending = approvals.create(_tool(automatic=False), requester_id=None, thread_key=("C1", "T1"))

    await plugin.start()
    await plugin.close()

    manager.start.assert_awaited_once_with()
    manager.close.assert_awaited_once_with()
    assert not approvals.resolve(
        pending.id,
        approved=True,
        thread_key=("C1", "T1"),
    )
