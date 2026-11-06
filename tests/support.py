"""Typed helpers that drive the same plugin routes as the Slack adapter."""

from collections.abc import Mapping

from slack_agent_runtime.plugins import ActionContext, EventContext, ViewContext
from slack_agent_runtime.plugins.api import HandlerResult
from slack_agent_runtime.services.conversation_coordinator import ConversationCoordinator
from slack_agent_runtime.slack.payloads import (
    parse_action,
    parse_view,
    user_id,
    workspace_id,
)
from slack_agent_runtime.slack.types import Say, SlackClient


async def dispatch_action(
    coordinator: ConversationCoordinator,
    action_id: str,
    payload: Mapping[str, object],
    say: Say,
    client: SlackClient,
) -> HandlerResult:
    action = parse_action(action_id, payload)
    assert action is not None
    return await coordinator.plugin_registry.dispatch_action(
        action_id,
        ActionContext(
            action=action,
            body=payload,
            say=say,
            client=client,
            workspace_id=workspace_id(payload),
            user_id=user_id(payload),
        ),
    )


async def dispatch_event(
    coordinator: ConversationCoordinator,
    event_name: str,
    event: Mapping[str, object],
    say: Say,
    client: SlackClient,
    *,
    body: Mapping[str, object] | None = None,
) -> HandlerResult:
    payload = body or event
    return await coordinator.plugin_registry.dispatch_event(
        event_name,
        EventContext(
            event=event,
            body=payload,
            say=say,
            client=client,
            workspace_id=workspace_id(payload),
            user_id=user_id(payload),
        ),
    )


async def dispatch_view(
    coordinator: ConversationCoordinator,
    view_id: str,
    payload: Mapping[str, object],
    client: SlackClient,
) -> HandlerResult:
    view = parse_view(payload)
    assert view is not None
    return await coordinator.plugin_registry.dispatch_view(
        view_id,
        ViewContext(
            view=view,
            body=payload,
            client=client,
            workspace_id=workspace_id(payload),
            user_id=user_id(payload),
        ),
    )
