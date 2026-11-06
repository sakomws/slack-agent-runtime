"""Rebuild bounded conversation history from Slack and delete prior responses."""

import logging
import re
from collections.abc import Mapping
from typing import Any

from slack_agent_runtime.domain.commands import parse_command
from slack_agent_runtime.domain.models import ChatMessage, ThreadKey, ThreadState
from slack_agent_runtime.plugins.feedback_index import FeedbackIndex
from slack_agent_runtime.slack.payloads import SlackEvent
from slack_agent_runtime.slack.types import Say, SlackClient

logger = logging.getLogger(__name__)

_LEADING_MENTION = re.compile(r"^\s*<@[A-Z0-9]+>\s*", re.IGNORECASE)
_SLACK_HISTORY_FETCH_LIMIT = 100


class HistoryManager:
    """Restores and removes Slack-backed conversation history."""

    def __init__(self, feedback: FeedbackIndex) -> None:
        self._feedback = feedback
        self._slack_bot_user_id: str | None = None
        self._slack_bot_id: str | None = None

    async def hydrate(
        self,
        state: ThreadState,
        *,
        event: SlackEvent,
        thread_key: ThreadKey,
        client: SlackClient | None,
    ) -> None:
        current_ts = event.get("ts")
        if (
            client is None
            or not isinstance(event.get("thread_ts"), str)
            or not isinstance(current_ts, str)
            or state.messages
            or state.history_hydrated
        ):
            return

        async with state.hydration_lock:
            if state.messages or state.history_hydrated:
                return
            try:
                bot_user_id, bot_id = await self._slack_bot_identity(client)
                response = await client.conversations_replies(
                    channel=thread_key[0],
                    ts=thread_key[1],
                    limit=_SLACK_HISTORY_FETCH_LIMIT,
                )
                raw_messages = response.get("messages")
                if not isinstance(raw_messages, list):
                    raise ValueError("Slack thread response has no message list")
                messages, response_groups = rebuild_slack_history(
                    raw_messages,
                    bot_user_id=bot_user_id,
                    bot_id=bot_id,
                    current_ts=current_ts,
                    require_mention=event.get("channel_type") != "im",
                )
                state.restore_history(messages, response_groups)
            except Exception:
                state.history_hydrated = True
                logger.warning(
                    "Could not restore Slack Agent Runtime thread history",
                    exc_info=True,
                    extra={"channel": thread_key[0], "thread_ts": thread_key[1]},
                )

    async def delete_last_response(
        self,
        state: ThreadState,
        *,
        thread_key: ThreadKey,
        thread_ts: Any,
        say: Say,
        client: SlackClient | None,
    ) -> None:
        if not state.response_timestamp_groups:
            await say(
                text="There is no bot response to delete in this thread.",
                thread_ts=thread_ts,
            )
            return
        if client is None:
            await say(
                text="Slack message deletion is unavailable.",
                thread_ts=thread_ts,
            )
            return

        timestamps = state.response_timestamp_groups[-1]
        failed: list[str] = []
        for message_ts in timestamps:
            try:
                await client.chat_delete(channel=thread_key[0], ts=message_ts)
                self._feedback.remove(thread_key[0], message_ts)
            except Exception:
                logger.exception(
                    "Failed to delete Slack Agent Runtime response",
                    extra={"channel": thread_key[0], "message_ts": message_ts},
                )
                failed.append(message_ts)

        if failed:
            state.response_timestamp_groups[-1] = failed
            await say(
                text="Some bot response messages could not be deleted.",
                thread_ts=thread_ts,
            )
            return
        state.response_timestamp_groups.pop()
        await say(text="Latest bot response deleted.", thread_ts=thread_ts)

    async def _slack_bot_identity(self, client: SlackClient) -> tuple[str, str | None]:
        if self._slack_bot_user_id is None:
            response = await client.auth_test()
            user_id = response.get("user_id")
            if not isinstance(user_id, str) or not user_id:
                raise ValueError("Slack auth response has no bot user ID")
            bot_id = response.get("bot_id")
            self._slack_bot_user_id = user_id
            self._slack_bot_id = bot_id if isinstance(bot_id, str) else None
        return self._slack_bot_user_id, self._slack_bot_id


def rebuild_slack_history(
    raw_messages: list[Any],
    *,
    bot_user_id: str,
    bot_id: str | None,
    current_ts: str,
    require_mention: bool,
) -> tuple[list[ChatMessage], list[list[str]]]:
    history: list[ChatMessage] = []
    response_groups: list[list[str]] = []
    active_group: list[str] = []
    assistant_index: int | None = None
    awaiting_response = False
    mention = f"<@{bot_user_id}>"

    for raw_message in raw_messages:
        if not isinstance(raw_message, Mapping):
            continue
        message_ts = raw_message.get("ts")
        text = raw_message.get("text")
        if (
            not isinstance(message_ts, str)
            or message_ts == current_ts
            or not isinstance(text, str)
            or not text.strip()
        ):
            continue

        is_own_bot = raw_message.get("user") == bot_user_id or (
            bot_id is not None and raw_message.get("bot_id") == bot_id
        )
        if is_own_bot:
            if not awaiting_response:
                continue
            content = text.partition("\n\n_Usage:_")[0].strip()
            if not content:
                continue
            if assistant_index is None:
                history.append({"role": "assistant", "content": content})
                assistant_index = len(history) - 1
            else:
                history[assistant_index]["content"] += f"\n\n{content}"
            active_group.append(message_ts)
            continue

        if raw_message.get("bot_id") is not None:
            continue
        if require_mention and mention not in text:
            continue

        prompt = _LEADING_MENTION.sub("", text).strip()
        if not prompt:
            continue
        if prompt.lower() in {"clear", "reset"}:
            history.clear()
            response_groups.clear()
            active_group = []
            assistant_index = None
            awaiting_response = False
            continue
        if active_group:
            response_groups.append(active_group)
        active_group = []
        assistant_index = None
        awaiting_response = False
        if parse_command(prompt) is not None:
            continue
        history.append({"role": "user", "content": prompt})
        awaiting_response = True

    if active_group:
        response_groups.append(active_group)
    return history, response_groups
