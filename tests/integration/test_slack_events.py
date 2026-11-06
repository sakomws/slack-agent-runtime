import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from tests.support import dispatch_event

from slack_agent_runtime.domain.policy import InMemoryPolicy, PolicyConfig
from slack_agent_runtime.inference.client import (
    InferenceResult,
    InferenceStreamComplete,
    InferenceStreamDelta,
    InferenceStreamEvent,
    InferenceUsage,
)
from slack_agent_runtime.services.conversation_coordinator import ConversationCoordinator

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "slack"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


class FixtureStreamingClient:
    streaming = True

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, *_: Any, **__: Any) -> InferenceResult:
        raise AssertionError("stream_complete should be used")

    async def stream_complete(
        self,
        *_: Any,
        messages: list[dict[str, str]],
        **__: Any,
    ) -> AsyncIterator[InferenceStreamEvent]:
        self.calls.append(messages)
        yield InferenceStreamDelta("Fixture ")
        yield InferenceStreamDelta("answer")
        yield InferenceStreamComplete(
            InferenceResult(
                content="Fixture answer",
                model="fixture-model",
                request_id="fixture-request",
                duration_ms=10,
                usage=InferenceUsage(total_tokens=4),
            )
        )

    async def list_models(self) -> tuple[str, ...]:
        return ("fixture-model",)

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_app_mention_fixture_runs_full_streaming_reply_flow() -> None:
    envelope = _fixture("app_mention_event.json")
    inference = FixtureStreamingClient()
    service = ConversationCoordinator(
        inference,
        default_model="fixture-model",
        policy=InMemoryPolicy(
            PolicyConfig(
                allowed_workspace_ids=frozenset({"T123"}),
                allowed_channel_ids=frozenset({"C123"}),
                allowed_user_ids=frozenset({"U123"}),
            )
        ),
        stream_update_interval=0.000000001,
    )
    say = AsyncMock(return_value={"ts": "101.000"})
    client = AsyncMock()

    await service.handle(
        envelope["event"],
        say,
        client=client,
        workspace_id=envelope["team_id"],
    )

    assert inference.calls == [[{"role": "user", "content": "explain event loops"}]]
    final = client.chat_update.await_args_list[-1].kwargs
    assert final["text"] == "Fixture answer"
    assert final["blocks"]


@pytest.mark.asyncio
async def test_dm_followup_and_reaction_fixtures_preserve_context_and_feedback() -> None:
    inference = FixtureStreamingClient()
    service = ConversationCoordinator(inference, default_model="fixture-model")
    first = _fixture("direct_message_event.json")
    followup = _fixture("thread_followup_event.json")
    say = AsyncMock(side_effect=({"ts": "101.000"}, {"ts": "202.000"}))

    await service.handle(first["event"], say, workspace_id=first["team_id"])
    await service.handle(followup["event"], say, workspace_id=followup["team_id"])

    assert inference.calls[1] == [
        {"role": "user", "content": "summarize this"},
        {"role": "assistant", "content": "Fixture answer"},
        {"role": "user", "content": "give me an example"},
    ]

    reaction = _fixture("reaction_added_event.json")
    with patch("slack_agent_runtime.plugins.feedback_index._FEEDBACK_COUNTER.add") as counter:
        await dispatch_event(
            service,
            "reaction_added",
            reaction["event"],
            AsyncMock(),
            AsyncMock(),
            body=reaction,
        )
    counter.assert_called_once()
