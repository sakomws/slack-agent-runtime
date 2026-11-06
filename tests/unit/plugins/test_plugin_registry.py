from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

from slack_agent_runtime.plugins import (
    ENTRY_POINT_GROUP,
    ActionContext,
    PluginDefinition,
    PluginRegistry,
    PluginRoute,
    ViewContext,
)
from slack_agent_runtime.plugins import registry as plugins
from slack_agent_runtime.plugins.registry import load_plugin_registry
from slack_agent_runtime.slack.payloads import SlackAction

if TYPE_CHECKING:
    from collections.abc import Callable


async def _handler(**kwargs: Any) -> None:
    del kwargs


class _FakeSay:
    async def __call__(self, **kwargs: Any) -> object:
        del kwargs
        return {}


class _FakeSlackClient:
    async def auth_test(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {}

    async def chat_delete(self, **kwargs: Any) -> object:
        del kwargs
        return {}

    async def chat_postEphemeral(self, **kwargs: Any) -> object:
        del kwargs
        return {}

    async def chat_postMessage(self, **kwargs: Any) -> object:
        del kwargs
        return {}

    async def chat_update(self, **kwargs: Any) -> object:
        del kwargs
        return {}

    async def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {}

    async def views_open(self, **kwargs: Any) -> object:
        del kwargs
        return {}


@dataclass
class _FakeEntryPoint:
    name: str
    value: Callable[[], object]
    loads: int = 0

    def load(self) -> object:
        self.loads += 1
        return self.value


def _install_entry_points(
    monkeypatch: pytest.MonkeyPatch,
    *entry_points: _FakeEntryPoint,
) -> None:
    def discover(**kwargs: str) -> tuple[_FakeEntryPoint, ...]:
        assert kwargs == {"group": ENTRY_POINT_GROUP}
        return entry_points

    monkeypatch.setattr(plugins, "discover_entry_points", discover)


def test_loader_imports_only_selected_plugins_and_preserves_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha = _FakeEntryPoint("alpha", lambda: PluginDefinition("alpha"))
    beta = _FakeEntryPoint("beta", lambda: PluginDefinition("beta"))
    unused = _FakeEntryPoint(
        "unused",
        lambda: pytest.fail("an unselected entry point must not be loaded"),
    )
    _install_entry_points(monkeypatch, unused, beta, alpha)

    registry = load_plugin_registry(["alpha", "beta"])

    assert tuple(plugin.name for plugin in registry.plugins) == ("alpha", "beta")
    assert alpha.loads == 1
    assert beta.loads == 1
    assert unused.loads == 0


def test_loader_keeps_builtins_before_selected_plugins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external = _FakeEntryPoint("external", lambda: PluginDefinition("external"))
    _install_entry_points(monkeypatch, external)

    registry = load_plugin_registry(
        ["external"],
        builtin_plugins=(PluginDefinition("builtin"),),
    )

    assert tuple(plugin.name for plugin in registry.plugins) == ("builtin", "external")


def test_loader_reports_missing_and_broken_selected_plugins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_factory() -> object:
        raise ImportError("optional dependency is absent")

    broken = _FakeEntryPoint("broken", broken_factory)
    _install_entry_points(monkeypatch, broken)

    with pytest.raises(RuntimeError, match=r"missing.*was not found.*install"):
        load_plugin_registry(["missing"])
    with pytest.raises(RuntimeError, match=r"broken.*optional dependency is absent"):
        load_plugin_registry(["broken"])


def test_loader_rejects_duplicate_selection_and_entry_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duplicate_one = _FakeEntryPoint("duplicate", lambda: PluginDefinition("duplicate"))
    duplicate_two = _FakeEntryPoint("duplicate", lambda: PluginDefinition("duplicate"))
    _install_entry_points(monkeypatch, duplicate_one, duplicate_two)

    with pytest.raises(RuntimeError, match="selected more than once"):
        load_plugin_registry(["duplicate", "duplicate"])
    with pytest.raises(RuntimeError, match="multiple entry points"):
        load_plugin_registry(["duplicate"])
    assert duplicate_one.loads == duplicate_two.loads == 0


def test_loader_rejects_factory_name_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    entry_point = _FakeEntryPoint("expected", lambda: PluginDefinition("different"))
    _install_entry_points(monkeypatch, entry_point)

    with pytest.raises(RuntimeError, match="named 'different', expected 'expected'"):
        load_plugin_registry(["expected"])


def test_registry_rejects_duplicate_plugin_names_and_routes() -> None:
    with pytest.raises(RuntimeError, match="Duplicate plugin name 'alpha'"):
        PluginRegistry([PluginDefinition("alpha"), PluginDefinition("alpha")])

    first = PluginDefinition(
        "first",
        (PluginRoute.event("message", _handler),),
    )
    second = PluginDefinition(
        "second",
        (PluginRoute.event("message", _handler),),
    )
    with pytest.raises(RuntimeError, match=r"Duplicate event route 'message'.*'first'.*'second'"):
        PluginRegistry([first, second])


@pytest.mark.parametrize("name", ["Upper", "with.dot", "-leading", "two words"])
def test_registry_rejects_invalid_plugin_names(name: str) -> None:
    with pytest.raises(RuntimeError, match="Plugin names"):
        PluginRegistry([PluginDefinition(name)])


@pytest.mark.parametrize(
    ("plugin_name", "route", "message"),
    [
        ("polls", PluginRoute.action("vote", _handler), "must be namespaced"),
        ("polls", PluginRoute.view("other.create", _handler), "must use namespace 'polls'"),
        ("polls", PluginRoute.action("polls.", _handler), "must be namespaced"),
        ("polls", PluginRoute.command("create poll", _handler), "one lowercase command word"),
    ],
)
def test_registry_validates_route_identifiers(
    plugin_name: str,
    route: PluginRoute,
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        PluginRegistry([PluginDefinition(plugin_name, (route,))])


def test_registry_accepts_all_route_types_and_exposes_handlers() -> None:
    routes = (
        PluginRoute.command("poll", _handler),
        PluginRoute.event("reaction_added", _handler),
        PluginRoute.action("polls.vote", _handler),
        PluginRoute.view("polls.create", _handler),
    )
    registry = PluginRegistry([PluginDefinition("polls", routes)])

    for route in routes:
        assert registry.handler_for(route.kind, route.identifier) is _handler


@pytest.mark.asyncio
async def test_lifecycle_uses_configured_and_reverse_order() -> None:
    calls: list[str] = []

    def hook(name: str) -> Callable[[], Any]:
        async def run() -> None:
            calls.append(name)

        return run

    registry = PluginRegistry(
        [
            PluginDefinition(
                "alpha",
                start_hook=hook("start alpha"),
                close_hook=hook("close alpha"),
            ),
            PluginDefinition(
                "beta",
                start_hook=hook("start beta"),
                close_hook=hook("close beta"),
            ),
        ]
    )

    await registry.start()
    await registry.close()

    assert calls == ["start alpha", "start beta", "close beta", "close alpha"]


@pytest.mark.asyncio
async def test_start_failure_rolls_back_started_plugins() -> None:
    calls: list[str] = []

    async def start_alpha() -> None:
        calls.append("start alpha")

    async def close_alpha() -> None:
        calls.append("close alpha")

    async def start_beta() -> None:
        raise ValueError("cannot connect")

    registry = PluginRegistry(
        [
            PluginDefinition("alpha", start_hook=start_alpha, close_hook=close_alpha),
            PluginDefinition("beta", start_hook=start_beta),
        ]
    )

    with pytest.raises(RuntimeError, match="'beta' failed to start: cannot connect"):
        await registry.start()

    assert calls == ["start alpha", "close alpha"]


@pytest.mark.asyncio
async def test_dispatch_isolates_handler_failure_and_reports_outcome(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail(context: ActionContext) -> None:
        del context
        raise ValueError("handler exploded")

    registry = PluginRegistry(
        [PluginDefinition("polls", (PluginRoute.action("polls.vote", fail),))]
    )

    context = ActionContext(
        SlackAction("polls.vote", "C1", "1", "1"),
        {"secret": "not logged"},
        _FakeSay(),
        _FakeSlackClient(),
        None,
        None,
    )
    failed = await registry.dispatch_action("polls.vote", context)
    missing = await registry.dispatch_action("unknown", context)

    assert failed.handled is True
    assert failed.succeeded is False
    assert missing.handled is False
    assert missing.succeeded is False
    assert "Plugin handler failed" in caplog.text
    assert "not logged" not in caplog.text


@pytest.mark.asyncio
async def test_dispatch_does_not_swallow_cancellation() -> None:
    async def cancel(context: ViewContext) -> None:
        del context
        raise asyncio.CancelledError

    registry = PluginRegistry(
        [PluginDefinition("polls", (PluginRoute.view("polls.create", cancel),))]
    )

    with pytest.raises(asyncio.CancelledError):
        await registry.dispatch_view(
            "polls.create",
            ViewContext({}, {}, _FakeSlackClient(), None, None),
        )
