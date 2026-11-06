from unittest.mock import AsyncMock

import pytest

from slack_agent_runtime.inference.client import InferenceRouterError
from slack_agent_runtime.services.conversation_coordinator import ConversationCoordinator


@pytest.mark.asyncio
async def test_mention_calls_router_and_replies_in_thread(inference_result_factory) -> None:
    inference_client = AsyncMock()
    inference_client.complete.return_value = inference_result_factory("The answer")
    say = AsyncMock()
    service = ConversationCoordinator(
        inference_client,
        default_model="kimi-k3",
    )

    await service.handle(
        {
            "type": "app_mention",
            "text": "<@U123> what is Kimi?",
            "ts": "123.456",
            "channel": "C123",
            "user": "U456",
        },
        say,
    )

    inference_client.complete.assert_awaited_once_with(
        messages=[{"role": "user", "content": "what is Kimi?"}],
        model="kimi-k3",
        web_search=False,
    )
    assert say.await_args.kwargs["text"] == "The answer"
    assert say.await_args.kwargs["thread_ts"] == "123.456"
    assert say.await_args.kwargs["blocks"]


@pytest.mark.asyncio
async def test_followup_reuses_existing_thread(inference_result_factory) -> None:
    inference_client = AsyncMock()
    inference_client.complete.return_value = inference_result_factory("Follow-up answer")
    say = AsyncMock()
    service = ConversationCoordinator(
        inference_client,
        default_model="kimi-k3",
    )

    await service.handle(
        {
            "type": "message",
            "text": "follow up",
            "ts": "124.000",
            "thread_ts": "123.456",
            "channel": "C123",
        },
        say,
    )

    inference_client.complete.assert_awaited_once_with(
        messages=[{"role": "user", "content": "follow up"}],
        model="kimi-k3",
        web_search=False,
    )
    assert say.await_args.kwargs["text"] == "Follow-up answer"
    assert say.await_args.kwargs["thread_ts"] == "123.456"
    assert say.await_args.kwargs["blocks"]


@pytest.mark.asyncio
async def test_bot_messages_are_ignored() -> None:
    inference_client = AsyncMock()
    say = AsyncMock()
    service = ConversationCoordinator(
        inference_client,
        default_model="kimi-k3",
    )

    await service.handle(
        {"text": "loop", "bot_id": "B123", "ts": "123.456"},
        say,
    )

    inference_client.complete.assert_not_awaited()
    say.assert_not_awaited()


@pytest.mark.asyncio
async def test_router_failure_returns_safe_message() -> None:
    inference_client = AsyncMock()
    inference_client.complete.side_effect = InferenceRouterError("HTTP 503")
    say = AsyncMock()
    service = ConversationCoordinator(
        inference_client,
        default_model="kimi-k3",
    )

    await service.handle(
        {"text": "hello", "ts": "123.456", "channel": "D123", "user": "U123"},
        say,
    )

    say.assert_awaited_once_with(
        text="I couldn't complete that request. Please try again shortly.",
        thread_ts="123.456",
    )


@pytest.mark.asyncio
async def test_model_command_switches_model_for_thread(inference_result_factory) -> None:
    inference_client = AsyncMock()
    inference_client.complete.return_value = inference_result_factory("GLM answer", model="glm-5.3")
    inference_client.list_models.return_value = ("glm-5.3", "kimi-k3")
    say = AsyncMock()
    service = ConversationCoordinator(
        inference_client,
        default_model="kimi-k3",
    )

    await service.handle(
        {
            "text": "model glm-5.3",
            "ts": "123.456",
            "channel": "C123",
        },
        say,
    )
    await service.handle(
        {
            "text": "answer this",
            "ts": "124.000",
            "thread_ts": "123.456",
            "channel": "C123",
        },
        say,
    )

    inference_client.complete.assert_awaited_once_with(
        messages=[{"role": "user", "content": "answer this"}],
        model="glm-5.3",
        web_search=False,
    )
    assert say.await_args_list[0].kwargs == {
        "text": "Model for this thread set to `glm-5.3`.",
        "thread_ts": "123.456",
    }


@pytest.mark.asyncio
async def test_model_switch_does_not_affect_other_threads(inference_result_factory) -> None:
    inference_client = AsyncMock()
    inference_client.complete.return_value = inference_result_factory("Answer")
    inference_client.list_models.return_value = ("glm-5.3", "kimi-k3")
    service = ConversationCoordinator(
        inference_client,
        default_model="kimi-k3",
    )

    await service.handle(
        {"text": "model glm-5.3", "ts": "123.456", "channel": "C123"},
        AsyncMock(),
    )
    await service.handle(
        {"text": "answer this", "ts": "200.000", "channel": "C123"},
        AsyncMock(),
    )

    inference_client.complete.assert_awaited_once_with(
        messages=[{"role": "user", "content": "answer this"}],
        model="kimi-k3",
        web_search=False,
    )


