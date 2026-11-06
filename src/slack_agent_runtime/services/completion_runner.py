"""Run inference requests and deliver their results."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from slack_agent_runtime.inference.client import (
    InferenceClient,
    InferenceMessage,
    InferenceOptions,
    InferenceResult,
    InferenceRouterError,
    InferenceStreamComplete,
    InferenceStreamDelta,
)
from slack_agent_runtime.slack.ui import mcp_approval_blocks

if TYPE_CHECKING:
    from collections.abc import Sequence

    from slack_agent_runtime.domain.models import ChatMessage, ThreadKey, ThreadState
    from slack_agent_runtime.domain.policy import InMemoryPolicy
    from slack_agent_runtime.mcp.approval import PendingApproval
    from slack_agent_runtime.services.agent_runner import AgentRunner
    from slack_agent_runtime.services.thread_repository import ThreadRepository
    from slack_agent_runtime.slack.delivery import ResponseDelivery
    from slack_agent_runtime.slack.types import Say, SlackClient

logger = logging.getLogger(__name__)


class CompletionRunner:
    def __init__(
        self,
        inference_client: InferenceClient,
        repository: ThreadRepository,
        delivery: ResponseDelivery,
        *,
        default_model: str,
        policy: InMemoryPolicy | None,
        agent_runner: AgentRunner | None = None,
    ) -> None:
        self._inference_client = inference_client
        self._repository = repository
        self._response_delivery = delivery
        self._default_model = default_model
        self._policy = policy
        self._agent_runner = agent_runner

    async def run(
        self,
        prompt: str,
        *,
        state: ThreadState,
        thread_key: ThreadKey,
        thread_ts: str,
        say: Say,
        client: SlackClient | None,
        history: Sequence[ChatMessage] | None = None,
        replace_last: bool = False,
        user_id: str | None,
    ) -> None:
        active_task = state.active_task
        if active_task is not None and not active_task.done():
            await say(
                text="A request is already running in this thread. Use `cancel` first.",
                thread_ts=thread_ts,
            )
            return

        permit = None
        if self._policy is not None:
            decision, permit = self._policy.acquire_inference(user_id)
            if not decision.allowed:
                await say(text=decision.message, thread_ts=thread_ts)
                return

        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Slack request is not running in an asyncio task")
        state.active_task = task
        self._repository.track(task)
        total_tokens: int | None = None
        try:
            placeholder_ts = await self._response_delivery.post_progress(
                state=state,
                thread_ts=thread_ts,
                say=say,
                client=client,
            )
            state.active_placeholder_ts = placeholder_ts

            model = state.model or self._default_model
            request_messages = (
                [*history, {"role": "user", "content": prompt}]
                if history is not None
                else state.messages_with(prompt)
            )
            try:
                result = await self._complete(
                    messages=request_messages,
                    model=model,
                    web_search=state.web_search,
                    state=state,
                    channel=thread_key[0],
                    placeholder_ts=placeholder_ts,
                    client=client,
                    thread_key=thread_key,
                    thread_ts=thread_ts,
                    say=say,
                    user_id=user_id,
                )
                total_tokens = result.usage.total_tokens
            except asyncio.CancelledError:
                await self._response_delivery.replace_or_say(
                    "Request cancelled.",
                    channel=thread_key[0],
                    placeholder_ts=placeholder_ts,
                    thread_ts=thread_ts,
                    say=say,
                    client=client,
                )
                return
            except Exception:
                logger.exception("Slack Agent Runtime request failed")
                await self._response_delivery.replace_or_say(
                    "I couldn't complete that request. Please try again shortly.",
                    channel=thread_key[0],
                    placeholder_ts=placeholder_ts,
                    thread_ts=thread_ts,
                    say=say,
                    client=client,
                )
                return

            if replace_last:
                state.pop_last_exchange()
            state.record_exchange(prompt, result.content)
            await self._repository.save(thread_key, state)
            await self._response_delivery.deliver_result(
                result,
                state=state,
                thread_key=thread_key,
                thread_ts=thread_ts,
                placeholder_ts=placeholder_ts,
                say=say,
                client=client,
            )
            await self._repository.save(thread_key, state)
        finally:
            if state.active_task is task:
                state.active_task = None
                state.active_placeholder_ts = None
            self._repository.untrack(task)
            if self._policy is not None and permit is not None:
                self._policy.release_inference(permit, total_tokens=total_tokens)

    async def _complete(
        self,
        *,
        messages: Sequence[InferenceMessage],
        model: str,
        web_search: bool,
        state: ThreadState,
        channel: str,
        placeholder_ts: str | None,
        client: SlackClient | None,
        thread_key: ThreadKey,
        thread_ts: str,
        say: Say,
        user_id: str | None,
    ) -> InferenceResult:
        temperature = state.params.get("temperature")
        top_p = state.params.get("top_p")
        max_tokens = state.params.get("max_tokens")
        reasoning_effort = state.params.get("reasoning_effort")
        request_temperature = (
            float(temperature)
            if isinstance(temperature, (int, float)) and not isinstance(temperature, bool)
            else None
        )
        request_top_p = (
            float(top_p)
            if isinstance(top_p, (int, float)) and not isinstance(top_p, bool)
            else None
        )
        request_max_tokens = (
            max_tokens if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) else None
        )
        request_reasoning = reasoning_effort if isinstance(reasoning_effort, str) else None
        options = InferenceOptions()
        if request_temperature is not None:
            options["temperature"] = request_temperature
        if request_top_p is not None:
            options["top_p"] = request_top_p
        if request_max_tokens is not None:
            options["max_tokens"] = request_max_tokens
        if request_reasoning is not None:
            options["reasoning_effort"] = request_reasoning
        if self._agent_runner is not None:

            async def update_progress(content: str) -> None:
                await self._response_delivery.update_progress_preview(
                    content,
                    channel=channel,
                    placeholder_ts=placeholder_ts,
                    client=client,
                )

            async def request_approval(approval: PendingApproval) -> None:
                await say(
                    text=(
                        "MCP approval required for "
                        f"`{approval.tool.server_name}/{approval.tool.tool_name}`."
                    ),
                    blocks=mcp_approval_blocks(
                        approval_id=approval.id,
                        server_name=approval.tool.server_name,
                        tool_name=approval.tool.tool_name,
                    ),
                    thread_ts=thread_ts,
                )

            return await self._agent_runner.run(
                messages=messages,
                model=model,
                web_search=web_search,
                streaming=state.streaming_enabled,
                options=options,
                thread_key=thread_key,
                requester_id=user_id,
                progress=update_progress,
                request_approval=request_approval,
            )
        if not state.streaming_enabled or self._inference_client.streaming is not True:
            return await self._inference_client.complete(
                messages=messages,
                model=model,
                web_search=web_search,
                **options,
            )

        stream = self._inference_client.stream_complete(
            messages=messages,
            model=model,
            web_search=web_search,
            **options,
        )
        parts: list[str] = []
        final_result: InferenceResult | None = None
        async for event in stream:
            if isinstance(event, InferenceStreamDelta):
                if final_result is not None:
                    raise InferenceRouterError("Inference Router returned an invalid stream")
                parts.append(event.content)
                await self._response_delivery.update_progress_preview(
                    "".join(parts),
                    channel=channel,
                    placeholder_ts=placeholder_ts,
                    client=client,
                )
            elif isinstance(event, InferenceStreamComplete):
                if final_result is not None:
                    raise InferenceRouterError("Inference Router returned an invalid stream")
                final_result = event.result
            else:
                raise InferenceRouterError("Inference Router returned an invalid stream")

        if final_result is None:
            raise InferenceRouterError("Inference Router stream ended unexpectedly")
        streamed_content = "".join(parts)
        if not streamed_content:
            streamed_content = final_result.content
        return replace(final_result, content=streamed_content)
