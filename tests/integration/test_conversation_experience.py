import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call, patch

import pytest
from tests.support import dispatch_action, dispatch_event, dispatch_view

from slack_agent_runtime.domain.commands import HelpEntry
from slack_agent_runtime.domain.models import ThreadStateStore
from slack_agent_runtime.domain.policy import InMemoryPolicy, PolicyConfig
from slack_agent_runtime.inference.client import InferenceResult
from slack_agent_runtime.plugins.api import PluginDefinition
from slack_agent_runtime.plugins.registry import PluginRegistry
from slack_agent_runtime.services.conversation_coordinator import ConversationCoordinator
from slack_agent_runtime.slack.ui import (
    ACTION_CLEAR,
    ACTION_FEEDBACK_POSITIVE,
    ACTION_MODEL_OPEN,
    ACTION_REGENERATE,
    ACTION_STREAM_TOGGLE,
    ACTION_WEB_TOGGLE,
    VIEW_MODEL_SELECT,
)

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "slack"


def _fixture(name: str) -> dict[str, object]:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_followup_sends_bounded_thread_history(inference_result_factory) -> None:
    inference_client = AsyncMock()
    inference_client.complete.side_effect = (
        inference_result_factory("First answer"),
        inference_result_factory("Second answer"),
    )
    service = ConversationCoordinator(inference_client, default_model="kimi-k3")

    await service.handle(
        {"text": "first question", "ts": "100", "channel": "C1"},
        AsyncMock(),
    )
    await service.handle(
        {
            "text": "follow up",
            "ts": "101",
            "thread_ts": "100",
            "channel": "C1",
        },
        AsyncMock(),
    )

    assert inference_client.complete.await_args_list[1].kwargs["messages"] == [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "First answer"},
        {"role": "user", "content": "follow up"},
    ]


@pytest.mark.asyncio
async def test_cache_miss_restores_recent_slack_thread_history(
    inference_result_factory,
) -> None:
    inference_client = AsyncMock()
    inference_client.complete.return_value = inference_result_factory("New answer")
    states = ThreadStateStore()
    service = ConversationCoordinator(
        inference_client,
        default_model="kimi-k3",
        state_store=states,
    )
    client = AsyncMock()
    client.auth_test.return_value = {"user_id": "UBOT", "bot_id": "BBOT"}
    client.conversations_replies.return_value = {
        "messages": [
            {
                "text": "<@UBOT> first question",
                "ts": "100",
                "user": "U1",
            },
            {
                "text": "First",
                "ts": "100.1",
                "user": "UBOT",
                "bot_id": "BBOT",
            },
            {
                "text": "answer\n\n_Usage:_ Model: `kimi-k3`",
                "ts": "100.2",
                "user": "UBOT",
                "bot_id": "BBOT",
            },
            {
                "text": "<@UBOT> settings",
                "ts": "100.3",
                "user": "U1",
            },
            {
                "text": "Model: `kimi-k3`",
                "ts": "100.4",
                "user": "UBOT",
                "bot_id": "BBOT",
            },
            {
                "text": "unrelated thread comment",
                "ts": "100.5",
                "user": "U2",
            },
            {
                "text": "<@UBOT> follow up",
                "ts": "101",
                "user": "U1",
            },
        ]
    }

    await service.handle(
        {
            "text": "<@UBOT> follow up",
            "ts": "101",
            "thread_ts": "100",
            "channel": "C1",
            "channel_type": "channel",
        },
        AsyncMock(return_value={"ts": "progress"}),
        client=client,
    )

    client.conversations_replies.assert_awaited_once_with(
        channel="C1",
        ts="100",
        limit=100,
    )
    assert inference_client.complete.await_args.kwargs["messages"] == [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "First\n\nanswer"},
        {"role": "user", "content": "follow up"},
    ]
    state = states.get(("C1", "100"))
    assert state.history_hydrated is True
    assert state.response_timestamp_groups[0] == ["100.1", "100.2"]


