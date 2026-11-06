"""Execute built-in thread commands."""

import logging
from collections.abc import Callable

from slack_agent_runtime.domain.commands import (
    CommandKind,
    HelpEntry,
    build_help_text,
    parse_command,
    parse_parameter,
)
from slack_agent_runtime.domain.models import (
    ThreadKey,
    ThreadState,
)
from slack_agent_runtime.domain.policy import InMemoryPolicy
from slack_agent_runtime.inference.client import InferenceClient
from slack_agent_runtime.plugins.feedback_index import FeedbackIndex
from slack_agent_runtime.services.completion_runner import CompletionRunner
from slack_agent_runtime.services.thread_repository import ThreadRepository
from slack_agent_runtime.slack.history import HistoryManager
from slack_agent_runtime.slack.types import Say, SlackClient

logger = logging.getLogger(__name__)


class CommandExecutor:
    def __init__(
        self,
        inference_client: InferenceClient,
        repository: ThreadRepository,
        history: HistoryManager,
        feedback: FeedbackIndex,
        completion: CompletionRunner,
        *,
        default_model: str,
        policy: InMemoryPolicy | None,
    ) -> None:
        self._inference_client = inference_client
        self._repository = repository
        self._history = history
        self._feedback = feedback
        self._completion = completion
        self._default_model = default_model
        self._policy = policy
        self._plugin_help_entries: tuple[HelpEntry, ...] = ()

    def set_plugin_help_entries(self, entries: tuple[HelpEntry, ...]) -> None:
        self._plugin_help_entries = entries

    async def handle(
        self,
        prompt: str,
        *,
        state: ThreadState,
        thread_key: ThreadKey,
        thread_ts: str,
        say: Say,
        client: SlackClient | None,
        user_id: str | None,
    ) -> bool:
        command = parse_command(prompt)
        if command is None:
            return False

        if command.kind is CommandKind.HELP:
            await say(
                text=build_help_text(self._plugin_help_entries),
                thread_ts=thread_ts,
            )
            return True

        if command.kind is CommandKind.CANCEL:
            await self.cancel(state, thread_ts=thread_ts, say=say)
            return True

        if command.kind is CommandKind.CLEAR:
            await self.clear_thread(
                state,
                thread_key=thread_key,
                thread_ts=thread_ts,
                say=say,
            )
            return True

        if command.kind is CommandKind.DELETE_LAST:
            await self._history.delete_last_response(
                state,
                thread_key=thread_key,
                thread_ts=thread_ts,
                say=say,
                client=client,
            )
            await self._repository.save(thread_key, state)
            return True

        if command.kind is CommandKind.REGENERATE:
            await self.regenerate(
                state,
                thread_key=thread_key,
                thread_ts=thread_ts,
                say=say,
                client=client,
                user_id=user_id,
            )
            return True

        if command.kind is CommandKind.MODEL:
            if command.value is not None and not await self._require_admin(
                user_id, say=say, thread_ts=thread_ts
            ):
                return True
            await self._handle_model_command(
                requested_model=command.value,
                state=state,
                thread_ts=thread_ts,
                say=say,
            )
            await self._repository.save(thread_key, state)
            return True

        if command.kind is CommandKind.TOGGLE:
            if command.name is None:
                raise ValueError("Toggle command is missing its name")
            toggle = {
                "web": ("Web search", "web_search"),
                "stream": ("Streaming", "streaming_enabled"),
                "usage": ("Usage details", "usage_enabled"),
            }.get(command.name)
            if toggle is None:
                raise ValueError(f"Unknown toggle command: {command.name}")
            label, attribute = toggle
            if command.value is not None and not await self._require_admin(
                user_id, say=say, thread_ts=thread_ts
            ):
                return True
            await self._handle_toggle(
                label=label,
                action=command.value,
                current=bool(getattr(state, attribute)),
                setter=lambda value: setattr(state, attribute, value),
                thread_ts=thread_ts,
                say=say,
            )
            await self._repository.save(thread_key, state)
            return True

        if command.kind is CommandKind.PARAMETER:
            if command.name is None:
                raise ValueError("Parameter command is missing its name")
            if command.value is not None and not await self._require_admin(
                user_id, say=say, thread_ts=thread_ts
            ):
                return True
            await self._handle_param_command(
                name=command.name,
                raw_value=command.value,
                state=state,
                thread_ts=thread_ts,
                say=say,
            )
            await self._repository.save(thread_key, state)
            return True

        if command.kind is CommandKind.SETTINGS_DEFAULT:
            if not await self._require_admin(user_id, say=say, thread_ts=thread_ts):
                return True
            state.reset_settings()
            await self._repository.save(thread_key, state)
            await say(
                text="Thread settings reset to defaults.",
                thread_ts=thread_ts,
            )
            return True

        if command.kind is CommandKind.SETTINGS:
            await self._show_settings(state, thread_ts=thread_ts, say=say)
            return True
        raise ValueError(f"Unhandled command: {command.kind}")

    async def cancel(self, state: ThreadState, *, thread_ts: str, say: Say) -> None:
        task = state.active_task
        if task is None or task.done():
            await say(text="There is no active request to cancel.", thread_ts=thread_ts)
            return
        task.cancel()
        await say(text="Cancellation requested.", thread_ts=thread_ts)

    async def clear_thread(
        self,
        state: ThreadState,
        *,
        thread_key: ThreadKey,
        thread_ts: str,
        say: Say,
    ) -> None:
        task = state.active_task
        if task is not None and not task.done():
            task.cancel()
        state.clear()
        self._feedback.clear_thread(*thread_key)
        await self._repository.delete(thread_key)
        await say(
            text="Thread history and settings cleared.",
            thread_ts=thread_ts,
        )

    async def regenerate(
        self,
        state: ThreadState,
        *,
        thread_key: ThreadKey,
        thread_ts: str,
        say: Say,
        client: SlackClient | None,
        user_id: str | None,
    ) -> None:
        if len(state.messages) < 2:
            await say(text="There is no previous answer to regenerate.", thread_ts=thread_ts)
            return
        user, assistant = state.messages[-2:]
        if user.get("role") != "user" or assistant.get("role") != "assistant":
            await say(text="There is no previous answer to regenerate.", thread_ts=thread_ts)
            return
        prompt = user.get("content")
        if not prompt:
            await say(text="There is no previous answer to regenerate.", thread_ts=thread_ts)
            return
        await self._completion.run(
            prompt,
            state=state,
            thread_key=thread_key,
            thread_ts=thread_ts,
            say=say,
            client=client,
            history=state.messages[:-2],
            replace_last=True,
            user_id=user_id,
        )

    async def _handle_model_command(
        self,
        *,
        requested_model: str | None,
        state: ThreadState,
        thread_ts: str,
        say: Say,
    ) -> None:
        current_model = state.model or self._default_model
        if requested_model is not None and requested_model.lower() == "default":
            state.model = None
            await say(
                text=f"Model for this thread reset to `{self._default_model}`.",
                thread_ts=thread_ts,
            )
            return

        try:
            models = await self._inference_client.list_models()
        except Exception:
            logger.exception("Slack Agent Runtime failed to list models")
            await say(
                text="I couldn't load the available models. Please try again shortly.",
                thread_ts=thread_ts,
            )
            return

        available = ", ".join(f"`{model}`" for model in models)
        if requested_model is None:
            await say(
                text=f"Current model: `{current_model}`\nAvailable models: {available}",
                thread_ts=thread_ts,
            )
            return

        if requested_model not in models:
            await say(
                text=f"Unknown model `{requested_model}`.\nAvailable models: {available}",
                thread_ts=thread_ts,
            )
            return

        state.model = requested_model
        await say(
            text=f"Model for this thread set to `{requested_model}`.",
            thread_ts=thread_ts,
        )

    async def _handle_toggle(
        self,
        *,
        label: str,
        action: str | None,
        current: bool,
        setter: Callable[[bool], None],
        thread_ts: str,
        say: Say,
    ) -> None:
        if action is None:
            status = "on" if current else "off"
            await say(text=f"{label} is `{status}` for this thread.", thread_ts=thread_ts)
            return
        enabled = action.lower() == "on"
        setter(enabled)
        status = "enabled" if enabled else "disabled"
        await say(text=f"{label} {status} for this thread.", thread_ts=thread_ts)

    async def _handle_param_command(
        self,
        *,
        name: str,
        raw_value: str | None,
        state: ThreadState,
        thread_ts: str,
        say: Say,
    ) -> None:
        api_name = "reasoning_effort" if name == "reasoning" else name.replace("-", "_")
        if raw_value is None:
            value = state.params.get(api_name, "default")
            await say(text=f"`{name}` is `{value}`.", thread_ts=thread_ts)
            return

        if raw_value.lower() == "default":
            state.params.pop(api_name, None)
            await say(text=f"`{name}` reset to its model default.", thread_ts=thread_ts)
            return

        value, error = parse_parameter(name, raw_value)
        if error is not None:
            await say(text=error, thread_ts=thread_ts)
            return

        state.params[api_name] = value
        await say(text=f"`{name}` set to `{value}` for this thread.", thread_ts=thread_ts)

    async def _show_settings(
        self,
        state: ThreadState,
        *,
        thread_ts: str,
        say: Say,
    ) -> None:
        lines = [
            f"Model: `{state.model or self._default_model}`",
            f"Web search: `{'on' if state.web_search else 'off'}`",
            f"Streaming: `{'on' if state.streaming_enabled else 'off'}`",
            f"Usage details: `{'on' if state.usage_enabled else 'off'}`",
            f"Temperature: `{state.params.get('temperature', 'default')}`",
            f"Top-p: `{state.params.get('top_p', 'default')}`",
            f"Max tokens: `{state.params.get('max_tokens', 'default')}`",
            f"Reasoning: `{state.params.get('reasoning_effort', 'default')}`",
            f"History messages: `{len(state.messages)}`",
        ]
        await say(text="\n".join(lines), thread_ts=thread_ts)

    async def _require_admin(self, user_id: str | None, *, say: Say, thread_ts: str) -> bool:
        if self._policy is None:
            return True
        decision = self._policy.check_admin(user_id)
        if decision.allowed:
            return True
        await say(text=decision.message, thread_ts=thread_ts)
        return False
