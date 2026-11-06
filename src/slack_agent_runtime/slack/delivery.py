"""Slack progress updates, final formatting, chunking, and feedback indexing."""

import time
from collections.abc import Callable, Mapping
from typing import Any

from slack_agent_runtime.domain.models import ThreadKey, ThreadState
from slack_agent_runtime.inference.client import InferenceResult
from slack_agent_runtime.plugins.feedback_index import FeedbackIndex
from slack_agent_runtime.slack.formatting import split_slack_message, to_slack_mrkdwn
from slack_agent_runtime.slack.types import Say, SlackClient
from slack_agent_runtime.slack.ui import progress_blocks, response_blocks


class ResponseDelivery:
    """Owns Slack progress messages, replacements, and final response delivery."""

    def __init__(
        self,
        feedback: FeedbackIndex,
        *,
        stream_update_interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._feedback = feedback
        self._stream_update_interval = stream_update_interval
        self._clock = clock
        self._last_preview_updates: dict[str, float] = {}

    async def post_progress(
        self,
        *,
        state: ThreadState,
        thread_ts: Any,
        say: Say,
        client: SlackClient | None,
    ) -> str | None:
        if client is None:
            return None
        status = "Searching…" if state.web_search else "Thinking…"
        placeholder = await say(
            text=status,
            blocks=progress_blocks(status),
            thread_ts=thread_ts,
        )
        placeholder_ts = message_ts(placeholder)
        if placeholder_ts is not None:
            self._last_preview_updates[placeholder_ts] = self._clock()
        return placeholder_ts

    async def update_progress_preview(
        self,
        content: str,
        *,
        channel: str,
        placeholder_ts: str | None,
        client: SlackClient | None,
    ) -> bool:
        if placeholder_ts is None or client is None or not content:
            return False
        now = self._clock()
        last_update = self._last_preview_updates.get(placeholder_ts, 0.0)
        if now - last_update < self._stream_update_interval:
            return False
        preview = to_slack_mrkdwn(content)
        if len(preview) > 2_900:
            preview = f"…{preview[-2_899:]}"
        await client.chat_update(
            channel=channel,
            ts=placeholder_ts,
            text=preview,
            blocks=progress_blocks(preview),
        )
        self._last_preview_updates[placeholder_ts] = now
        return True

    async def replace_or_say(
        self,
        text: str,
        *,
        channel: str,
        placeholder_ts: str | None,
        thread_ts: Any,
        say: Say,
        client: SlackClient | None,
    ) -> None:
        if placeholder_ts is not None:
            self._last_preview_updates.pop(placeholder_ts, None)
        if placeholder_ts is not None and client is not None:
            await client.chat_update(
                channel=channel,
                ts=placeholder_ts,
                text=text,
                blocks=[],
            )
        else:
            await say(text=text, thread_ts=thread_ts)

    async def deliver_result(
        self,
        result: InferenceResult,
        *,
        state: ThreadState,
        thread_key: ThreadKey,
        thread_ts: Any,
        placeholder_ts: str | None,
        say: Say,
        client: SlackClient | None,
    ) -> None:
        if placeholder_ts is not None:
            self._last_preview_updates.pop(placeholder_ts, None)
        text = to_slack_mrkdwn(result.content)
        if state.usage_enabled:
            text = f"{text}\n\n{usage_footer(result)}"
        chunks = split_slack_message(text) or ["No response content."]
        message_timestamps: list[str] = []

        if placeholder_ts is not None and client is not None:
            update: dict[str, Any] = {
                "channel": thread_key[0],
                "ts": placeholder_ts,
                "text": chunks[0],
                "blocks": [],
            }
            if len(chunks) == 1:
                update["blocks"] = response_blocks(
                    chunks[0],
                    web_enabled=state.web_search,
                    streaming_enabled=state.streaming_enabled,
                )
            await client.chat_update(**update)
            message_timestamps.append(placeholder_ts)
        else:
            first_response = await say(
                text=chunks[0],
                blocks=(
                    response_blocks(
                        chunks[0],
                        web_enabled=state.web_search,
                        streaming_enabled=state.streaming_enabled,
                    )
                    if len(chunks) == 1
                    else []
                ),
                thread_ts=thread_ts,
            )
            first_ts = message_ts(first_response)
            if first_ts is not None:
                message_timestamps.append(first_ts)

        for index, chunk in enumerate(chunks[1:], start=1):
            response = await say(
                text=chunk,
                blocks=(
                    response_blocks(
                        chunk,
                        web_enabled=state.web_search,
                        streaming_enabled=state.streaming_enabled,
                    )
                    if index == len(chunks) - 1
                    else []
                ),
                thread_ts=thread_ts,
            )
            response_ts = message_ts(response)
            if response_ts is not None:
                message_timestamps.append(response_ts)

        self._feedback.index(
            message_timestamps=message_timestamps,
            model=result.model,
            request_id=result.request_id,
            channel=thread_key[0],
            thread_ts=thread_key[1],
        )
        state.record_response_timestamps(message_timestamps)


def message_ts(response: Any) -> str | None:
    if isinstance(response, Mapping):
        value = response.get("ts")
    else:
        data = getattr(response, "data", None)
        value = data.get("ts") if isinstance(data, Mapping) else None
    return value if isinstance(value, str) and value else None


def usage_footer(result: InferenceResult) -> str:
    fields = [
        f"Model: `{result.model}`",
        f"Latency: `{result.duration_ms:.0f} ms`",
    ]
    usage = result.usage
    if usage.total_tokens is not None:
        token_text = f"{usage.total_tokens}"
        if usage.prompt_tokens is not None and usage.completion_tokens is not None:
            token_text += f" ({usage.prompt_tokens} input, {usage.completion_tokens} output)"
        fields.append(f"Tokens: `{token_text}`")
    if result.request_id:
        fields.append(f"Request ID: `{result.request_id}`")
    return "_Usage:_ " + " • ".join(fields)
