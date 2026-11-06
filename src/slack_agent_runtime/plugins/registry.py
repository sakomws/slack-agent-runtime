"""Plugin validation, discovery, lifecycle, and dispatch."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from dataclasses import dataclass
from importlib.metadata import entry_points as discover_entry_points
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, cast

from slack_agent_runtime.domain.commands import HelpEntry
from slack_agent_runtime.domain.validation import is_plugin_name
from slack_agent_runtime.plugins.api import (
    ENTRY_POINT_GROUP,
    ActionContext,
    CommandContext,
    EventContext,
    HandlerResult,
    Plugin,
    PluginHandler,
    PluginRoute,
    RouteKind,
    ViewContext,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence

_NAMESPACED_ID = re.compile(r"^[a-z][a-z0-9_-]*\.[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_COMMAND_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_EVENT_ID = re.compile(r"^[a-z][a-z0-9_.-]*$")


@dataclass(frozen=True, slots=True)
class _RegisteredRoute:
    plugin_name: str
    route: PluginRoute


class _EntryPoint(Protocol):
    name: str

    def load(self) -> Any: ...


class PluginRegistry:
    def __init__(
        self,
        plugins: Iterable[Plugin],
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        ordered = tuple(plugins)
        by_name: dict[str, Plugin] = {}
        routes: dict[tuple[RouteKind, str], _RegisteredRoute] = {}
        help_entries: list[HelpEntry] = []
        for plugin in ordered:
            validate_plugin_name(plugin.name)
            if plugin.name in by_name:
                raise RuntimeError(f"Duplicate plugin name {plugin.name!r}")
            by_name[plugin.name] = plugin
            for route in plugin.routes:
                _validate_route(plugin.name, route)
                key = (route.kind, route.identifier)
                if key in routes:
                    other = routes[key].plugin_name
                    raise RuntimeError(
                        f"Duplicate {route.kind} route {route.identifier!r} "
                        f"from plugins {other!r} and {plugin.name!r}"
                    )
                routes[key] = _RegisteredRoute(plugin.name, route)
            plugin_help = getattr(plugin, "help_entries", ())
            try:
                entries = tuple(plugin_help)
            except TypeError as exc:
                raise RuntimeError(f"Plugin {plugin.name!r} help entries must be iterable") from exc
            for entry in entries:
                if (
                    not isinstance(entry, HelpEntry)
                    or not entry.group.strip()
                    or not entry.usage.strip()
                ):
                    raise RuntimeError(f"Plugin {plugin.name!r} has an invalid help entry")
                help_entries.append(entry)
        self._plugins = ordered
        self._by_name = MappingProxyType(by_name)
        self._routes = MappingProxyType(routes)
        self._help_entries = tuple(help_entries)
        self._started: list[Plugin] = []
        self._logger = logger or logging.getLogger(__name__)

    @property
    def plugins(self) -> tuple[Plugin, ...]:
        return self._plugins

    @property
    def plugins_by_name(self) -> Mapping[str, Plugin]:
        return self._by_name

    @property
    def help_entries(self) -> tuple[HelpEntry, ...]:
        return self._help_entries

    def handler_for(self, kind: RouteKind, identifier: str) -> PluginHandler | None:
        found = self._routes.get((kind, identifier))
        return found.route.handler if found else None

    def routes_for(self, kind: RouteKind) -> tuple[PluginRoute, ...]:
        return tuple(route.route for (key, _), route in self._routes.items() if key == kind)

    async def start(self) -> None:
        if self._started:
            return
        for plugin in self._plugins:
            try:
                await plugin.start()
            except Exception as exc:
                await self._rollback()
                raise RuntimeError(f"Plugin {plugin.name!r} failed to start: {exc}") from exc
            self._started.append(plugin)

    async def close(self) -> None:
        failures: list[str] = []
        while self._started:
            plugin = self._started.pop()
            try:
                await plugin.close()
            except Exception as exc:
                failures.append(f"{plugin.name!r}: {exc}")
                self._logger.exception("Plugin failed to close")
        if failures:
            raise RuntimeError("Plugins failed to close: " + "; ".join(failures))

    async def dispatch_command(self, identifier: str, context: CommandContext) -> HandlerResult:
        return await self._dispatch("command", identifier, context)

    async def dispatch_event(self, identifier: str, context: EventContext) -> HandlerResult:
        return await self._dispatch("event", identifier, context)

    async def dispatch_action(self, identifier: str, context: ActionContext) -> HandlerResult:
        return await self._dispatch("action", identifier, context)

    async def dispatch_view(self, identifier: str, context: ViewContext) -> HandlerResult:
        return await self._dispatch("view", identifier, context)

    async def _dispatch(self, kind: RouteKind, identifier: str, context: object) -> HandlerResult:
        found = self._routes.get((kind, identifier))
        if found is None:
            return HandlerResult(False, False)
        try:
            result = cast("Callable[[object], Awaitable[None]]", found.route.handler)(context)
            if not inspect.isawaitable(result):
                raise TypeError("handler must return an awaitable")
            await result
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception(
                "Plugin handler failed",
                extra={
                    "plugin": found.plugin_name,
                    "route_kind": kind,
                    "route": identifier,
                },
            )
            return HandlerResult(True, False)
        return HandlerResult(True, True)

    async def _rollback(self) -> None:
        while self._started:
            try:
                await self._started.pop().close()
            except Exception:
                self._logger.exception("Plugin startup rollback failed")


def validate_plugin_name(name: object) -> str:
    if not is_plugin_name(name):
        raise RuntimeError(
            "Plugin names must start with a lowercase letter and contain only "
            "lowercase letters, digits, underscores, or hyphens"
        )
    return cast("str", name)


def _validate_route(plugin: str, route: PluginRoute) -> None:
    identifier = route.identifier
    if route.kind == "command" and _COMMAND_ID.fullmatch(identifier) is None:
        raise RuntimeError(
            f"Plugin {plugin!r} command {identifier!r} must be one lowercase command word"
        )
    if route.kind == "event" and _EVENT_ID.fullmatch(identifier) is None:
        raise RuntimeError(f"Plugin {plugin!r} event {identifier!r} is invalid")
    if route.kind in ("action", "view"):
        if _NAMESPACED_ID.fullmatch(identifier) is None:
            raise RuntimeError(
                f"Plugin {plugin!r} {route.kind} ID {identifier!r} must be "
                "namespaced as '<plugin-name>.<id>'"
            )
        if identifier.partition(".")[0] != plugin:
            raise RuntimeError(
                f"Plugin {plugin!r} {route.kind} ID {identifier!r} must use namespace {plugin!r}"
            )


def load_plugin_registry(
    selected_names: Sequence[str],
    *,
    builtin_plugins: Iterable[Plugin] = (),
    logger: logging.Logger | None = None,
) -> PluginRegistry:
    if len(set(selected_names)) != len(selected_names):
        raise RuntimeError("Plugin is selected more than once")
    discovered = tuple(
        cast(
            "Iterable[_EntryPoint]",
            discover_entry_points(group=ENTRY_POINT_GROUP),
        )
    )
    grouped: dict[str, list[_EntryPoint]] = {}
    for item in discovered:
        grouped.setdefault(item.name, []).append(item)
    plugins = list(builtin_plugins)
    for name in selected_names:
        validate_plugin_name(name)
        matches = grouped.get(name, [])
        if not matches:
            raise RuntimeError(
                f"Selected plugin {name!r} was not found in entry-point group "
                f"{ENTRY_POINT_GROUP!r}; install its package or remove it "
                "from configuration"
            )
        if len(matches) > 1:
            raise RuntimeError(
                f"Selected plugin {name!r} has multiple entry points in "
                f"{ENTRY_POINT_GROUP!r}; uninstall the duplicate package"
            )
        try:
            factory = matches[0].load()
            plugin = factory()
            validate_plugin_name(plugin.name)
            if plugin.name != name:
                raise TypeError(f"factory returned plugin named {plugin.name!r}, expected {name!r}")
        except Exception as exc:
            raise RuntimeError(
                f"Could not load selected plugin {name!r} from entry-point "
                f"group {ENTRY_POINT_GROUP!r}: {exc}"
            ) from exc
        plugins.append(cast("Plugin", plugin))
    return PluginRegistry(plugins, logger=logger)
