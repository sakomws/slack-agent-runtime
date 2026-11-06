from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from slack_agent_runtime.coding.github import CodingJobError, CodingRun
from slack_agent_runtime.domain.policy import InMemoryPolicy, PolicyConfig
from slack_agent_runtime.inference.client import InferenceResult, InferenceUsage
from slack_agent_runtime.mcp.manager import McpTool
from slack_agent_runtime.plugins.api import CommandContext, ThreadContext
from slack_agent_runtime.plugins.coding import CodingPlugin


def _context(prompt: str, *, user_id: str = "admin") -> CommandContext:
    return CommandContext(
        prompt=prompt,
        thread=ThreadContext("model", False, True, False, MappingProxyType({}), 0),
        thread_key=("C1", "T1"),
        thread_ts="T1",
        say=AsyncMock(),
        client=None,
        user_id=user_id,
        workspace_id="W1",
        event={},
        host=AsyncMock(),
    )


def _handler(plugin: CodingPlugin):
    return plugin.routes[0].handler


def _spec_handler(plugin: CodingPlugin):
    return plugin.routes[1].handler


@pytest.mark.asyncio
async def test_coding_command_is_admin_only_and_enforces_allowlist() -> None:
    client = AsyncMock()
    client.allowed_repositories = frozenset({"owner/repo"})
    plugin = CodingPlugin(
        client,
        policy=InMemoryPolicy(PolicyConfig(admin_user_ids=frozenset({"admin"}))),
        max_prompt_chars=100,
    )
    denied = _context("code owner/repo request", user_id="member")
    await _handler(plugin)(denied)
    assert "administrator" in denied.say.await_args.kwargs["text"]
    client.dispatch.assert_not_awaited()

    unlisted = _context("code other/repo request")
    await _handler(plugin)(unlisted)
    assert "not allowlisted" in unlisted.say.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_coding_command_dispatches_and_stores_only_job_pointer() -> None:
    client = AsyncMock()
    client.allowed_repositories = frozenset({"owner/repo"})
    context = _context("code owner/repo add tests")
    plugin = CodingPlugin(client, policy=None, max_prompt_chars=100)

    await _handler(plugin)(context)

    payload = client.dispatch.await_args.args[1]
    assert payload["prompt"] == "add tests"
    assert payload["channel"] == "C1"
    state = context.host.set_plugin_state.await_args.args[2]
    assert set(state) == {"job_id", "repository", "queued_at"}
    assert "prompt" not in state


@pytest.mark.asyncio
async def test_coding_status_reads_github_actions_source_of_truth() -> None:
    client = AsyncMock()
    client.allowed_repositories = frozenset({"owner/repo"})
    client.status.return_value = CodingRun(
        "completed",
        "success",
        "https://github.test/run/1",
        1,
    )
    context = _context("code status")
    context.host.get_plugin_state.return_value = {
        "job_id": "abc",
        "repository": "owner/repo",
        "queued_at": "now",
    }

    await _handler(CodingPlugin(client, policy=None, max_prompt_chars=100))(context)

    client.status.assert_awaited_once_with("owner/repo", "abc")
    assert "success" in context.say.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_failed_dispatch_is_safe_and_does_not_store_state() -> None:
    client = AsyncMock()
    client.allowed_repositories = frozenset({"owner/repo"})
    client.dispatch.side_effect = CodingJobError("GitHub coding-job dispatch failed")
    context = _context("code owner/repo add tests")

    await _handler(CodingPlugin(client, policy=None, max_prompt_chars=100))(context)

    context.host.set_plugin_state.assert_not_awaited()
    assert context.say.await_args.kwargs["text"] == "GitHub coding-job dispatch failed"


