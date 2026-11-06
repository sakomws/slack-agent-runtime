"""Assistant plugin: interactive actions and model-selection views."""

import logging
from collections.abc import Mapping

from slack_agent_runtime.domain.models import ThreadState
from slack_agent_runtime.domain.policy import InMemoryPolicy
from slack_agent_runtime.inference.client import InferenceClient
from slack_agent_runtime.plugins.api import (
    ActionContext,
    PluginRoute,
    ViewContext,
)
from slack_agent_runtime.plugins.feedback_index import FeedbackIndex
from slack_agent_runtime.services.command_executor import CommandExecutor
from slack_agent_runtime.services.thread_repository import ThreadRepository
from slack_agent_runtime.slack.payloads import SlackAction, SlackPayload, SlackView
from slack_agent_runtime.slack.payloads import (
    user_id as payload_user_id,
)
from slack_agent_runtime.slack.payloads import (
    workspace_id as payload_workspace_id,
)
from slack_agent_runtime.slack.types import Say, SlackClient
from slack_agent_runtime.slack.ui import (
    ACTION_CANCEL,
    ACTION_CLEAR,
    ACTION_FEEDBACK_NEGATIVE,
    ACTION_FEEDBACK_POSITIVE,
    ACTION_MODEL_OPEN,
    ACTION_REGENERATE,
    ACTION_STREAM_TOGGLE,
    ACTION_WEB_TOGGLE,
    VIEW_MODEL_SELECT,
    model_modal,
    model_selection,
)

logger = logging.getLogger(__name__)

_ACTIONS = (
    ACTION_CANCEL,
    ACTION_CLEAR,
    ACTION_REGENERATE,
    ACTION_WEB_TOGGLE,
    ACTION_STREAM_TOGGLE,
    ACTION_MODEL_OPEN,
    ACTION_FEEDBACK_POSITIVE,
    ACTION_FEEDBACK_NEGATIVE,
)


