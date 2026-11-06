import asyncio
from unittest.mock import AsyncMock

import pytest
from tests.support import dispatch_action

from slack_agent_runtime.config import SlackAgentRuntimeSettings
from slack_agent_runtime.domain.policy import InMemoryPolicy, PolicyConfig
from slack_agent_runtime.inference.client import InferenceResult, InferenceUsage
from slack_agent_runtime.services.conversation_coordinator import ConversationCoordinator


def test_empty_allowlists_are_unrestricted_but_empty_admins_deny_mutation() -> None:
    policy = InMemoryPolicy()

    assert policy.check_access(workspace_id="T1", channel_id="C1", user_id="U1").allowed
    assert not policy.check_admin("U1").allowed


@pytest.mark.parametrize(
    ("workspace_id", "channel_id", "user_id", "denial"),
    [
        ("T2", "C1", "U1", "workspace"),
        ("T1", "C2", "U1", "channel"),
        ("T1", "C1", "U2", "not allowed"),
        (None, "C1", "U1", "workspace"),
        ("T1", None, "U1", "channel"),
        ("T1", "C1", None, "not allowed"),
    ],
)
def test_nonempty_allowlists_require_every_dimension(
    workspace_id: str | None,
    channel_id: str | None,
    user_id: str | None,
    denial: str,
) -> None:
    policy = InMemoryPolicy(
        PolicyConfig(
            allowed_workspace_ids=frozenset({"T1"}),
            allowed_channel_ids=frozenset({"C1"}),
            allowed_user_ids=frozenset({"U1"}),
        )
    )

    decision = policy.check_access(
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
    )

    assert not decision.allowed
    assert decision.message is not None
    assert denial in decision.message.lower()


def test_combined_allowlists_allow_only_the_matching_tuple() -> None:
    policy = InMemoryPolicy(
        PolicyConfig(
            allowed_workspace_ids=frozenset({"T1"}),
            allowed_channel_ids=frozenset({"C1"}),
            allowed_user_ids=frozenset({"U1"}),
        )
    )

    assert policy.check_access(
        workspace_id="T1",
        channel_id="C1",
        user_id="U1",
    ).allowed


def test_sliding_request_and_token_windows() -> None:
    now = 0.0
    policy = InMemoryPolicy(
        PolicyConfig(
            request_limit=2,
            request_window_seconds=10,
            token_budget=10,
            token_window_seconds=20,
        ),
        clock=lambda: now,
    )

    first, first_permit = policy.acquire_inference("U1")
    assert first.allowed and first_permit is not None
    policy.release_inference(first_permit, total_tokens=10)
    second, second_permit = policy.acquire_inference("U1")
    assert not second.allowed
    assert second_permit is None

    now = 20.1
    third, third_permit = policy.acquire_inference("U1")
    assert third.allowed and third_permit is not None


def test_request_window_expires_old_admissions() -> None:
    now = 0.0
    policy = InMemoryPolicy(
        PolicyConfig(request_limit=1, request_window_seconds=10),
        clock=lambda: now,
    )
    decision, permit = policy.acquire_inference("U1")
    assert decision.allowed and permit is not None
    policy.release_inference(permit)
    assert not policy.acquire_inference("U1")[0].allowed

    now = 10.1
    assert policy.acquire_inference("U1")[0].allowed


def test_concurrency_release_is_idempotent() -> None:
    policy = InMemoryPolicy(PolicyConfig(max_concurrent_requests=1))
    decision, permit = policy.acquire_inference("U1")
    assert decision.allowed and permit is not None
    assert not policy.acquire_inference("U1")[0].allowed

    policy.release_inference(permit, total_tokens=4)
    policy.release_inference(permit, total_tokens=4)
    assert policy.acquire_inference("U1")[0].allowed


def test_concurrency_limit_is_per_user() -> None:
    policy = InMemoryPolicy(PolicyConfig(max_concurrent_requests=1))

    first, permit = policy.acquire_inference("U1")

    assert first.allowed and permit is not None
    assert not policy.acquire_inference("U1")[0].allowed
    other_user, other_permit = policy.acquire_inference("U2")
    assert other_user.allowed and other_permit is not None


def test_typed_policy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_APP_TOKEN": "xapp-test",
        "INFERENCE_ROUTER_API_KEY": "key",
        "INFERENCE_ROUTER_BASE_URL": "https://example.com/",
        "INFERENCE_ROUTER_MODEL": "model",
        "SLACK_AGENT_RUNTIME_ALLOWED_WORKSPACE_IDS": "T1, T2",
        "SLACK_AGENT_RUNTIME_ADMIN_USER_IDS": "U1",
        "SLACK_AGENT_RUNTIME_REQUEST_LIMIT": "5",
        "SLACK_AGENT_RUNTIME_REQUEST_WINDOW_SECONDS": "30.5",
        "SLACK_AGENT_RUNTIME_TOKEN_BUDGET": "1000",
        "SLACK_AGENT_RUNTIME_TOKEN_WINDOW_SECONDS": "120",
        "SLACK_AGENT_RUNTIME_MAX_CONCURRENT_REQUESTS": "2",
        "SLACK_AGENT_RUNTIME_STREAM_UPDATE_INTERVAL_SECONDS": "1.5",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    settings = SlackAgentRuntimeSettings.from_env()

    assert settings.allowed_workspace_ids == frozenset({"T1", "T2"})
    assert settings.admin_user_ids == frozenset({"U1"})
    assert settings.request_limit == 5
    assert settings.request_window_seconds == 30.5
    assert settings.token_budget == 1000
    assert settings.token_window_seconds == 120
    assert settings.max_concurrent_requests == 2
    assert settings.stream_update_interval_seconds == 1.5