@pytest.mark.asyncio
async def test_slack_history_clear_command_starts_a_new_context_boundary(
    inference_result_factory,
) -> None:
    inference_client = AsyncMock()
    inference_client.complete.return_value = inference_result_factory("New answer")
    service = ConversationCoordinator(inference_client, default_model="kimi-k3")
    client = AsyncMock()
    client.auth_test.return_value = {"user_id": "UBOT", "bot_id": "BBOT"}
    client.conversations_replies.return_value = {
        "messages": [
            {"text": "old question", "ts": "100", "user": "U1"},
            {
                "text": "old answer",
                "ts": "100.1",
                "user": "UBOT",
                "bot_id": "BBOT",
            },
            {"text": "clear", "ts": "100.2", "user": "U1"},
            {
                "text": "Thread history and settings cleared.",
                "ts": "100.3",
                "user": "UBOT",
                "bot_id": "BBOT",
            },
            {"text": "new question", "ts": "100.4", "user": "U1"},
            {
                "text": "new answer",
                "ts": "100.5",
                "user": "UBOT",
                "bot_id": "BBOT",
            },
        ]
    }

    await service.handle(
        {
            "text": "follow up",
            "ts": "101",
            "thread_ts": "100",
            "channel": "D1",
            "channel_type": "im",
        },
        AsyncMock(return_value={"ts": "progress"}),
        client=client,
    )

    assert inference_client.complete.await_args.kwargs["messages"] == [
        {"role": "user", "content": "new question"},
        {"role": "assistant", "content": "new answer"},
        {"role": "user", "content": "follow up"},
    ]


@pytest.mark.asyncio
async def test_slack_history_failure_falls_back_to_the_current_prompt(
    inference_result_factory,
) -> None:
    inference_client = AsyncMock()
    inference_client.complete.return_value = inference_result_factory("Answer")
    service = ConversationCoordinator(inference_client, default_model="kimi-k3")
    client = AsyncMock()
    client.auth_test.return_value = {"user_id": "UBOT", "bot_id": "BBOT"}
    client.conversations_replies.side_effect = RuntimeError("Slack unavailable")

    await service.handle(
        {
            "text": "follow up",
            "ts": "101",
            "thread_ts": "100",
            "channel": "D1",
            "channel_type": "im",
        },
        AsyncMock(return_value={"ts": "progress"}),
        client=client,
    )

    assert inference_client.complete.await_args.kwargs["messages"] == [
        {"role": "user", "content": "follow up"}
    ]


@pytest.mark.asyncio
async def test_progress_citations_and_long_response_delivery(inference_result_factory) -> None:
    inference_client = AsyncMock()
    inference_client.complete.return_value = inference_result_factory(
        "[Source](https://example.com) " + ("word " * 1_500)
    )
    states = ThreadStateStore()
    states.get(("C1", "100")).web_search = True
    service = ConversationCoordinator(
        inference_client,
        default_model="kimi-k3",
        state_store=states,
    )
    say = AsyncMock(
        side_effect=(
            {"ts": "bot-first"},
            {"ts": "bot-second"},
            {"ts": "bot-third"},
        )
    )
    client = AsyncMock()

    await service.handle(
        {"text": "latest news", "ts": "100", "channel": "C1"},
        say,
        client=client,
    )

    assert say.await_args_list[0].kwargs["text"] == "Searching…"
    assert say.await_args_list[0].kwargs["thread_ts"] == "100"
    assert say.await_args_list[0].kwargs["blocks"]
    first_chunk = client.chat_update.await_args.kwargs["text"]
    assert "<https://example.com|Source>" in first_chunk
    assert len(first_chunk) <= 3_500
    assert say.await_count >= 2
    assert all(len(call.kwargs["text"]) <= 3_500 for call in say.await_args_list[1:])


@pytest.mark.asyncio
async def test_usage_details_are_optional(inference_result_factory) -> None:
    inference_client = AsyncMock()
    inference_client.complete.return_value = inference_result_factory("Answer")
    service = ConversationCoordinator(inference_client, default_model="kimi-k3")
    await service.handle(
        {"text": "usage on", "ts": "100", "channel": "C1"},
        AsyncMock(),
    )
    say = AsyncMock(return_value={"ts": "bot-1"})
    client = AsyncMock()

    await service.handle(
        {
            "text": "question",
            "ts": "101",
            "thread_ts": "100",
            "channel": "C1",
        },
        say,
        client=client,
    )

    text = client.chat_update.await_args.kwargs["text"]
    assert "Model: `kimi-k3`" in text
    assert "Latency: `250 ms`" in text
    assert "Tokens: `20 (12 input, 8 output)`" in text
    assert "Request ID: `req-123`" in text


