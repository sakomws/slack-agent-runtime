import json
from types import MappingProxyType
from unittest.mock import AsyncMock

import pytest

from slack_agent_runtime.coding.github import CodingRun, PullRequestSummary
from slack_agent_runtime.domain.policy import InMemoryPolicy, PolicyConfig
from slack_agent_runtime.plugins.api import ActionContext, CommandContext, ThreadContext
from slack_agent_runtime.plugins.reviews import PullRequestReviewPlugin
from slack_agent_runtime.slack.payloads import SlackAction
from slack_agent_runtime.slack.ui import ACTION_PR_REVIEW


def _pull_request(*, draft: bool = False) -> PullRequestSummary:
    return PullRequestSummary(
        42,
        "Add feature",
        "octocat",
        draft,
        "open",
        "2026-11-06T00:00:00Z",
        "https://github.test/owner/repo/pull/42",
        "a" * 40,
    )


def _context(prompt: str, *, user_id: str = "admin") -> CommandContext:
    return CommandContext(
        prompt,
        ThreadContext("thread-model", False, True, False, MappingProxyType({}), 0),
        ("C1", "T1"),
        "T1",
        AsyncMock(),
        None,
        user_id,
        "W1",
        {},
        AsyncMock(),
    )


def _plugin(client: AsyncMock, inference: AsyncMock, *, policy=None):
    return PullRequestReviewPlugin(
        client,
        inference,
        policy=policy,
        event_type="slack_agent_pr_review",
        workflow_filename="pr-review.yml",
    )


def _route(plugin: PullRequestReviewPlugin, identifier: str):
    return next(route.handler for route in plugin.routes if route.identifier == identifier)


@pytest.mark.asyncio
async def test_prs_lists_allowlisted_pull_requests_with_review_buttons() -> None:
    client = AsyncMock()
    client.allowed_repositories = frozenset({"owner/repo"})
    client.list_pull_requests.return_value = (_pull_request(),)
    context = _context("prs owner/repo open page 2")

    await _route(_plugin(client, AsyncMock()), "prs")(context)

    client.list_pull_requests.assert_awaited_once_with(
        "owner/repo",
        state="open",
        page=2,
        per_page=10,
    )
    assert context.say.await_args.kwargs["blocks"][-1]["elements"][0]["text"]["text"] == "Review"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        "https://github.com/owner/repo",
        "<https://github.com/owner/repo>",
        "<https://github.com/owner/repo|github.com/owner/repo>",
    ],
)
async def test_prs_accepts_full_github_repository_url(target: str) -> None:
    client = AsyncMock()
    client.allowed_repositories = frozenset({"owner/repo"})
    client.list_pull_requests.return_value = ()
    context = _context(f"prs {target} closed page 3")

    await _route(_plugin(client, AsyncMock()), "prs")(context)

    client.list_pull_requests.assert_awaited_once_with(
        "owner/repo",
        state="closed",
        page=3,
        per_page=10,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        "https://github.example/owner/repo",
        "https://github.com/owner/repo/issues",
        "https://github.com/owner/repo?tab=readme",
    ],
)
async def test_prs_rejects_unsupported_or_ambiguous_urls(target: str) -> None:
    client = AsyncMock()
    context = _context(f"prs {target}")

    await _route(_plugin(client, AsyncMock()), "prs")(context)

    client.list_pull_requests.assert_not_awaited()
    assert "https://github.com/owner/repository" in context.say.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_review_validates_model_dispatches_and_stores_pointer() -> None:
    client = AsyncMock()
    client.allowed_repositories = frozenset({"owner/repo"})
    client.pull_request.return_value = _pull_request()
    inference = AsyncMock()
    inference.list_models.return_value = ("thread-model", "muse")
    context = _context("review owner/repo#42 model muse")

    await _route(_plugin(client, inference), "review")(context)

    dispatch = client.dispatch.await_args
    assert dispatch.args[0] == "owner/repo"
    assert dispatch.kwargs["event_type"] == "slack_agent_pr_review"
    assert dispatch.args[1]["model"] == "muse"
    state = context.host.set_plugin_state.await_args.args[2]
    assert set(state) == {
        "job_id",
        "repository",
        "pull_request_number",
        "head_sha",
        "model",
        "queued_at",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        "https://github.com/sakomws/everyballwins/pull/6",
        "<https://github.com/sakomws/everyballwins/pull/6>",
        "<https://github.com/sakomws/everyballwins/pull/6|github.com/sakomws/everyballwins/pull/6>",
    ],
)
async def test_review_accepts_full_github_pull_request_url(target: str) -> None:
    client = AsyncMock()
    client.allowed_repositories = frozenset({"sakomws/everyballwins"})
    client.pull_request.return_value = _pull_request()
    inference = AsyncMock()
    inference.list_models.return_value = ("thread-model",)
    context = _context(f"review {target}")

    await _route(_plugin(client, inference), "review")(context)

    client.pull_request.assert_awaited_once_with("sakomws/everyballwins", 6)
    client.dispatch.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        "https://github.example/sakomws/everyballwins/pull/6",
        "https://github.com/sakomws/everyballwins/issues/6",
        "https://github.com/sakomws/everyballwins/pull/6?diff=split",
    ],
)
async def test_review_rejects_unsupported_or_ambiguous_urls(target: str) -> None:
    client = AsyncMock()
    context = _context(f"review {target}")

    await _route(_plugin(client, AsyncMock()), "review")(context)

    client.pull_request.assert_not_awaited()
    assert (
        "https://github.com/owner/repository/pull/number" in (context.say.await_args.kwargs["text"])
    )


@pytest.mark.asyncio
async def test_review_button_dispatches_with_encoded_thread_model() -> None:
    client = AsyncMock()
    client.allowed_repositories = frozenset({"owner/repo"})
    client.pull_request.return_value = _pull_request()
    inference = AsyncMock()
    inference.list_models.return_value = ("thread-model",)
    host = AsyncMock()
    action = SlackAction(
        ACTION_PR_REVIEW,
        "C1",
        "T1",
        "M1",
        json.dumps({"repository": "owner/repo", "number": 42, "model": "thread-model"}),
    )
    context = ActionContext(action, {}, AsyncMock(), AsyncMock(), "W1", "admin", host)

    await _route(_plugin(client, inference), ACTION_PR_REVIEW)(context)

    client.dispatch.assert_awaited_once()
    host.set_plugin_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_review_status_and_admin_policy() -> None:
    client = AsyncMock()
    client.allowed_repositories = frozenset({"owner/repo"})
    client.status.return_value = CodingRun("completed", "success", "https://run", 1)
    inference = AsyncMock()
    policy = InMemoryPolicy(PolicyConfig(admin_user_ids=frozenset({"admin"})))
    plugin = _plugin(client, inference, policy=policy)

    denied = _context("prs owner/repo", user_id="member")
    await _route(plugin, "prs")(denied)
    assert "administrator" in denied.say.await_args.kwargs["text"]

    status = _context("review status")
    status.host.get_plugin_state.return_value = {
        "job_id": "job",
        "repository": "owner/repo",
    }
    await _route(plugin, "review")(status)
    client.status.assert_awaited_once_with(
        "owner/repo",
        "job",
        workflow_filename="pr-review.yml",
    )
    assert "success" in status.say.await_args.kwargs["text"]
