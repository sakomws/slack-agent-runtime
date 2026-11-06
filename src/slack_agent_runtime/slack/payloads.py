"""Validated Slack payload boundary types and parsers."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeAlias

SlackPayload: TypeAlias = Mapping[str, object]
SlackEvent: TypeAlias = Mapping[str, object]
SlackView: TypeAlias = Mapping[str, object]


@dataclass(frozen=True, slots=True)
class SlackAction:
    action_id: str
    channel: str
    thread_ts: str
    message_ts: str
    value: str = ""


def string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def user_id(payload: SlackPayload) -> str | None:
    user = payload.get("user")
    return string(user.get("id")) if isinstance(user, Mapping) else string(user)


def workspace_id(payload: SlackPayload) -> str | None:
    team = payload.get("team")
    return (
        string(team.get("id"))
        if isinstance(team, Mapping)
        else string(team) or string(payload.get("team_id"))
    )


def parse_action(action_id: str, body: SlackPayload) -> SlackAction | None:
    channel_payload, message, container = (
        body.get("channel"),
        body.get("message"),
        body.get("container"),
    )
    if not isinstance(channel_payload, Mapping) or not isinstance(message, Mapping):
        return None
    channel = string(channel_payload.get("id"))
    message_ts = string(message.get("ts"))
    thread_ts = string(message.get("thread_ts"))
    if thread_ts is None and isinstance(container, Mapping):
        thread_ts = string(container.get("thread_ts"))
    if channel is None or message_ts is None:
        return None
    actions = body.get("actions")
    value = ""
    if isinstance(actions, list) and actions and isinstance(actions[0], Mapping):
        value = string(actions[0].get("value")) or ""
    return SlackAction(action_id, channel, thread_ts or message_ts, message_ts, value)


def parse_view(body: SlackPayload) -> SlackView | None:
    view = body.get("view")
    return view if isinstance(view, Mapping) else None
