from collections.abc import AsyncIterator, Sequence
from typing import Any
from unittest.mock import AsyncMock

import pytest

from slack_agent_runtime.domain.models import ThreadStateStore
from slack_agent_runtime.domain.policy import InMemoryPolicy, PolicyConfig
from slack_agent_runtime.inference.client import (
    InferenceResult,
    InferenceStreamComplete,
    InferenceStreamDelta,
    InferenceStreamEvent,
    InferenceUsage,
)
from slack_agent_runtime.services.conversation_coordinator import ConversationCoordinator


class StreamingClient:
    streaming = True

    def __init__(self, deltas: Sequence[str], *, total_tokens: int = 5) -> None:
        self.deltas = deltas
        self.total_tokens = total_tokens

    async def complete(self, *_: Any, **__: Any) -> InferenceResult:
        raise AssertionError("stream_complete should be used")

    async def stream_complete(self, *_: Any, **__: Any) -> AsyncIterator[InferenceStreamEvent]:
        for delta in self.deltas:
            yield InferenceStreamDelta(delta, model="stream-model", request_id="req-stream")
        yield InferenceStreamComplete(
            InferenceResult(
                content="client-final-content",
                model="stream-model",
                request_id="req-stream",
                duration_ms=20,
                usage=InferenceUsage(total_tokens=self.total_tokens),
            )
        )

    async def list_models(self) -> tuple[str, ...]:
        return ("stream-model",)

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_service_streams_preview_then_delivers_exact_long_final_and_charges_usage() -> None:
    content = "first " + ("word " * 1_600)
    inference_client = StreamingClient((content[:2_000], content[2_000:]))
    states = ThreadStateStore()
    policy = InMemoryPolicy(PolicyConfig(token_budget=5))
    service = ConversationCoordinator(
        inference_client,
        default_model="stream-model",
        state_store=states,
        policy=policy,
        stream_update_interval=0.000000001,
    )
    say = AsyncMock(
        side_effect=[
            {"ts": "preview"},
            {"ts": "chunk-2"},
            {"ts": "chunk-3"},
        ]
    )
    client = AsyncMock()

    await service.handle(
        {"text": "question", "ts": "100", "channel": "C1", "user": "U1"},
        say,
        client=client,
    )

    state = states.get(("C1", "100"))
    assert state.messages[-1] == {"role": "assistant", "content": content}
    assert client.chat_update.await_count >= 2
    assert all(call.kwargs["ts"] == "preview" for call in client.chat_update.await_args_list)
    final_update = client.chat_update.await_args_list[-1].kwargs
    assert final_update["blocks"] == []
    delivered = [final_update["text"], *[call.kwargs["text"] for call in say.await_args_list[1:]]]
    assert all(len(chunk) <= 3_500 for chunk in delivered)
    assert "".join(delivered).startswith("first word")

    denied = AsyncMock()
    await service.handle(
        {"text": "again", "ts": "200", "channel": "C2", "user": "U1"},
        denied,
    )
    assert "token budget" in denied.await_args.kwargs["text"].lower()


@pytest.mark.asyncio
async def test_thread_can_disable_streaming(inference_result_factory) -> None:
    inference_client = AsyncMock()
    inference_client.streaming = True
    inference_client.complete.return_value = inference_result_factory("Non-streamed answer")
    service = ConversationCoordinator(
        inference_client,
        default_model="stream-model",
    )

    await service.handle(
        {"text": "stream off", "ts": "100", "channel": "C1", "user": "U1"},
        AsyncMock(),
    )
    await service.handle(
        {
            "text": "question",
            "ts": "101",
            "thread_ts": "100",
            "channel": "C1",
            "user": "U1",
        },
        AsyncMock(),
    )

    inference_client.complete.assert_awaited_once()
    inference_client.stream_complete.assert_not_called()
