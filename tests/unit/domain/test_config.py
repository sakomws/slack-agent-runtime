import pytest

from slack_agent_runtime.config import SlackAgentRuntimeSettings


def test_settings_load_required_secrets_and_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("INFERENCE_ROUTER_API_KEY", "api-test")
    monkeypatch.setenv("INFERENCE_ROUTER_BASE_URL", "https://api.example.com/")
    monkeypatch.setenv("INFERENCE_ROUTER_MODEL", "default-model")

    settings = SlackAgentRuntimeSettings.from_env()

    assert settings.slack_bot_token == "xoxb-test"
    assert settings.slack_app_token == "xapp-test"
    assert settings.inference_api_key == "api-test"
    assert settings.inference_base_url == "https://api.example.com"
    assert settings.model == "default-model"


def test_settings_preserve_unique_plugin_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("INFERENCE_ROUTER_API_KEY", "api-test")
    monkeypatch.setenv("INFERENCE_ROUTER_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("INFERENCE_ROUTER_MODEL", "default-model")
    monkeypatch.setenv("SLACK_AGENT_RUNTIME_PLUGINS", " Echo, incidents,echo ")

    settings = SlackAgentRuntimeSettings.from_env()

    assert settings.plugins == ("echo", "incidents")


def test_settings_load_mcp_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("INFERENCE_ROUTER_API_KEY", "api-test")
    monkeypatch.setenv("INFERENCE_ROUTER_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("INFERENCE_ROUTER_MODEL", "default-model")
    monkeypatch.setenv("SLACK_AGENT_RUNTIME_MCP_CONFIG", "mcp.json")
    monkeypatch.setenv("SLACK_AGENT_RUNTIME_MCP_MAX_STEPS", "4")
    monkeypatch.setenv("SLACK_AGENT_RUNTIME_MCP_TOOL_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("SLACK_AGENT_RUNTIME_MCP_APPROVAL_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("SLACK_AGENT_RUNTIME_MCP_MAX_RESULT_CHARS", "4096")

    settings = SlackAgentRuntimeSettings.from_env()

    assert settings.mcp_config_path == "mcp.json"
    assert settings.mcp_max_steps == 4
    assert settings.mcp_tool_timeout_seconds == 12.5
    assert settings.mcp_approval_timeout_seconds == 60
    assert settings.mcp_max_result_chars == 4096


def test_settings_fail_when_slack_token_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)

    with pytest.raises(
        RuntimeError,
        match="SLACK_BOT_TOKEN is required",
    ):
        SlackAgentRuntimeSettings.from_env()


def test_settings_load_coding_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("INFERENCE_ROUTER_API_KEY", "api-test")
    monkeypatch.setenv("INFERENCE_ROUTER_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("INFERENCE_ROUTER_MODEL", "default-model")
    monkeypatch.setenv("SLACK_AGENT_RUNTIME_GITHUB_TOKEN", "github-token")
    monkeypatch.setenv(
        "SLACK_AGENT_RUNTIME_CODING_REPOSITORIES",
        "owner/repo,owner/another",
    )

    settings = SlackAgentRuntimeSettings.from_env()

    assert settings.coding_github_token == "github-token"
    assert settings.coding_repositories == frozenset({"owner/repo", "owner/another"})
    assert settings.review_event_type == "slack_agent_pr_review"
    assert settings.review_workflow_filename == "pr-review.yml"
    assert settings.review_page_size == 10
    assert settings.security_audit_event_type == "slack_agent_security_audit"
    assert settings.security_audit_workflow_filename == "security-audit.yml"


def test_settings_require_coding_token_and_allowlist_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("INFERENCE_ROUTER_API_KEY", "api-test")
    monkeypatch.setenv("INFERENCE_ROUTER_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("INFERENCE_ROUTER_MODEL", "default-model")
    monkeypatch.setenv("SLACK_AGENT_RUNTIME_GITHUB_TOKEN", "github-token")

    with pytest.raises(RuntimeError, match="must be set together"):
        SlackAgentRuntimeSettings.from_env()


def test_review_page_size_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("INFERENCE_ROUTER_API_KEY", "api-test")
    monkeypatch.setenv("INFERENCE_ROUTER_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("INFERENCE_ROUTER_MODEL", "default-model")
    monkeypatch.setenv("SLACK_AGENT_RUNTIME_REVIEW_PAGE_SIZE", "21")

    with pytest.raises(RuntimeError, match="cannot exceed 20"):
        SlackAgentRuntimeSettings.from_env()
