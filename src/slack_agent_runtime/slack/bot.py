"""Slack Bolt composition root and Socket Mode lifecycle."""

from typing import Any, Protocol, cast

from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from slack_agent_runtime.coding.github import GitHubCodingClient
from slack_agent_runtime.config import SlackAgentRuntimeSettings
from slack_agent_runtime.domain.policy import InMemoryPolicy
from slack_agent_runtime.inference.client import (
    InferenceClient,
    InferenceRouterClient,
)
from slack_agent_runtime.mcp.approval import ApprovalBroker
from slack_agent_runtime.mcp.config import load_mcp_config
from slack_agent_runtime.mcp.manager import McpManager
from slack_agent_runtime.plugins.api import (
    ActionContext,
    EventContext,
    ViewContext,
)
from slack_agent_runtime.plugins.builtins import builtin_plugins
from slack_agent_runtime.plugins.coding import CodingPlugin
from slack_agent_runtime.plugins.mcp import McpPlugin
from slack_agent_runtime.plugins.registry import load_plugin_registry
from slack_agent_runtime.plugins.reviews import PullRequestReviewPlugin
from slack_agent_runtime.plugins.security_audit import SecurityAuditPlugin
from slack_agent_runtime.services.agent_runner import AgentRunner
from slack_agent_runtime.services.conversation_coordinator import (
    ConversationCoordinator,
    build_conversation_dependencies,
)
from slack_agent_runtime.slack.payloads import (
    parse_action,
    parse_view,
    user_id,
    workspace_id,
)
from slack_agent_runtime.slack.types import Say, SlackClient
from slack_agent_runtime.storage.persistence import (
    MemoryThreadStatePersistence,
    ThreadStatePersistence,
)

_SLACK_ACCEPT_ENCODING = "br, gzip, deflate"


class ReadinessError(RuntimeError):
    """Raised when Slack or the inference endpoint is not ready."""


class SocketModeRunner(Protocol):
    async def start_async(self) -> None: ...

    async def close_async(self) -> None: ...


