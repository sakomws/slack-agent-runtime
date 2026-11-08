from types import MappingProxyType
from unittest.mock import AsyncMock

import pytest

from slack_agent_runtime.coding.github import CodingRun
from slack_agent_runtime.domain.policy import InMemoryPolicy, PolicyConfig
from slack_agent_runtime.plugins.api import CommandContext, ThreadContext
from slack_agent_runtime.plugins.security_audit import SecurityAuditPlugin


def _context(prompt: str, *, user_id: str = "admin") -> CommandContext:
    return CommandContext(
        prompt,
        ThreadContext("model", False, True, False, MappingProxyType({}), 0),
        ("C1", "T1"),
        "T1",
        AsyncMock(),
        None,
        user_id,
        "W1",
        {},
        AsyncMock(),
    )


def _plugin(client: AsyncMock, *, policy=None) -> SecurityAuditPlugin:
    return SecurityAuditPlugin(
        client,
        policy=policy,
        event_type="slack_agent_security_audit",
        workflow_filename="security-audit.yml",
    )


def _route(plugin: SecurityAuditPlugin):
    return next(route.handler for route in plugin.routes if route.identifier == "security")


@pytest.mark.asyncio
async def test_security_audit_dispatches_and_stores_pointer() -> None:
    client = AsyncMock()
    context = _context(
        "security audit https://github.com/owner/repo profile quick budget 12 scope src"
    )

    await _route(_plugin(client))(context)

    dispatch = client.dispatch.await_args
    assert dispatch.args[0] == "owner/repo"
    assert dispatch.kwargs["event_type"] == "slack_agent_security_audit"
    assert dispatch.args[1]["profile"] == "quick"
    assert dispatch.args[1]["budget"] == "12"
    assert dispatch.args[1]["scope"] == "src"
    state = context.host.set_plugin_state.await_args.args[2]
    assert set(state) == {
        "job_id",
        "repository",
        "profile",
        "budget",
        "scope",
        "queued_at",
    }


@pytest.mark.asyncio
async def test_security_audit_accepts_slack_wrapped_repository_url() -> None:
    client = AsyncMock()
    context = _context("security audit <https://github.com/owner/repo>")

    await _route(_plugin(client))(context)

    client.dispatch.assert_awaited_once()
    assert client.dispatch.await_args.args[0] == "owner/repo"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt, expected",
    [
        ("security audit owner/repo profile fastest", "profile"),
        ("security audit owner/repo budget 7", "budget"),
        ("security audit owner/repo scope ../private", "repository-relative"),
        ("security audit https://github.example/owner/repo", "github.com"),
    ],
)
async def test_security_audit_rejects_invalid_options(prompt: str, expected: str) -> None:
    client = AsyncMock()
    context = _context(prompt)

    await _route(_plugin(client))(context)

    client.dispatch.assert_not_awaited()
    assert expected in context.say.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_security_audit_status_and_findings_link_workflow() -> None:
    client = AsyncMock()
    client.status.return_value = CodingRun("completed", "success", "https://run", 1)
    plugin = _plugin(client)
    status = _context("security audit status")
    status.host.get_plugin_state.return_value = {
        "job_id": "job",
        "repository": "owner/repo",
    }

    await _route(plugin)(status)

    client.status.assert_awaited_once_with(
        "owner/repo",
        "job",
        workflow_filename="security-audit.yml",
    )
    assert "success" in status.say.await_args.kwargs["text"]

    findings = _context("security audit findings")
    findings.host.get_plugin_state.return_value = status.host.get_plugin_state.return_value
    await _route(plugin)(findings)
    assert "download its report artifact" in findings.say.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_security_audit_requires_admin() -> None:
    client = AsyncMock()
    policy = InMemoryPolicy(PolicyConfig(admin_user_ids=frozenset({"admin"})))
    context = _context("security audit owner/repo", user_id="member")

    await _route(_plugin(client, policy=policy))(context)

    client.dispatch.assert_not_awaited()
    assert "administrator" in context.say.await_args.kwargs["text"]
