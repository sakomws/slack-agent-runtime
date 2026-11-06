"""Conversation orchestration shared by built-in and external plugins.

This module owns thread state, policy checks, inference execution, and Slack
delivery. Transport registration and plugin discovery stay in the Slack
adapter and plugin registry so this service remains directly testable.
"""

import re
from dataclasses import dataclass
from types import MappingProxyType

from slack_agent_runtime.domain.models import (
    ThreadKey,
    ThreadStateStore,
)
from slack_agent_runtime.domain.policy import InMemoryPolicy
from slack_agent_runtime.inference.client import (
    InferenceClient,
)
from slack_agent_runtime.plugins.api import CommandContext, PluginHost, ThreadContext
from slack_agent_runtime.plugins.assistant import AssistantPlugin
from slack_agent_runtime.plugins.feedback import FeedbackPlugin
from slack_agent_runtime.plugins.feedback_index import FeedbackIndex
from slack_agent_runtime.plugins.registry import PluginRegistry
from slack_agent_runtime.services.agent_runner import AgentRunner
from slack_agent_runtime.services.command_executor import CommandExecutor
from slack_agent_runtime.services.completion_runner import CompletionRunner
from slack_agent_runtime.services.thread_repository import ThreadRepository
from slack_agent_runtime.slack.delivery import ResponseDelivery
from slack_agent_runtime.slack.history import HistoryManager
from slack_agent_runtime.slack.payloads import (
    SlackEvent,
    string,
    user_id,
)
from slack_agent_runtime.slack.payloads import (
    workspace_id as payload_workspace_id,
)
from slack_agent_runtime.slack.types import Say, SlackClient
from slack_agent_runtime.storage.persistence import (
    MemoryThreadStatePersistence,
    ThreadStatePersistence,
)

