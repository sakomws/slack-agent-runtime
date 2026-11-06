from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest

from slack_agent_runtime.inference.client import InferenceResult, InferenceUsage
from slack_agent_runtime.services.conversation_coordinator import ConversationCoordinator

InferenceResultFactory = Callable[..., InferenceResult]
CoordinatorFactory = Callable[..., ConversationCoordinator]


@pytest.fixture
def inference_result_factory() -> InferenceResultFactory:
    def create(
        content: str,
        *,
        model: str = "kimi-k3",
        request_id: str | None = "req-123",
        duration_ms: float = 250.4,
        prompt_tokens: int | None = 12,
        completion_tokens: int | None = 8,
        total_tokens: int | None = 20,
    ) -> InferenceResult:
        return InferenceResult(
            content=content,
            model=model,
            request_id=request_id,
            duration_ms=duration_ms,
            usage=InferenceUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            ),
        )

    return create


@pytest.fixture
def coordinator_factory() -> CoordinatorFactory:
    def create(
        inference_client: object | None = None,
        *,
        default_model: str = "kimi-k3",
        **kwargs: object,
    ) -> ConversationCoordinator:
        return ConversationCoordinator(
            inference_client or AsyncMock(),
            default_model=default_model,
            **kwargs,
        )

    return create
