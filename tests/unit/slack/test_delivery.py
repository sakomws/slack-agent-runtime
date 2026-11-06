from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from slack_agent_runtime.domain.models import ThreadState
from slack_agent_runtime.inference.client import InferenceResult, InferenceUsage
from slack_agent_runtime.plugins.feedback_index import FeedbackIndex
from slack_agent_runtime.slack.delivery import (
    ResponseDelivery,
    message_ts,
    usage_footer,
)
from slack_agent_runtime.slack.ui import ACTION_FEEDBACK_POSITIVE


def _result(content: str) -> InferenceResult:
    return InferenceResult(
        content=content,
        model="model-a",
        request_id="req-1",
        duration_ms=12.6,
        usage=InferenceUsage(
            prompt_tokens=3,
            completion_tokens=4,
            total_tokens=7,
        ),
    )


def test_message_ts_accepts_slack_mapping_and_response_data() -> None:
    assert message_ts({"ts": "1"}) == "1"
    assert message_ts(SimpleNamespace(data={"ts": "2"})) == "2"
    assert message_ts({"ts": ""}) is None
    assert message_ts(SimpleNamespace(data=None)) is None


def test_usage_footer_includes_only_available_usage_fields() -> None:
    assert usage_footer(_result("answer")) == (
        "_Usage:_ Model: `model-a` • Latency: `13 ms` • "
        "Tokens: `7 (3 input, 4 output)` • Request ID: `req-1`"
    )
    sparse = InferenceResult(
        content="answer",
        model="model-b",
        request_id=None,
        duration_ms=1,
        usage=InferenceUsage(),
    )
    assert usage_footer(sparse) == "_Usage:_ Model: `model-b` • Latency: `1 ms`"


@pytest.mark.asyncio
async def test_replace_or_say_uses_placeholder_when_available_and_falls_back_to_say() -> None:
    delivery = ResponseDelivery(FeedbackIndex())
    say = AsyncMock()
    client = AsyncMock()

    await delivery.replace_or_say(
        "replacement",
        channel="C1",
        placeholder_ts="1",
        thread_ts="root",
        say=say,
        client=client,
    )
    await delivery.replace_or_say(
        "fallback",
        channel="C1",
        placeholder_ts=None,
        thread_ts="root",
        say=say,
        client=client,
    )

    client.chat_update.assert_awaited_once_with(
        channel="C1",
        ts="1",
        text="replacement",
        blocks=[],
    )
    say.assert_awaited_once_with(text="fallback", thread_ts="root")


@pytest.mark.asyncio
async def test_progress_preview_is_throttled_and_bounded() -> None:
    now = 0.0
    delivery = ResponseDelivery(
        FeedbackIndex(),
        stream_update_interval=1.0,
        clock=lambda: now,
    )
    say = AsyncMock(return_value={"ts": "preview"})
    client = AsyncMock()
    placeholder_ts = await delivery.post_progress(
        state=ThreadState(),
        thread_ts="100",
        say=say,
        client=client,
    )

    now = 0.5
    assert not await delivery.update_progress_preview(
        "early",
        channel="C1",
        placeholder_ts=placeholder_ts,
        client=client,
    )
    now = 1.0
    assert await delivery.update_progress_preview(
        "x" * 4_000,
        channel="C1",
        placeholder_ts=placeholder_ts,
        client=client,
    )
    now = 1.5
    assert not await delivery.update_progress_preview(
        "still throttled",
        channel="C1",
        placeholder_ts=placeholder_ts,
        client=client,
    )
    now = 2.0
    assert await delivery.update_progress_preview(
        "final preview",
        channel="C1",
        placeholder_ts=placeholder_ts,
        client=client,
    )

    assert client.chat_update.await_count == 2
    first = client.chat_update.await_args_list[0].kwargs
    assert first["ts"] == "preview"
    assert len(first["blocks"][0]["text"]["text"]) <= 2_900


@pytest.mark.asyncio
async def test_deliver_result_splits_long_final_and_puts_actions_on_last_chunk() -> None:
    feedback = FeedbackIndex()
    delivery = ResponseDelivery(feedback)
    state = ThreadState(web_search=True)
    say = AsyncMock(side_effect=({"ts": "2"}, {"ts": "3"}))
    client = AsyncMock()
    content = "x" * 7_000

    await delivery.deliver_result(
        _result(content),
        state=state,
        thread_key=("C1", "root"),
        thread_ts="root",
        placeholder_ts="1",
        say=say,
        client=client,
    )

    first = client.chat_update.await_args.kwargs
    later = [call.kwargs for call in say.await_args_list]
    delivered = [first["text"], *(message["text"] for message in later)]
    assert "".join(delivered) == content
    assert all(len(chunk) <= 2_900 for chunk in delivered)
    assert first["blocks"] == []
    assert later[0]["blocks"] == []
    assert any(
        element.get("action_id") == ACTION_FEEDBACK_POSITIVE
        for block in later[-1]["blocks"]
        for element in block.get("elements", [])
    )
    assert state.response_timestamp_groups == [["1", "2", "3"]]

    with patch("slack_agent_runtime.plugins.feedback_index._FEEDBACK_COUNTER.add") as metric:
        assert feedback.record("positive", channel="C1", message_ts="3", user="U1")
    metric.assert_called_once()


@pytest.mark.asyncio
async def test_deliver_empty_result_without_client_posts_fallback() -> None:
    delivery = ResponseDelivery(FeedbackIndex())
    state = ThreadState()
    say = AsyncMock(return_value={"ts": "1"})

    await delivery.deliver_result(
        _result(""),
        state=state,
        thread_key=("C1", "root"),
        thread_ts="root",
        placeholder_ts=None,
        say=say,
        client=None,
    )

    assert say.await_args.kwargs["text"] == "No response content."
    assert state.response_timestamp_groups == [["1"]]