@pytest.mark.asyncio
async def test_help_lists_experience_commands(coordinator_factory) -> None:
    service = coordinator_factory()
    say = AsyncMock()

    await service.handle(
        {"text": "help", "ts": "100", "channel": "C1"},
        say,
    )

    assert say.await_args is not None
    text = say.await_args.kwargs["text"]
    assert "`regenerate`" in text
    assert "`cancel`" in text
    assert "`delete last`" in text
    assert "`clear`" in text
    assert "`settings default`" in text
    assert "`usage [on|off]`" in text
    assert "*Models & search*" in text
    assert "*Generation*" in text
    assert "*Thread actions*" in text
    assert "*Settings & info*" in text
    assert "response limit" in text
    assert "reasoning effort" in text
    assert "All settings are per thread" in text


@pytest.mark.asyncio
async def test_help_includes_entries_from_enabled_plugins(coordinator_factory) -> None:
    service = coordinator_factory()
    registry = PluginRegistry(
        (
            *service.plugin_registry.plugins,
            PluginDefinition(
                "tools",
                help_entries=(
                    HelpEntry("MCP tools", "tools"),
                    HelpEntry("MCP tools", "mcp status"),
                ),
            ),
        )
    )
    service.set_plugin_registry(registry)
    say = AsyncMock()

    await service.handle(
        {"text": "help", "ts": "100", "channel": "C1"},
        say,
    )

    text = say.await_args.kwargs["text"]
    assert "*MCP tools*" in text
    assert "`tools`" in text
    assert "`mcp status`" in text


@pytest.mark.asyncio
async def test_settings_default_preserves_thread_history() -> None:
    states = ThreadStateStore()
    state = states.get(("C1", "100"))
    state.model = "other-model"
    state.web_search = True
    state.usage_enabled = True
    state.params["temperature"] = 0.5
    state.record_exchange("question", "answer")
    service = ConversationCoordinator(
        AsyncMock(),
        default_model="kimi-k3",
        state_store=states,
    )
    say = AsyncMock()

    await service.handle(
        {
            "text": "settings default",
            "ts": "101",
            "thread_ts": "100",
            "channel": "C1",
        },
        say,
    )

    assert state.messages == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    assert state.model is None
    assert state.web_search is False
    assert state.usage_enabled is False
    assert state.params == {}
    say.assert_awaited_once_with(
        text="Thread settings reset to defaults.",
        thread_ts="100",
    )


@pytest.mark.asyncio
async def test_delete_last_removes_latest_bot_response_but_keeps_history(
    inference_result_factory,
) -> None:
    inference_client = AsyncMock()
    inference_client.complete.return_value = inference_result_factory("Answer")
    states = ThreadStateStore()
    service = ConversationCoordinator(
        inference_client,
        default_model="kimi-k3",
        state_store=states,
    )
    client = AsyncMock()

    await service.handle(
        {"text": "question", "ts": "100", "channel": "C1"},
        AsyncMock(return_value={"ts": "bot-1"}),
        client=client,
    )
    delete_say = AsyncMock()
    await service.handle(
        {
            "text": "delete last",
            "ts": "101",
            "thread_ts": "100",
            "channel": "C1",
        },
        delete_say,
        client=client,
    )

    client.chat_delete.assert_awaited_once_with(channel="C1", ts="bot-1")
    delete_say.assert_awaited_once_with(
        text="Latest bot response deleted.",
        thread_ts="100",
    )
    assert states.get(("C1", "100")).messages == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "Answer"},
    ]
    assert states.get(("C1", "100")).response_timestamp_groups == []