@pytest.mark.asyncio
async def test_unknown_model_is_rejected() -> None:
    inference_client = AsyncMock()
    inference_client.list_models.return_value = ("glm-5.3", "kimi-k3")
    say = AsyncMock()
    service = ConversationCoordinator(
        inference_client,
        default_model="kimi-k3",
    )

    await service.handle(
        {"text": "model unknown", "ts": "123.456", "channel": "C123"},
        say,
    )

    inference_client.complete.assert_not_awaited()
    say.assert_awaited_once_with(
        text=("Unknown model `unknown`.\nAvailable models: `glm-5.3`, `kimi-k3`"),
        thread_ts="123.456",
    )


@pytest.mark.asyncio
async def test_model_command_lists_models_from_inference_router() -> None:
    inference_client = AsyncMock()
    inference_client.list_models.return_value = ("glm-5.3", "new-model")
    say = AsyncMock()
    service = ConversationCoordinator(
        inference_client,
        default_model="kimi-k3",
    )

    await service.handle(
        {"text": "model", "ts": "123.456", "channel": "C123"},
        say,
    )

    inference_client.list_models.assert_awaited_once_with()
    say.assert_awaited_once_with(
        text=("Current model: `kimi-k3`\nAvailable models: `glm-5.3`, `new-model`"),
        thread_ts="123.456",
    )


@pytest.mark.asyncio
async def test_web_command_enables_search_for_thread(inference_result_factory) -> None:
    inference_client = AsyncMock()
    inference_client.complete.return_value = inference_result_factory("Grounded answer")
    say = AsyncMock()
    service = ConversationCoordinator(
        inference_client,
        default_model="kimi-k3",
    )

    await service.handle(
        {"text": "web on", "ts": "123.456", "channel": "C123"},
        say,
    )
    await service.handle(
        {
            "text": "latest AI news",
            "ts": "124.000",
            "thread_ts": "123.456",
            "channel": "C123",
        },
        say,
    )

    inference_client.complete.assert_awaited_once_with(
        messages=[{"role": "user", "content": "latest AI news"}],
        model="kimi-k3",
        web_search=True,
    )
    assert say.await_args_list[0].kwargs == {
        "text": "Web search enabled for this thread.",
        "thread_ts": "123.456",
    }


@pytest.mark.asyncio
async def test_web_search_is_scoped_to_thread(inference_result_factory) -> None:
    inference_client = AsyncMock()
    inference_client.complete.return_value = inference_result_factory("Answer")
    service = ConversationCoordinator(
        inference_client,
        default_model="kimi-k3",
    )

    await service.handle(
        {"text": "web on", "ts": "123.456", "channel": "C123"},
        AsyncMock(),
    )
    await service.handle(
        {"text": "another thread", "ts": "200.000", "channel": "C123"},
        AsyncMock(),
    )

    inference_client.complete.assert_awaited_once_with(
        messages=[{"role": "user", "content": "another thread"}],
        model="kimi-k3",
        web_search=False,
    )


@pytest.mark.asyncio
async def test_generation_parameters_are_scoped_to_thread(inference_result_factory) -> None:
    inference_client = AsyncMock()
    inference_client.complete.return_value = inference_result_factory("Tuned answer")
    service = ConversationCoordinator(
        inference_client,
        default_model="kimi-k3",
    )
    command_say = AsyncMock()

    for command in (
        "temperature 0.7",
        "top-p 0.9",
        "max-tokens 2048",
        "reasoning high",
    ):
        await service.handle(
            {"text": command, "ts": "123.456", "channel": "C123"},
            command_say,
        )
    await service.handle(
        {
            "text": "answer carefully",
            "ts": "124.000",
            "thread_ts": "123.456",
            "channel": "C123",
        },
        AsyncMock(),
    )

    inference_client.complete.assert_awaited_once_with(
        messages=[{"role": "user", "content": "answer carefully"}],
        model="kimi-k3",
        web_search=False,
        temperature=0.7,
        top_p=0.9,
        max_tokens=2048,
        reasoning_effort="high",
    )


@pytest.mark.asyncio
async def test_invalid_generation_parameter_is_rejected() -> None:
    inference_client = AsyncMock()
    say = AsyncMock()
    service = ConversationCoordinator(
        inference_client,
        default_model="kimi-k3",
    )

    await service.handle(
        {"text": "temperature 3", "ts": "123.456", "channel": "C123"},
        say,
    )

    inference_client.complete.assert_not_awaited()
    say.assert_awaited_once_with(
        text="`temperature` must be a number from 0 to 2.",
        thread_ts="123.456",
    )