class AssistantPlugin:
    name = "assistant"

    def __init__(
        self,
        repository: ThreadRepository,
        commands: CommandExecutor,
        inference_client: InferenceClient,
        feedback: FeedbackIndex,
        *,
        default_model: str,
        policy: InMemoryPolicy | None,
    ) -> None:
        self._repository = repository
        self._commands = commands
        self._inference_client = inference_client
        self._feedback = feedback
        self._default_model = default_model
        self._policy = policy
        self.routes = (
            *(PluginRoute.action(identifier, self._action) for identifier in _ACTIONS),
            PluginRoute.view(VIEW_MODEL_SELECT, self._model_submission),
        )

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def _action(self, context: ActionContext) -> None:
        await self._handle_action(
            context.action,
            context.body,
            context.say,
            client=context.client,
            workspace_id=context.workspace_id,
        )

    async def _model_submission(self, context: ViewContext) -> None:
        await self._handle_model_submission(
            context.view,
            client=context.client,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
        )

    async def _handle_action(
        self,
        action: SlackAction,
        body: SlackPayload,
        say: Say,
        *,
        client: SlackClient,
        workspace_id: str | None = None,
    ) -> None:
        action_id = action.action_id
        channel, thread_ts, message_ts = action.channel, action.thread_ts, action.message_ts
        actor_id = payload_user_id(body)
        if not await self._allow_entrypoint(
            workspace_id=workspace_id or payload_workspace_id(body),
            channel_id=channel,
            user_id=actor_id,
            say=say,
            thread_ts=thread_ts,
        ):
            return
        thread_key = (channel, thread_ts)
        state = await self._repository.state(thread_key)

        if action_id == ACTION_CANCEL:
            await self._commands.cancel(state, thread_ts=thread_ts, say=say)
        elif action_id == ACTION_CLEAR:
            await self._commands.clear_thread(
                state,
                thread_key=thread_key,
                thread_ts=thread_ts,
                say=say,
            )
        elif action_id == ACTION_REGENERATE:
            await self._commands.regenerate(
                state,
                thread_key=thread_key,
                thread_ts=thread_ts,
                say=say,
                client=client,
                user_id=actor_id,
            )
        elif action_id == ACTION_WEB_TOGGLE:
            if not await self._require_admin(actor_id, say=say, thread_ts=thread_ts):
                return
            state.web_search = not state.web_search
            await self._repository.save(thread_key, state)
            status = "enabled" if state.web_search else "disabled"
            await say(text=f"Web search {status} for this thread.", thread_ts=thread_ts)
        elif action_id == ACTION_STREAM_TOGGLE:
            if not await self._require_admin(actor_id, say=say, thread_ts=thread_ts):
                return
            state.streaming_enabled = not state.streaming_enabled
            await self._repository.save(thread_key, state)
            status = "enabled" if state.streaming_enabled else "disabled"
            await say(text=f"Streaming {status} for this thread.", thread_ts=thread_ts)
        elif action_id == ACTION_MODEL_OPEN:
            if not await self._require_admin(actor_id, say=say, thread_ts=thread_ts):
                return
            await self._open_model_modal(
                body,
                state=state,
                channel=channel,
                thread_ts=thread_ts,
                say=say,
                client=client,
            )
        elif action_id in {ACTION_FEEDBACK_POSITIVE, ACTION_FEEDBACK_NEGATIVE}:
            sentiment = "positive" if action_id == ACTION_FEEDBACK_POSITIVE else "negative"
            user = body.get("user")
            user_id = user.get("id") if isinstance(user, Mapping) else None
            recorded = self._feedback.record(
                sentiment,
                channel=channel,
                message_ts=message_ts,
                user=user_id,
            )
            if recorded and isinstance(user_id, str) and user_id:
                await client.chat_postEphemeral(
                    channel=channel,
                    user=user_id,
                    text="Thanks for the feedback.",
                    thread_ts=thread_ts,
                )

    async def _handle_model_submission(
        self,
        view: SlackView,
        *,
        client: SlackClient,
        user_id: str | None = None,
        workspace_id: str | None = None,
    ) -> None:
        selection = model_selection(view)
        if selection is None:
            return
        selected_model, channel, thread_ts = selection
        if self._policy is not None:
            access = self._policy.check_access(
                workspace_id=workspace_id,
                channel_id=channel,
                user_id=user_id,
            )
            admin = self._policy.check_admin(user_id)
            denial = access if not access.allowed else admin
            if not denial.allowed:
                await client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=denial.message,
                )
                return
        try:
            models = await self._inference_client.list_models()
        except Exception:
            logger.exception("Failed to validate selected model")
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="I couldn't validate that model. Please try again.",
            )
            return
        if selected_model not in models:
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f"Model `{selected_model}` is no longer available.",
            )
            return
        thread_key = (channel, thread_ts)
        state = await self._repository.state(thread_key)
        state.model = selected_model
        await self._repository.save(thread_key, state)
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"Model for this thread set to `{selected_model}`.",
        )

    async def _open_model_modal(
        self,
        body: Mapping[str, object],
        *,
        state: ThreadState,
        channel: str,
        thread_ts: str,
        say: Say,
        client: SlackClient,
    ) -> None:
        trigger_id = body.get("trigger_id")
        if not isinstance(trigger_id, str) or not trigger_id:
            return
        try:
            models = await self._inference_client.list_models()
            await client.views_open(
                trigger_id=trigger_id,
                view=model_modal(
                    models,
                    current_model=state.model or self._default_model,
                    channel=channel,
                    thread_ts=thread_ts,
                ),
            )
        except Exception:
            logger.exception("Failed to open model selection modal")
            await say(
                text="I couldn't load the model picker. Please try again.",
                thread_ts=thread_ts,
            )

    async def _allow_entrypoint(
        self,
        *,
        workspace_id: str | None,
        channel_id: str | None,
        user_id: str | None,
        say: Say,
        thread_ts: str,
    ) -> bool:
        if self._policy is None:
            return True
        decision = self._policy.check_access(
            workspace_id=workspace_id,
            channel_id=channel_id,
            user_id=user_id,
        )
        if decision.allowed:
            return True
        await say(text=decision.message, thread_ts=thread_ts)
        return False

    async def _require_admin(self, user_id: str | None, *, say: Say, thread_ts: str) -> bool:
        if self._policy is None:
            return True
        decision = self._policy.check_admin(user_id)
        if decision.allowed:
            return True
        await say(text=decision.message, thread_ts=thread_ts)
        return False


def assistant_plugin(
    repository: ThreadRepository,
    commands: CommandExecutor,
    inference_client: InferenceClient,
    feedback: FeedbackIndex,
    *,
    default_model: str,
    policy: InMemoryPolicy | None,
) -> AssistantPlugin:
    return AssistantPlugin(
        repository,
        commands,
        inference_client,
        feedback,
        default_model=default_model,
        policy=policy,
    )
