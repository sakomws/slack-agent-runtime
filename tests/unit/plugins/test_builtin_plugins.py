import asyncio
import operator
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock

import pytest

from slack_agent_runtime.domain.models import ThreadStateStore
from slack_agent_runtime.plugins.api import (
    CommandContext,
    PluginDefinition,
    PluginRoute,
)
from slack_agent_runtime.plugins.registry import PluginRegistry
from slack_agent_runtime.services.conversation_coordinator import ConversationCoordinator
from slack_agent_runtime.services.thread_repository import ThreadRepository
from slack_agent_runtime.storage.persistence import (
    MemoryThreadStatePersistence,
    SnapshotError,
)


def _service() -> ConversationCoordinator:
    return ConversationCoordinator(AsyncMock(), default_model="default-model")


def test_builtin_plugins_register_all_slack_surfaces() -> None:
    service = _service()
    registry = service.plugin_registry

    assert {route.identifier for route in registry.routes_for("event")} == {
        "app_mention",
        "message",
        "reaction_added",
    }
    assert "model" in {route.identifier for route in registry.routes_for("command")}
    assert "assistant.cancel" in {route.identifier for route in registry.routes_for("action")}
    assert {route.identifier for route in registry.routes_for("view")} == {
        "assistant.model_select_view"
    }


@pytest.mark.asyncio
async def test_builtin_command_dispatch_preserves_existing_behavior() -> None:
    inference = AsyncMock()
    inference.list_models.return_value = ("default-model", "other-model")
    service = ConversationCoordinator(inference, default_model="default-model")
    say = AsyncMock()

    await service.handle(
        {"text": "model", "ts": "100", "channel": "C1", "user": "U1"},
        say,
    )

    say.assert_awaited_once_with(
        text="Current model: `default-model`\nAvailable models: `default-model`, `other-model`",
        thread_ts="100",
    )


@pytest.mark.asyncio
async def test_external_command_can_persist_namespaced_thread_state() -> None:
    service = _service()
    seen: list[CommandContext] = []

    async def remember(context: CommandContext) -> None:
        seen.append(context)
        await context.host.set_plugin_state(
            context.thread_key,
            "remember",
            {"last_prompt": context.prompt},
        )
        await context.say(text="Remembered.", thread_ts=context.thread_ts)

    plugin = PluginDefinition(
        "remember",
        (PluginRoute.command("remember", remember),),
    )
    registry = PluginRegistry((*service.plugin_registry.plugins, plugin))
    service.set_plugin_registry(registry)
    say = AsyncMock()

    await service.handle(
        {"text": "remember this", "ts": "100", "channel": "C1", "user": "U1"},
        say,
    )

    assert await seen[0].host.get_plugin_state(("C1", "100"), "remember") == {
        "last_prompt": "remember this"
    }
    say.assert_awaited_once_with(text="Remembered.", thread_ts="100")


@pytest.mark.asyncio
async def test_invalid_plugin_state_is_rejected_without_mutating_thread() -> None:
    repository = ThreadRepository(ThreadStateStore(), MemoryThreadStatePersistence())
    key = ("C1", "100")

    with pytest.raises(SnapshotError, match="plugin name"):
        await repository.set_plugin_state(key, "Invalid Name", {"enabled": True})

    assert await repository.get_plugin_state(key, "Invalid Name") is None


@pytest.mark.asyncio
async def test_command_context_exposes_immutable_typed_values() -> None:
    service = _service()
    seen: list[CommandContext] = []

    async def inspect_context(context: CommandContext) -> None:
        seen.append(context)

    plugin = PluginDefinition(
        "inspect",
        (PluginRoute.command("inspect", inspect_context),),
    )
    service.set_plugin_registry(PluginRegistry((*service.plugin_registry.plugins, plugin)))

    await service.handle(
        {"text": "inspect", "ts": "100", "channel": "C1", "user": "U1"},
        AsyncMock(),
    )

    context = seen[0]
    assert not hasattr(context, "state")
    assert context.thread.model == "default-model"
    with pytest.raises(FrozenInstanceError):
        context.prompt = "changed"
    with pytest.raises(TypeError):
        operator.setitem(context.thread.parameters, "temperature", 0.5)


@pytest.mark.asyncio
async def test_repository_shutdown_cancels_active_tasks() -> None:
    repository = ThreadRepository(
        ThreadStateStore(),
        MemoryThreadStatePersistence(),
    )

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(wait_forever())
    repository.track(task)

    await repository.close()

    assert task.cancelled()