_LEADING_MENTION = re.compile(r"^\s*<@[A-Z0-9]+>\s*", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ConversationDependencies:
    repository: ThreadRepository
    history: HistoryManager
    delivery: ResponseDelivery
    completion: CompletionRunner
    commands: CommandExecutor
    feedback: FeedbackIndex
    assistant_plugin: AssistantPlugin
    feedback_plugin: FeedbackPlugin


def build_conversation_dependencies(
    inference_client: InferenceClient,
    *,
    default_model: str,
    state_store: ThreadStateStore | None = None,
    persistence: ThreadStatePersistence | None = None,
    policy: InMemoryPolicy | None = None,
    stream_update_interval: float = 1.0,
    agent_runner: AgentRunner | None = None,
) -> ConversationDependencies:
    feedback = FeedbackIndex()
    history = HistoryManager(feedback)
    delivery = ResponseDelivery(
        feedback,
        stream_update_interval=stream_update_interval,
    )
    repository = ThreadRepository(
        state_store or ThreadStateStore(),
        persistence or MemoryThreadStatePersistence(),
    )
    completion = CompletionRunner(
        inference_client,
        repository,
        delivery,
        default_model=default_model,
        policy=policy,
        agent_runner=agent_runner,
    )
    commands = CommandExecutor(
        inference_client,
        repository,
        history,
        feedback,
        completion,
        default_model=default_model,
        policy=policy,
    )
    return ConversationDependencies(
        repository=repository,
        history=history,
        delivery=delivery,
        completion=completion,
        commands=commands,
        feedback=feedback,
        assistant_plugin=AssistantPlugin(
            repository,
            commands,
            inference_client,
            feedback,
            default_model=default_model,
            policy=policy,
        ),
        feedback_plugin=FeedbackPlugin(feedback),
    )


class ConversationCoordinator:
    """Coordinate one Slack thread without depending on Bolt registration."""

    def __init__(
        self,
        inference_client: InferenceClient,
        *,
        default_model: str,
        state_store: ThreadStateStore | None = None,
        persistence: ThreadStatePersistence | None = None,
        policy: InMemoryPolicy | None = None,
        plugin_registry: PluginRegistry | None = None,
        stream_update_interval: float = 1.0,
        dependencies: ConversationDependencies | None = None,
    ) -> None:
        self._default_model = default_model
        dependencies = dependencies or build_conversation_dependencies(
            inference_client,
            default_model=default_model,
            state_store=state_store,
            persistence=persistence,
            policy=policy,
            stream_update_interval=stream_update_interval,
        )
        self._policy = policy
        self._repository = dependencies.repository
        self._history = dependencies.history
        self._completion = dependencies.completion
        self._commands = dependencies.commands
        if plugin_registry is None:
            from slack_agent_runtime.plugins.builtins import builtin_plugins

            plugin_registry = PluginRegistry(builtin_plugins(self, dependencies))
        self._plugin_registry = plugin_registry
        self._commands.set_plugin_help_entries(plugin_registry.help_entries)

    def set_plugin_registry(self, registry: PluginRegistry) -> None:
        self._plugin_registry = registry
        self._commands.set_plugin_help_entries(registry.help_entries)

    @property
    def plugin_registry(self) -> PluginRegistry:
        """Expose the immutable routing surface used by production and tests."""

        return self._plugin_registry

    @property
    def plugin_host(self) -> PluginHost:
        return self._repository

    async def handle(
        self,
        event: SlackEvent,
        say: Say,
        *,
        client: SlackClient | None = None,
        workspace_id: str | None = None,
    ) -> None:
        if event.get("bot_id") or event.get("subtype"):
            return

        text = event.get("text")
        if not isinstance(text, str):
            return
        actor_id = user_id(event)
        thread_ts = str(event.get("thread_ts") or event.get("ts") or "")
        if not await self._allow_entrypoint(
            workspace_id=workspace_id or payload_workspace_id(event),
            channel_id=string(event.get("channel")),
            user_id=actor_id,
            say=say,
            thread_ts=thread_ts,
        ):
            return
        prompt = _LEADING_MENTION.sub("", text).strip()
        if not prompt:
            await say(
                text="Please include a question with your mention.",
                thread_ts=thread_ts,
            )
            return

        thread_key = self._thread_key(event)
        state = await self._repository.state(thread_key)
        if not isinstance(event.get("thread_ts"), str):
            state.history_hydrated = True
        if prompt.lower() not in {"clear", "reset"}:
            # A process restart may leave memory empty while Slack still has
            # the conversation. Hydrate before routing so every plugin sees
            # the same bounded history.
            await self._history.hydrate(
                state,
                event=event,
                thread_key=thread_key,
                client=client,
            )
            await self._repository.save(thread_key, state)
        # Commands are routed by their first word through one typed path.
        identifier = prompt.split(maxsplit=1)[0].lower()
        dispatched = await self._plugin_registry.dispatch_command(
            identifier,
            CommandContext(
                prompt=prompt,
                thread=ThreadContext(
                    model=state.model or self._default_model,
                    web_search=state.web_search,
                    streaming_enabled=state.streaming_enabled,
                    usage_enabled=state.usage_enabled,
                    parameters=MappingProxyType(dict(state.params)),
                    history_size=len(state.messages),
                ),
                thread_key=thread_key,
                thread_ts=str(thread_ts),
                say=say,
                client=client,
                user_id=actor_id,
                event=event,
                workspace_id=workspace_id or payload_workspace_id(event),
                host=self._repository,
            ),
        )
        if dispatched.handled:
            if not dispatched.succeeded:
                await say(
                    text="That plugin failed. Please try again shortly.",
                    thread_ts=thread_ts,
                )
            return

        await self._completion.run(
            prompt,
            state=state,
            thread_key=thread_key,
            thread_ts=thread_ts,
            say=say,
            client=client,
            user_id=actor_id,
        )

    async def handle_registered_command(self, context: CommandContext) -> None:
        state = await self._repository.state(context.thread_key)
        handled = await self._commands.handle(
            context.prompt,
            state=state,
            thread_key=context.thread_key,
            thread_ts=context.thread_ts,
            say=context.say,
            client=context.client,
            user_id=context.user_id,
        )
        if not handled:
            await self._completion.run(
                context.prompt,
                state=state,
                thread_key=context.thread_key,
                thread_ts=context.thread_ts,
                say=context.say,
                client=context.client,
                user_id=context.user_id,
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

    async def close(self) -> None:
        await self._repository.close()

    @staticmethod
    def _thread_key(event: SlackEvent) -> ThreadKey:
        channel = str(event.get("channel", ""))
        thread_ts = str(event.get("thread_ts") or event.get("ts") or "")
        return channel, thread_ts