@pytest.mark.asyncio
async def test_delete_last_can_repeatedly_remove_earlier_bot_responses(
    inference_result_factory,
) -> None:
    inference_client = AsyncMock()
    inference_client.complete.side_effect = (
        inference_result_factory("First answer"),
        inference_result_factory("Second answer"),
    )
    states = ThreadStateStore()
    service = ConversationCoordinator(
        inference_client,
        default_model="kimi-k3",
        state_store=states,
    )
    client = AsyncMock()

    await service.handle(
        {"text": "first", "ts": "100", "channel": "C1"},
        AsyncMock(return_value={"ts": "bot-1"}),
        client=client,
    )
    await service.handle(
        {
            "text": "second",
            "ts": "101",
            "thread_ts": "100",
            "channel": "C1",
        },
        AsyncMock(return_value={"ts": "bot-2"}),
        client=client,
    )
    for command_ts in ("102", "103"):
        await service.handle(
            {
                "text": "delete last",
                "ts": command_ts,
                "thread_ts": "100",
                "channel": "C1",
            },
            AsyncMock(),
            client=client,
        )

    assert client.chat_delete.await_args_list == [
        call(channel="C1", ts="bot-2"),
        call(channel="C1", ts="bot-1"),
    ]
    assert states.get(("C1", "100")).response_timestamp_groups == []


@pytest.mark.asyncio
async def test_delete_last_reports_when_no_bot_response_is_cached() -> None:
    service = ConversationCoordinator(AsyncMock(), default_model="kimi-k3")
    say = AsyncMock()

    await service.handle(
        {"text": "delete last", "ts": "100", "channel": "C1"},
        say,
        client=AsyncMock(),
    )

    say.assert_awaited_once_with(
        text="There is no bot response to delete in this thread.",
        thread_ts="100",
    )


@pytest.mark.asyncio
async def test_clear_purges_only_the_current_thread_state() -> None:
    inference_client = AsyncMock()
    states = ThreadStateStore()
    state = states.get(("C1", "100"))
    state.model = "other-model"
    state.web_search = True
    state.usage_enabled = True
    state.params["temperature"] = 0.5
    state.record_exchange("question", "answer")
    other_state = states.get(("C2", "200"))
    other_state.record_exchange("keep", "this")
    service = ConversationCoordinator(
        inference_client,
        default_model="kimi-k3",
        state_store=states,
    )
    say = AsyncMock()

    await service.handle(
        {
            "text": "clear",
            "ts": "101",
            "thread_ts": "100",
            "channel": "C1",
        },
        say,
    )

    cleared = states.get(("C1", "100"))
    assert cleared is state
    assert cleared.messages == []
    assert cleared.model is None
    assert cleared.web_search is False
    assert cleared.usage_enabled is False
    assert cleared.params == {}
    assert cleared.history_hydrated is True
    assert states.get(("C2", "200")) is other_state
    inference_client.complete.assert_not_awaited()
    say.assert_awaited_once_with(
        text="Thread history and settings cleared.",
        thread_ts="100",
    )


@pytest.mark.asyncio
async def test_regenerate_replaces_latest_answer_in_history(inference_result_factory) -> None:
    inference_client = AsyncMock()
    inference_client.complete.side_effect = (
        inference_result_factory("First answer"),
        inference_result_factory("Regenerated answer", request_id="req-456"),
    )
    states = ThreadStateStore()
    service = ConversationCoordinator(
        inference_client,
        default_model="kimi-k3",
        state_store=states,
    )

    await service.handle(
        {"text": "question", "ts": "100", "channel": "C1"},
        AsyncMock(),
    )
    await service.handle(
        {
            "text": "regenerate",
            "ts": "101",
            "thread_ts": "100",
            "channel": "C1",
        },
        AsyncMock(),
    )

    assert inference_client.complete.await_args_list[1].kwargs["messages"] == [
        {"role": "user", "content": "question"}
    ]
    assert states.get(("C1", "100")).messages == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "Regenerated answer"},
    ]


