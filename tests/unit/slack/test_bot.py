from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest
from slack_sdk.web.async_client import AsyncWebClient

from slack_agent_runtime.config import SlackAgentRuntimeSettings
from slack_agent_runtime.services.conversation_coordinator import ConversationCoordinator
from slack_agent_runtime.slack import bot as bot_module
from slack_agent_runtime.slack.bot import ReadinessError, SlackAgentRuntime


def _settings(*, model: str = "kimi-k3") -> SlackAgentRuntimeSettings:
    return SlackAgentRuntimeSettings(
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
        inference_api_key="api-test",
        inference_base_url="https://api.example.com",
        model=model,
    )


def test_slack_client_prefers_brotli_with_standard_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler_factory = Mock(return_value=AsyncMock())
    monkeypatch.setattr(bot_module, "AsyncSocketModeHandler", handler_factory)

    SlackAgentRuntime(_settings(), inference_client=AsyncMock())

    slack_app = handler_factory.call_args.args[0]
    assert slack_app.client.headers["Accept-Encoding"] == "br, gzip, deflate"


def test_production_root_composes_conversation_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bot_module,
        "AsyncSocketModeHandler",
        Mock(return_value=AsyncMock()),
    )

    bot = SlackAgentRuntime(_settings(), inference_client=AsyncMock())
    conversation = bot._conversation

    assert type(conversation) is ConversationCoordinator
    assert conversation.plugin_registry is bot._plugins
    assert tuple(plugin.name for plugin in bot._plugins.plugins) == (
        "commands",
        "conversation",
        "feedback",
        "assistant",
    )


def test_coding_plugin_is_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bot_module,
        "AsyncSocketModeHandler",
        Mock(return_value=AsyncMock()),
    )
    github_client = AsyncMock()
    github_client.allowed_repositories = frozenset({"owner/repo"})
    github_client_factory = Mock(return_value=github_client)
    monkeypatch.setattr(bot_module, "GitHubCodingClient", github_client_factory)
    settings = replace(
        _settings(),
        coding_github_token="github-token",
        coding_repositories=frozenset({"owner/repo"}),
        admin_user_ids=frozenset({"admin"}),
    )

    bot = SlackAgentRuntime(settings, inference_client=AsyncMock())

    assert tuple(plugin.name for plugin in bot._plugins.plugins)[-2:] == (
        "coding",
        "reviews",
    )
    assert github_client_factory.call_count == 2


@pytest.mark.asyncio
async def test_readiness_checks_slack_and_configured_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bot_module, "AsyncSocketModeHandler", Mock(return_value=AsyncMock()))
    inference_client = AsyncMock()
    inference_client.list_models.return_value = ("default-model", "other-model")
    slack_client = AsyncWebClient(token="xoxb-test")
    monkeypatch.setattr(
        slack_client,
        "auth_test",
        AsyncMock(return_value={"team": "Example", "user": "assistant"}),
    )
    bot = SlackAgentRuntime(
        _settings(model="default-model"),
        inference_client=inference_client,
        slack_client=slack_client,
    )

    identity, models = await bot.check_readiness()

    assert identity == "Example / assistant"
    assert models == ("default-model", "other-model")


@pytest.mark.asyncio
async def test_readiness_rejects_unavailable_default_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bot_module, "AsyncSocketModeHandler", Mock(return_value=AsyncMock()))
    inference_client = AsyncMock()
    inference_client.list_models.return_value = ("other-model",)
    slack_client = AsyncWebClient(token="xoxb-test")
    monkeypatch.setattr(
        slack_client,
        "auth_test",
        AsyncMock(return_value={"team": "Example", "user": "assistant"}),
    )
    bot = SlackAgentRuntime(
        _settings(model="default-model"),
        inference_client=inference_client,
        slack_client=slack_client,
    )

    with pytest.raises(ReadinessError, match="default-model"):
        await bot.check_readiness()