@pytest.mark.asyncio
async def test_entrypoint_allowlist_denies_before_inference() -> None:
    inference_client = AsyncMock()
    service = ConversationCoordinator(
        inference_client,
        default_model="model",
        policy=InMemoryPolicy(PolicyConfig(allowed_workspace_ids=frozenset({"T1"}))),
    )
    say = AsyncMock()

    await service.handle(
        {"text": "hello", "ts": "1", "channel": "C1", "user": "U1"},
        say,
        workspace_id="T2",
    )

    inference_client.complete.assert_not_awaited()
    assert "workspace" in say.await_args.kwargs["text"].lower()


@pytest.mark.asyncio
async def test_normal_user_can_chat_but_cannot_mutate_settings() -> None:
    inference_client = AsyncMock()
    inference_client.complete.return_value = InferenceResult(
        content="answer",
        model="model",
        request_id=None,
        duration_ms=1,
        usage=InferenceUsage(total_tokens=3),
    )
    service = ConversationCoordinator(
        inference_client,
        default_model="model",
        policy=InMemoryPolicy(PolicyConfig(admin_user_ids=frozenset({"ADMIN"}))),
    )
    denied = AsyncMock()

    await service.handle(
        {"text": "temperature 0.5", "ts": "1", "channel": "C1", "user": "U1"},
        denied,
        workspace_id="T1",
    )
    await service.handle(
        {"text": "hello", "ts": "2", "channel": "C1", "user": "U1"},
        AsyncMock(),
        workspace_id="T1",
    )

    assert "administrator" in denied.await_args.kwargs["text"].lower()
    inference_client.complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_normal_user_cannot_use_mutable_block_action() -> None:
    service = ConversationCoordinator(
        AsyncMock(),
        default_model="model",
        policy=InMemoryPolicy(PolicyConfig(admin_user_ids=frozenset({"ADMIN"}))),
    )
    say = AsyncMock()
    body = {
        "team": {"id": "T1"},
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "message": {"ts": "2", "thread_ts": "1"},
        "container": {"thread_ts": "1"},
    }

    await dispatch_action(service, "assistant.web_toggle", body, say, AsyncMock())

    assert "administrator" in say.await_args.kwargs["text"].lower()


@pytest.mark.asyncio
async def test_only_final_reported_usage_charges_budget_and_error_releases() -> None:
    inference_client = AsyncMock()
    inference_client.complete.side_effect = (
        RuntimeError("failed"),
        InferenceResult(
            content="answer",
            model="model",
            request_id=None,
            duration_ms=1,
            usage=InferenceUsage(total_tokens=5),
        ),
    )
    policy = InMemoryPolicy(PolicyConfig(token_budget=5, max_concurrent_requests=1))
    service = ConversationCoordinator(inference_client, default_model="model", policy=policy)

    await service.handle(
        {"text": "first", "ts": "1", "channel": "C1", "user": "U1"},
        AsyncMock(),
    )
    await service.handle(
        {"text": "second", "ts": "2", "channel": "C2", "user": "U1"},
        AsyncMock(),
    )
    denied = AsyncMock()
    await service.handle(
        {"text": "third", "ts": "3", "channel": "C3", "user": "U1"},
        denied,
    )

    assert inference_client.complete.await_count == 2
    assert "token budget" in denied.await_args.kwargs["text"].lower()


@pytest.mark.asyncio
async def test_cancelled_request_releases_concurrency_permit() -> None:
    started = asyncio.Event()

    async def block_until_cancelled(**_: object) -> InferenceResult:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    inference_client = AsyncMock()
    inference_client.complete.side_effect = block_until_cancelled
    completed = InferenceResult(
        content="answer",
        model="model",
        request_id=None,
        duration_ms=1,
        usage=InferenceUsage(),
    )
    policy = InMemoryPolicy(PolicyConfig(max_concurrent_requests=1))
    service = ConversationCoordinator(inference_client, default_model="model", policy=policy)
    first = asyncio.create_task(
        service.handle(
            {"text": "slow", "ts": "1", "channel": "C1", "user": "U1"},
            AsyncMock(),
        )
    )
    await started.wait()

    denied = AsyncMock()
    await service.handle(
        {"text": "blocked", "ts": "2", "channel": "C2", "user": "U1"},
        denied,
    )
    first.cancel()
    await first
    inference_client.complete.side_effect = None
    inference_client.complete.return_value = completed
    accepted = AsyncMock()
    await service.handle(
        {"text": "retry", "ts": "3", "channel": "C3", "user": "U1"},
        accepted,
    )

    assert "maximum" in denied.await_args.kwargs["text"].lower()
    assert inference_client.complete.await_count == 2
    assert accepted.await_args.kwargs["text"] == "answer"