class SlackAgentRuntime:
    """Wire configured dependencies and plugin routes into Slack Bolt."""

    def __init__(
        self,
        settings: SlackAgentRuntimeSettings,
        *,
        inference_client: InferenceClient | None = None,
        slack_client: AsyncWebClient | None = None,
        persistence: ThreadStatePersistence | None = None,
    ) -> None:
        self._default_model = settings.model
        self._inference_client = inference_client or InferenceRouterClient(
            base_url=settings.inference_base_url,
            api_key=settings.inference_api_key,
            model=settings.model,
        )
        state_persistence = persistence or MemoryThreadStatePersistence()
        policy = InMemoryPolicy(settings.policy_config)
        selected_builtins = settings.builtin_plugins

        def builtin_enabled(name: str, *, available: bool, requirement: str) -> bool:
            if selected_builtins is None:
                return available
            if name not in selected_builtins:
                return False
            if not available:
                raise RuntimeError(f"Built-in plugin `{name}` requires {requirement}")
            return True

        mcp_requested = selected_builtins is None or "mcp" in selected_builtins
        mcp_manager = McpManager(
            load_mcp_config(settings.mcp_config_path if mcp_requested else ""),
            tool_timeout_seconds=settings.mcp_tool_timeout_seconds,
            max_result_chars=settings.mcp_max_result_chars,
        )
        mcp_enabled = builtin_enabled(
            "mcp",
            available=mcp_manager.enabled,
            requirement="SLACK_AGENT_RUNTIME_MCP_CONFIG with at least one server",
        )
        approvals = ApprovalBroker(
            timeout_seconds=settings.mcp_approval_timeout_seconds,
        )
        agent_runner = (
            AgentRunner(
                self._inference_client,
                mcp_manager,
                approvals,
                max_steps=settings.mcp_max_steps,
            )
            if mcp_enabled
            else None
        )
        mcp_plugin = McpPlugin(mcp_manager, approvals, policy=policy) if mcp_enabled else None
        coding_plugin = None
        review_plugin = None
        security_audit_plugin = None
        github_available = bool(settings.coding_github_token)
        if builtin_enabled(
            "coding",
            available=github_available,
            requirement=(
                "SLACK_AGENT_RUNTIME_GITHUB_TOKEN and SLACK_AGENT_RUNTIME_CODING_REPOSITORIES"
            ),
        ):
            coding_plugin = CodingPlugin(
                GitHubCodingClient(
                    token=settings.coding_github_token,
                    allowed_repositories=settings.coding_repositories,
                    api_url=settings.coding_github_api_url,
                    event_type=settings.coding_event_type,
                    workflow_filename=settings.coding_workflow_filename,
                ),
                policy=policy,
                max_prompt_chars=settings.coding_max_prompt_chars,
                agent_runner=agent_runner,
                mcp_manager=mcp_manager if mcp_enabled else None,
            )
        if builtin_enabled(
            "reviews",
            available=github_available,
            requirement=(
                "SLACK_AGENT_RUNTIME_GITHUB_TOKEN and SLACK_AGENT_RUNTIME_CODING_REPOSITORIES"
            ),
        ):
            review_plugin = PullRequestReviewPlugin(
                GitHubCodingClient(
                    token=settings.coding_github_token,
                    allowed_repositories=settings.coding_repositories,
                    api_url=settings.coding_github_api_url,
                    event_type=settings.review_event_type,
                    workflow_filename=settings.review_workflow_filename,
                ),
                self._inference_client,
                policy=policy,
                event_type=settings.review_event_type,
                workflow_filename=settings.review_workflow_filename,
                page_size=settings.review_page_size,
            )
        if builtin_enabled(
            "security_audit",
            available=github_available,
            requirement=(
                "SLACK_AGENT_RUNTIME_GITHUB_TOKEN and SLACK_AGENT_RUNTIME_CODING_REPOSITORIES"
            ),
        ):
            security_audit_plugin = SecurityAuditPlugin(
                GitHubCodingClient(
                    token=settings.coding_github_token,
                    allowed_repositories=settings.coding_repositories,
                    api_url=settings.coding_github_api_url,
                    event_type=settings.security_audit_event_type,
                    workflow_filename=settings.security_audit_workflow_filename,
                ),
                policy=policy,
                event_type=settings.security_audit_event_type,
                workflow_filename=settings.security_audit_workflow_filename,
            )
        dependencies = build_conversation_dependencies(
            self._inference_client,
            default_model=settings.model,
            persistence=state_persistence,
            policy=policy,
            stream_update_interval=settings.stream_update_interval_seconds,
            agent_runner=agent_runner,
        )
        self._conversation = ConversationCoordinator(
            self._inference_client,
            default_model=settings.model,
            policy=policy,
            dependencies=dependencies,
        )
        self._plugins = load_plugin_registry(
            settings.plugins,
            builtin_plugins=builtin_plugins(
                self._conversation,
                dependencies,
                additional=tuple(
                    plugin
                    for plugin in (
                        mcp_plugin,
                        coding_plugin,
                        review_plugin,
                        security_audit_plugin,
                    )
                    if plugin is not None
                ),
            ),
        )
        self._conversation.set_plugin_registry(self._plugins)
        self._plugins_started = False
        slack_client = slack_client or AsyncWebClient(
            token=settings.slack_bot_token,
            headers={"Accept-Encoding": _SLACK_ACCEPT_ENCODING},
        )
        self._slack_client = slack_client
        self._app = AsyncApp(client=slack_client)
        self._handler = cast(
            "SocketModeRunner",
            AsyncSocketModeHandler(self._app, settings.slack_app_token),
        )
        self._register_listeners()

    def _register_listeners(self) -> None:
        # Bolt registration is generated from the validated route table. Core
        # behavior and third-party plugins therefore follow the same path.
        for route in self._plugins.routes_for("event"):
            self._app.event(route.identifier)(self._event_handler(route.identifier))
        for route in self._plugins.routes_for("action"):
            self._app.action(route.identifier)(self._action_handler(route.identifier))
        for route in self._plugins.routes_for("view"):
            self._app.view(route.identifier)(self._view_handler(route.identifier))

    def _event_handler(self, identifier: str) -> Any:
        # Use a factory instead of a loop closure so each Bolt listener keeps
        # the identifier that was current when it was registered.
        async def handle_event(
            event: dict[str, Any],
            body: dict[str, Any],
            say: Say,
            client: AsyncWebClient,
        ) -> None:
            await self._plugins.dispatch_event(
                identifier,
                EventContext(
                    event,
                    body,
                    say,
                    cast("SlackClient", client),
                    workspace_id(body),
                    user_id(body),
                ),
            )

        return handle_event

    def _action_handler(self, identifier: str) -> Any:
        async def handle_action(
            ack: Any,
            body: dict[str, Any],
            say: Say,
            client: AsyncWebClient,
        ) -> None:
            await ack()
            action = parse_action(identifier, body)
            if action is None:
                return
            await self._plugins.dispatch_action(
                identifier,
                ActionContext(
                    action,
                    body,
                    say,
                    cast("SlackClient", client),
                    workspace_id(body),
                    user_id(body),
                    self._conversation.plugin_host,
                ),
            )

        return handle_action

    def _view_handler(self, identifier: str) -> Any:
        async def handle_view(
            ack: Any,
            body: dict[str, Any],
            client: AsyncWebClient,
        ) -> None:
            await ack()
            view = parse_view(body)
            if view is None:
                return
            await self._plugins.dispatch_view(
                identifier,
                ViewContext(
                    view,
                    body,
                    cast("SlackClient", client),
                    workspace_id(body),
                    user_id(body),
                ),
            )

        return handle_view

    async def run(self) -> None:
        await self._start_plugins()
        await self._handler.start_async()

    async def check_readiness(self) -> tuple[str, tuple[str, ...]]:
        await self._start_plugins()
        auth = await self._slack_client.auth_test()
        team = auth.get("team")
        user = auth.get("user")
        identity = " / ".join(value for value in (team, user) if isinstance(value, str) and value)
        if not identity:
            raise ReadinessError("Slack authentication returned no workspace identity")

        models = await self._inference_client.list_models()
        if self._default_model not in models:
            raise ReadinessError(f"Configured model `{self._default_model}` is unavailable")
        return identity, models

    async def close(self) -> None:
        # Every layer gets a shutdown attempt even if an earlier close fails.
        try:
            await self._handler.close_async()
        finally:
            try:
                await self._plugins.close()
            finally:
                try:
                    await self._inference_client.close()
                finally:
                    await self._conversation.close()

    async def _start_plugins(self) -> None:
        # Readiness and run() may both be called during normal CLI startup.
        if not self._plugins_started:
            await self._plugins.start()
            self._plugins_started = True
