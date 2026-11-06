"""Public, typed plugin API."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, Protocol, TypeAlias

if TYPE_CHECKING:
    from slack_agent_runtime.domain.commands import HelpEntry
    from slack_agent_runtime.domain.models import JsonValue, ParameterValue, ThreadKey
    from slack_agent_runtime.slack.payloads import SlackAction, SlackEvent, SlackView
    from slack_agent_runtime.slack.types import Say, SlackClient

ENTRY_POINT_GROUP: Final = "slack_agent_runtime.plugins"
RouteKind = Literal["command", "event", "action", "view"]
LifecycleHook: TypeAlias = Callable[[], Awaitable[None]]


class PluginHost(Protocol):
    async def get_plugin_state(self, key: ThreadKey, plugin_name: str) -> JsonValue | None: ...

    async def set_plugin_state(
        self, key: ThreadKey, plugin_name: str, value: JsonValue
    ) -> None: ...

    async def clear_plugin_state(self, key: ThreadKey, plugin_name: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ThreadContext:
    """Immutable values describing a thread at dispatch time."""

    model: str
    web_search: bool
    streaming_enabled: bool
    usage_enabled: bool
    parameters: Mapping[str, ParameterValue]
    history_size: int


@dataclass(frozen=True, slots=True)
class CommandContext:
    prompt: str
    thread: ThreadContext
    thread_key: ThreadKey
    thread_ts: str
    say: Say
    client: SlackClient | None
    user_id: str | None
    workspace_id: str | None
    event: SlackEvent
    host: PluginHost


@dataclass(frozen=True, slots=True)
class EventContext:
    event: SlackEvent
    body: Mapping[str, object]
    say: Say
    client: SlackClient
    workspace_id: str | None
    user_id: str | None


@dataclass(frozen=True, slots=True)
class ActionContext:
    action: SlackAction
    body: Mapping[str, object]
    say: Say
    client: SlackClient
    workspace_id: str | None
    user_id: str | None
    host: PluginHost | None = None


@dataclass(frozen=True, slots=True)
class ViewContext:
    view: SlackView
    body: Mapping[str, object]
    client: SlackClient
    workspace_id: str | None
    user_id: str | None


class CommandHandler(Protocol):
    async def __call__(self, context: CommandContext) -> None: ...


class EventHandler(Protocol):
    async def __call__(self, context: EventContext) -> None: ...


class ActionHandler(Protocol):
    async def __call__(self, context: ActionContext) -> None: ...


class ViewHandler(Protocol):
    async def __call__(self, context: ViewContext) -> None: ...


PluginHandler: TypeAlias = CommandHandler | EventHandler | ActionHandler | ViewHandler


@dataclass(frozen=True, slots=True)
class PluginRoute:
    kind: RouteKind
    identifier: str
    handler: PluginHandler

    @classmethod
    def command(cls, identifier: str, handler: CommandHandler) -> PluginRoute:
        return cls("command", identifier, handler)

    @classmethod
    def event(cls, identifier: str, handler: EventHandler) -> PluginRoute:
        return cls("event", identifier, handler)

    @classmethod
    def action(cls, identifier: str, handler: ActionHandler) -> PluginRoute:
        return cls("action", identifier, handler)

    @classmethod
    def view(cls, identifier: str, handler: ViewHandler) -> PluginRoute:
        return cls("view", identifier, handler)


class Plugin(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def routes(self) -> Sequence[PluginRoute]: ...

    async def start(self) -> None: ...

    async def close(self) -> None: ...


async def _noop() -> None:
    pass


@dataclass(frozen=True, slots=True)
class PluginDefinition:
    name: str
    routes: tuple[PluginRoute, ...] = ()
    start_hook: LifecycleHook = _noop
    close_hook: LifecycleHook = _noop
    help_entries: tuple[HelpEntry, ...] = ()

    async def start(self) -> None:
        await self.start_hook()

    async def close(self) -> None:
        await self.close_hook()


@dataclass(frozen=True, slots=True)
class HandlerResult:
    handled: bool
    succeeded: bool