@pytest.mark.asyncio
async def test_cancel_stops_active_request_and_updates_placeholder() -> None:
    started = asyncio.Event()

    async def wait_for_cancel(**_: object) -> InferenceResult:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("request should have been cancelled")

    inference_client = AsyncMock()
    inference_client.complete.side_effect = wait_for_cancel
    service = ConversationCoordinator(inference_client, default_model="kimi-k3")
    request_say = AsyncMock(return_value={"ts": "bot-1"})
    client = AsyncMock()
    request = asyncio.create_task(
        service.handle(
            {"text": "slow question", "ts": "100", "channel": "C1"},
            request_say,
            client=client,
        )
    )
    await started.wait()

    cancel_say = AsyncMock()
    await service.handle(
        {
            "text": "cancel",
            "ts": "101",
            "thread_ts": "100",
            "channel": "C1",
        },
        cancel_say,
    )
    await request

    cancel_say.assert_awaited_once_with(
        text="Cancellation requested.",
        thread_ts="100",
    )
    client.chat_update.assert_awaited_once_with(
        channel="C1",
        ts="bot-1",
        text="Request cancelled.",
        blocks=[],
    )


@pytest.mark.asyncio
async def test_feedback_reaction_logs_and_emits_metric(
    caplog: pytest.LogCaptureFixture,
    inference_result_factory,
) -> None:
    inference_client = AsyncMock()
    inference_client.complete.return_value = inference_result_factory("Answer")
    service = ConversationCoordinator(inference_client, default_model="kimi-k3")
    say = AsyncMock(return_value={"ts": "bot-1"})
    client = AsyncMock()
    await service.handle(
        {"text": "question", "ts": "100", "channel": "C1"},
        say,
        client=client,
    )

    metric = Mock()
    with (
        patch(
            "slack_agent_runtime.plugins.feedback_index._FEEDBACK_COUNTER.add",
            metric,
        ),
        caplog.at_level(logging.INFO, logger="slack_agent_runtime.plugins.feedback_index"),
    ):
        await dispatch_event(
            service,
            "reaction_added",
            {
                "reaction": "+1",
                "user": "U1",
                "item": {"type": "message", "channel": "C1", "ts": "bot-1"},
            },
            AsyncMock(),
            AsyncMock(),
        )

    metric.assert_called_once()
    record = next(
        record for record in caplog.records if record.getMessage() == "Slack Agent Runtime feedback"
    )
    feedback = record.__dict__["slack_bot_feedback"]
    assert feedback["request_id"] == "req-123"
    assert feedback["model"] == "kimi-k3"


@pytest.mark.asyncio
async def test_web_button_toggles_thread_setting_from_fixture() -> None:
    states = ThreadStateStore()
    service = ConversationCoordinator(
        AsyncMock(),
        default_model="default-model",
        state_store=states,
    )
    body = _fixture("block_action.json")
    say = AsyncMock()

    await dispatch_action(
        service,
        ACTION_WEB_TOGGLE,
        body,
        say,
        AsyncMock(),
    )

    assert states.get(("C123", "100.000")).web_search is True
    say.assert_awaited_once_with(
        text="Web search enabled for this thread.",
        thread_ts="100.000",
    )


@pytest.mark.asyncio
async def test_stream_button_toggles_thread_setting_from_fixture() -> None:
    states = ThreadStateStore()
    service = ConversationCoordinator(
        AsyncMock(),
        default_model="default-model",
        state_store=states,
    )
    say = AsyncMock()

    await dispatch_action(
        service,
        ACTION_STREAM_TOGGLE,
        _fixture("block_action.json"),
        say,
        AsyncMock(),
    )

    assert states.get(("C123", "100.000")).streaming_enabled is False
    say.assert_awaited_once_with(
        text="Streaming disabled for this thread.",
        thread_ts="100.000",
    )


@pytest.mark.asyncio
async def test_model_button_opens_model_picker() -> None:
    inference_client = AsyncMock()
    inference_client.list_models.return_value = ("default-model", "example-model")
    service = ConversationCoordinator(inference_client, default_model="default-model")
    client = AsyncMock()

    await dispatch_action(
        service,
        ACTION_MODEL_OPEN,
        _fixture("block_action.json"),
        AsyncMock(),
        client,
    )

    view = client.views_open.await_args.kwargs["view"]
    assert client.views_open.await_args.kwargs["trigger_id"] == "trigger-123"
    assert view["callback_id"] == "assistant.model_select_view"
    assert "example-model" in str(view)


