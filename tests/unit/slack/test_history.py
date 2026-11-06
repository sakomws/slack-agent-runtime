from unittest.mock import AsyncMock

import pytest

from slack_agent_runtime.domain.models import ThreadState
from slack_agent_runtime.plugins.feedback_index import FeedbackIndex
from slack_agent_runtime.slack.history import HistoryManager, rebuild_slack_history


def test_rebuild_slack_history_filters_noise_commands_and_groups_bot_chunks() -> None:
    messages, response_groups = rebuild_slack_history(
        [
            {"text": "<@UBOT> ignored current", "ts": "9", "user": "U1"},
            {"text": "unmentioned", "ts": "1", "user": "U1"},
            {"text": "<@UBOT> first question", "ts": "2", "user": "U1"},
            {"text": "first chunk", "ts": "3", "user": "UBOT"},
            {
                "text": "second chunk\n\n_Usage:_ hidden",
                "ts": "4",
                "bot_id": "BBOT",
            },
            {"text": "<@UBOT> settings", "ts": "5", "user": "U1"},
            {"text": "must not attach", "ts": "6", "user": "UBOT"},
            {"text": "<@UBOT> second question", "ts": "7", "user": "U2"},
            {"text": "second answer", "ts": "8", "bot_id": "BBOT"},
            {"text": "other bot", "ts": "8.1", "bot_id": "BOTHER"},
        ],
        bot_user_id="UBOT",
        bot_id="BBOT",
        current_ts="9",
        require_mention=True,
    )

    assert messages == [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first chunk\n\nsecond chunk"},
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": "second answer"},
    ]
    assert response_groups == [["3", "4"], ["8"]]


def test_rebuild_slack_history_resets_at_clear_boundary_in_direct_messages() -> None:
    messages, response_groups = rebuild_slack_history(
        [
            {"text": "old question", "ts": "1", "user": "U1"},
            {"text": "old answer", "ts": "2", "user": "UBOT"},
            {"text": "reset", "ts": "3", "user": "U1"},
            {"text": "new question", "ts": "4", "user": "U1"},
            {"text": "new answer", "ts": "5", "user": "UBOT"},
        ],
        bot_user_id="UBOT",
        bot_id=None,
        current_ts="6",
        require_mention=False,
    )

    assert messages == [
        {"role": "user", "content": "new question"},
        {"role": "assistant", "content": "new answer"},
    ]
    assert response_groups == [["5"]]


@pytest.mark.asyncio
async def test_hydrate_fetches_once_and_caches_bot_identity() -> None:
    manager = HistoryManager(FeedbackIndex())
    client = AsyncMock()
    client.auth_test.return_value = {"user_id": "UBOT", "bot_id": "BBOT"}
    client.conversations_replies.return_value = {
        "messages": [
            {"text": "question", "ts": "1", "user": "U1"},
            {"text": "answer", "ts": "2", "bot_id": "BBOT"},
        ]
    }
    first = ThreadState()
    second = ThreadState()

    await manager.hydrate(
        first,
        event={"ts": "3", "thread_ts": "1", "channel_type": "im"},
        thread_key=("D1", "1"),
        client=client,
    )
    await manager.hydrate(
        second,
        event={"ts": "4", "thread_ts": "1", "channel_type": "im"},
        thread_key=("D2", "1"),
        client=client,
    )

    client.auth_test.assert_awaited_once_with()
    assert client.conversations_replies.await_count == 2
    assert first.history_hydrated and second.history_hydrated
    assert first.messages[-1] == {"role": "assistant", "content": "answer"}


@pytest.mark.asyncio
async def test_delete_last_keeps_only_failed_timestamps_for_retry() -> None:
    manager = HistoryManager(FeedbackIndex())
    state = ThreadState(response_timestamp_groups=[["1.1", "1.2"]])
    client = AsyncMock()
    client.chat_delete.side_effect = (None, RuntimeError("unavailable"))
    say = AsyncMock()

    await manager.delete_last_response(
        state,
        thread_key=("C1", "1"),
        thread_ts="1",
        say=say,
        client=client,
    )

    assert state.response_timestamp_groups == [["1.2"]]
    say.assert_awaited_once_with(
        text="Some bot response messages could not be deleted.",
        thread_ts="1",
    )