@pytest.mark.asyncio
async def test_notion_spec_is_grounded_then_dispatched_without_persisting_content() -> None:
    client = AsyncMock()
    client.allowed_repositories = frozenset({"owner/repo"})
    runner = AsyncMock()
    runner.run.return_value = InferenceResult(
        (
            "Requirement: add the import endpoint.\n"
            "Acceptance: tests pass.\n"
            "Example token: ghp_abcdefghijklmnop"
        ),
        "model",
        "request",
        10,
        InferenceUsage(),
    )
    notion_tool = McpTool(
        "mcp__notion__fetch",
        "notion",
        "fetch",
        "Fetch page",
        {},
        read_only=True,
        auto_approved=False,
    )
    context = _context(
        "code owner/repo from notion <https://app.notion.com/p/spec-123|app.notion.com/p/spec-123>"
    )
    plugin = CodingPlugin(
        client,
        policy=None,
        max_prompt_chars=1_000,
        agent_runner=runner,
        mcp_manager=SimpleNamespace(tools=(notion_tool,)),
    )

    await _handler(plugin)(context)

    run_kwargs = runner.run.await_args.kwargs
    assert run_kwargs["allowed_tool_names"] == frozenset({"mcp__notion__fetch"})
    assert run_kwargs["required_tool_names"] == frozenset({"mcp__notion__fetch"})
    assert run_kwargs["messages"][1]["content"] == (
        "Notion specification URL: https://app.notion.com/p/spec-123"
    )
    dispatch_payload = client.dispatch.await_args.args[1]
    assert "Grounded requirements" in dispatch_payload["prompt"]
    assert "add the import endpoint" in dispatch_payload["prompt"]
    assert "ghp_" not in dispatch_payload["prompt"]
    state = context.host.set_plugin_state.await_args.args[2]
    assert "prompt" not in state


@pytest.mark.asyncio
async def test_notion_source_requires_valid_url_and_configured_server() -> None:
    client = AsyncMock()
    client.allowed_repositories = frozenset({"owner/repo"})

    invalid = _context("code owner/repo from notion https://example.com/spec")
    await _handler(CodingPlugin(client, policy=None, max_prompt_chars=1_000))(invalid)
    assert "valid HTTPS" in invalid.say.await_args.kwargs["text"]

    lookalike = _context("code owner/repo from notion https://app.notion.com.example.com/spec")
    await _handler(CodingPlugin(client, policy=None, max_prompt_chars=1_000))(lookalike)
    assert "valid HTTPS" in lookalike.say.await_args.kwargs["text"]

    missing = _context("code owner/repo from notion https://app.notion.com/p/spec")
    await _handler(CodingPlugin(client, policy=None, max_prompt_chars=1_000))(missing)
    assert "not configured" in missing.say.await_args.kwargs["text"]
    client.dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_spec_review_uses_grounded_notion_tools_and_editorial_checklist() -> None:
    client = AsyncMock()
    client.allowed_repositories = frozenset({"owner/repo"})
    runner = AsyncMock()
    runner.run.return_value = InferenceResult(
        "## Conclusion\nRewrite the opening.\n\n## Facts\n- Launch date is documented.",
        "model",
        "request",
        10,
        InferenceUsage(),
    )
    notion_tool = McpTool(
        "mcp__notion__fetch",
        "notion",
        "fetch",
        "Fetch page",
        {},
        read_only=True,
        auto_approved=False,
    )
    context = _context(
        "spec review from notion <https://app.notion.com/p/spec-123|app.notion.com/p/spec-123>"
    )
    plugin = CodingPlugin(
        client,
        policy=None,
        max_prompt_chars=8_000,
        agent_runner=runner,
        mcp_manager=SimpleNamespace(tools=(notion_tool,)),
    )

    await _spec_handler(plugin)(context)

    run_kwargs = runner.run.await_args.kwargs
    assert run_kwargs["allowed_tool_names"] == frozenset({"mcp__notion__fetch"})
    assert run_kwargs["required_tool_names"] == frozenset({"mcp__notion__fetch"})
    assert run_kwargs["messages"][1]["content"] == (
        "Notion specification URL: https://app.notion.com/p/spec-123"
    )
    instructions = run_kwargs["messages"][0]["content"]
    assert "Define the audience and purpose" in instructions
    assert "Separate facts, assumptions, decisions, and open questions" in instructions
    assert "never invent them" in instructions
    assert context.say.await_args.kwargs["text"].startswith("## Conclusion")
    client.dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_spec_review_validates_syntax_and_admin_access() -> None:
    client = AsyncMock()
    client.allowed_repositories = frozenset({"owner/repo"})
    plugin = CodingPlugin(
        client,
        policy=InMemoryPolicy(PolicyConfig(admin_user_ids=frozenset({"admin"}))),
        max_prompt_chars=8_000,
    )

    denied = _context(
        "spec review from notion https://www.notion.so/spec-123",
        user_id="member",
    )
    await _spec_handler(plugin)(denied)
    assert "administrator" in denied.say.await_args.kwargs["text"]

    invalid = _context("spec review https://www.notion.so/spec-123")
    await _spec_handler(plugin)(invalid)
    assert invalid.say.await_args.kwargs["text"] == ("Use `spec review from notion <page URL>`.")