@pytest.mark.asyncio
async def test_non_admin_model_button_is_denied_before_loading_models() -> None:
    inference_client = AsyncMock()
    service = ConversationCoordinator(
        inference_client,
        default_model="default-model",
        policy=InMemoryPolicy(PolicyConfig(admin_user_ids=frozenset({"ADMIN"}))),
    )
    say = AsyncMock()
    client = AsyncMock()

    await dispatch_action(
        service,
        ACTION_MODEL_OPEN,
        _fixture("block_action.json"),
        say,
        client,
    )

    assert "administrator" in say.await_args.kwargs["text"].lower()
    inference_client.list_models.assert_not_awaited()
    client.views_open.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_submission_updates_only_the_selected_thread() -> None:
    inference_client = AsyncMock()
    inference_client.list_models.return_value = ("default-model", "example-model")
    states = ThreadStateStore()
    service = ConversationCoordinator(
        inference_client,
        default_model="default-model",
        state_store=states,
    )
    body = _fixture("model_submission.json")
    client = AsyncMock()

    await dispatch_view(service, VIEW_MODEL_SELECT, body, client)

    assert states.get(("C123", "100.000")).model == "example-model"
    client.chat_postMessage.assert_awaited_once_with(
        channel="C123",
        thread_ts="100.000",
        text="Model for this thread set to `example-model`.",
    )


@pytest.mark.asyncio
async def test_non_admin_model_submission_is_denied_before_validation() -> None:
    inference_client = AsyncMock()
    states = ThreadStateStore()
    service = ConversationCoordinator(
        inference_client,
        default_model="default-model",
        state_store=states,
        policy=InMemoryPolicy(PolicyConfig(admin_user_ids=frozenset({"ADMIN"}))),
    )
    body = _fixture("model_submission.json")
    client = AsyncMock()

    await dispatch_view(service, VIEW_MODEL_SELECT, body, client)

    inference_client.list_models.assert_not_awaited()
    assert states.get(("C123", "100.000")).model is None
    assert "administrator" in client.chat_postMessage.await_args.kwargs["text"].lower()


@pytest.mark.asyncio
async def test_feedback_button_records_feedback_and_confirms_ephemerally(
    inference_result_factory,
) -> None:
    inference_client = AsyncMock()
    inference_client.complete.return_value = inference_result_factory(
        "Answer", model="default-model"
    )
    service = ConversationCoordinator(inference_client, default_model="default-model")
    await service.handle(
        {"text": "question", "ts": "100.000", "channel": "C123"},
        AsyncMock(return_value={"ts": "101.000"}),
    )
    body = _fixture("block_action.json")
    client = AsyncMock()

    with patch("slack_agent_runtime.plugins.feedback_index._FEEDBACK_COUNTER.add") as metric:
        await dispatch_action(
            service,
            ACTION_FEEDBACK_POSITIVE,
            body,
            AsyncMock(),
            client,
        )

    metric.assert_called_once()
    client.chat_postEphemeral.assert_awaited_once_with(
        channel="C123",
        user="U123",
        text="Thanks for the feedback.",
        thread_ts="100.000",
    )


@pytest.mark.asyncio
async def test_regenerate_and_clear_buttons_reuse_command_behavior(
    inference_result_factory,
) -> None:
    inference_client = AsyncMock()
    inference_client.complete.return_value = inference_result_factory(
        "Regenerated",
        model="default-model",
    )
    states = ThreadStateStore()
    state = states.get(("C123", "100.000"))
    state.record_exchange("question", "answer")
    service = ConversationCoordinator(
        inference_client,
        default_model="default-model",
        state_store=states,
    )
    body = _fixture("block_action.json")
    say = AsyncMock(side_effect=({"ts": "progress"}, {"ts": "answer"}))

    await dispatch_action(
        service,
        ACTION_REGENERATE,
        body,
        say,
        AsyncMock(),
    )
    assert state.messages[-1]["content"] == "Regenerated"

    await dispatch_action(
        service,
        ACTION_CLEAR,
        body,
        say,
        AsyncMock(),
    )
    assert state.messages == []
