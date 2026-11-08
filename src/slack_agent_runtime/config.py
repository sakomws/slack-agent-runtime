"""Environment parsing and validation for process-wide bot settings."""

import math
import os
import re
from dataclasses import dataclass

from slack_agent_runtime.domain.policy import PolicyConfig

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_EVENT_TYPE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_OPTIONAL_BUILTIN_PLUGINS = frozenset({"mcp", "coding", "reviews", "security_audit"})


def _value(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _required(*names: str) -> str:
    value = _value(*names)
    if not value:
        raise RuntimeError(f"{names[0]} is required")
    return value


def _id_set(*names: str) -> frozenset[str]:
    value = _value(*names)
    return frozenset(item.strip() for item in value.split(",") if item.strip())


def _name_list(*names: str) -> tuple[str, ...]:
    values = (item.strip().lower() for item in _value(*names).split(","))
    return tuple(dict.fromkeys(item for item in values if item))


def _builtin_plugin_list(name: str) -> tuple[str, ...] | None:
    raw = os.getenv(name)
    if raw is None or raw.strip().lower() == "auto":
        return None
    values = tuple(dict.fromkeys(item.strip().lower() for item in raw.split(",") if item.strip()))
    unknown = set(values) - _OPTIONAL_BUILTIN_PLUGINS
    if unknown:
        allowed = ", ".join(sorted(_OPTIONAL_BUILTIN_PLUGINS))
        raise RuntimeError(f"{name} must contain only: {allowed}")
    return values


def _repository_set(name: str) -> frozenset[str]:
    repositories = _id_set(name)
    if any(_REPOSITORY.fullmatch(repository) is None for repository in repositories):
        raise RuntimeError(f"{name} must contain comma-separated owner/repository names")
    return repositories


def _non_negative_int(name: str, *, default: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < 0:
        raise RuntimeError(f"{name} must be zero or greater")
    return value


def _positive_int(name: str, *, default: int) -> int:
    value = _non_negative_int(name, default=default)
    if value == 0:
        raise RuntimeError(f"{name} must be greater than zero")
    return value


def _positive_float(name: str, *, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"{name} must be greater than zero")
    return value


@dataclass(frozen=True)
class SlackAgentRuntimeSettings:
    slack_bot_token: str
    slack_app_token: str
    inference_api_key: str
    inference_base_url: str
    model: str
    allowed_workspace_ids: frozenset[str] = frozenset()
    allowed_channel_ids: frozenset[str] = frozenset()
    allowed_user_ids: frozenset[str] = frozenset()
    admin_user_ids: frozenset[str] = frozenset()
    request_limit: int = 0
    request_window_seconds: float = 60.0
    token_budget: int = 0
    token_window_seconds: float = 3600.0
    max_concurrent_requests: int = 0
    stream_update_interval_seconds: float = 1.0
    thread_state_backend: str = "memory"
    thread_state_url: str = ""
    plugins: tuple[str, ...] = ()
    builtin_plugins: tuple[str, ...] | None = None
    mcp_config_path: str = ""
    mcp_max_steps: int = 8
    mcp_tool_timeout_seconds: float = 30.0
    mcp_approval_timeout_seconds: float = 300.0
    mcp_max_result_chars: int = 20_000
    coding_github_token: str = ""
    coding_repositories: frozenset[str] = frozenset()
    coding_github_api_url: str = "https://api.github.com"
    coding_event_type: str = "slack_agent_coding_job"
    coding_workflow_filename: str = "coding-agent.yml"
    coding_max_prompt_chars: int = 8_000
    review_event_type: str = "slack_agent_pr_review"
    review_workflow_filename: str = "pr-review.yml"
    review_page_size: int = 10
    security_audit_event_type: str = "slack_agent_security_audit"
    security_audit_workflow_filename: str = "security-audit.yml"

    @property
    def policy_config(self) -> PolicyConfig:
        return PolicyConfig(
            allowed_workspace_ids=self.allowed_workspace_ids,
            allowed_channel_ids=self.allowed_channel_ids,
            allowed_user_ids=self.allowed_user_ids,
            admin_user_ids=self.admin_user_ids,
            request_limit=self.request_limit,
            request_window_seconds=self.request_window_seconds,
            token_budget=self.token_budget,
            token_window_seconds=self.token_window_seconds,
            max_concurrent_requests=self.max_concurrent_requests,
        )

    @classmethod
    def from_env(cls) -> "SlackAgentRuntimeSettings":
        thread_state_backend = os.getenv("THREAD_STATE_BACKEND", "memory").strip().lower()
        if thread_state_backend not in {"memory", "sqlite", "redis", "postgres"}:
            raise RuntimeError("THREAD_STATE_BACKEND must be memory, sqlite, redis, or postgres")
        thread_state_url = os.getenv("THREAD_STATE_URL", "").strip()
        if thread_state_backend != "memory" and not thread_state_url:
            raise RuntimeError("THREAD_STATE_URL is required for non-memory persistence")
        coding_token = os.getenv("SLACK_AGENT_RUNTIME_GITHUB_TOKEN", "").strip()
        coding_repositories = _repository_set("SLACK_AGENT_RUNTIME_CODING_REPOSITORIES")
        if bool(coding_token) != bool(coding_repositories):
            raise RuntimeError(
                "SLACK_AGENT_RUNTIME_GITHUB_TOKEN and "
                "SLACK_AGENT_RUNTIME_CODING_REPOSITORIES must be set together"
            )
        coding_event_type = os.getenv(
            "SLACK_AGENT_RUNTIME_CODING_EVENT_TYPE",
            "slack_agent_coding_job",
        ).strip()
        if _EVENT_TYPE.fullmatch(coding_event_type) is None:
            raise RuntimeError("SLACK_AGENT_RUNTIME_CODING_EVENT_TYPE is invalid")
        coding_workflow_filename = os.getenv(
            "SLACK_AGENT_RUNTIME_CODING_WORKFLOW_FILENAME",
            "coding-agent.yml",
        ).strip()
        if (
            not coding_workflow_filename
            or "/" in coding_workflow_filename
            or not coding_workflow_filename.endswith((".yml", ".yaml"))
        ):
            raise RuntimeError("SLACK_AGENT_RUNTIME_CODING_WORKFLOW_FILENAME is invalid")
        review_event_type = os.getenv(
            "SLACK_AGENT_RUNTIME_REVIEW_EVENT_TYPE",
            "slack_agent_pr_review",
        ).strip()
        if _EVENT_TYPE.fullmatch(review_event_type) is None:
            raise RuntimeError("SLACK_AGENT_RUNTIME_REVIEW_EVENT_TYPE is invalid")
        review_workflow_filename = os.getenv(
            "SLACK_AGENT_RUNTIME_REVIEW_WORKFLOW_FILENAME",
            "pr-review.yml",
        ).strip()
        if (
            not review_workflow_filename
            or "/" in review_workflow_filename
            or not review_workflow_filename.endswith((".yml", ".yaml"))
        ):
            raise RuntimeError("SLACK_AGENT_RUNTIME_REVIEW_WORKFLOW_FILENAME is invalid")
        review_page_size = _positive_int(
            "SLACK_AGENT_RUNTIME_REVIEW_PAGE_SIZE",
            default=10,
        )
        if review_page_size > 20:
            raise RuntimeError("SLACK_AGENT_RUNTIME_REVIEW_PAGE_SIZE cannot exceed 20")
        security_audit_event_type = os.getenv(
            "SLACK_AGENT_RUNTIME_SECURITY_AUDIT_EVENT_TYPE",
            "slack_agent_security_audit",
        ).strip()
        if _EVENT_TYPE.fullmatch(security_audit_event_type) is None:
            raise RuntimeError("SLACK_AGENT_RUNTIME_SECURITY_AUDIT_EVENT_TYPE is invalid")
        security_audit_workflow_filename = os.getenv(
            "SLACK_AGENT_RUNTIME_SECURITY_AUDIT_WORKFLOW_FILENAME",
            "security-audit.yml",
        ).strip()
        if (
            not security_audit_workflow_filename
            or "/" in security_audit_workflow_filename
            or not security_audit_workflow_filename.endswith((".yml", ".yaml"))
        ):
            raise RuntimeError("SLACK_AGENT_RUNTIME_SECURITY_AUDIT_WORKFLOW_FILENAME is invalid")
        return cls(
            slack_bot_token=_required("SLACK_BOT_TOKEN"),
            slack_app_token=_required("SLACK_APP_TOKEN"),
            inference_api_key=_required("INFERENCE_ROUTER_API_KEY"),
            inference_base_url=_required("INFERENCE_ROUTER_BASE_URL").rstrip("/"),
            model=_required("INFERENCE_ROUTER_MODEL"),
            allowed_workspace_ids=_id_set("SLACK_AGENT_RUNTIME_ALLOWED_WORKSPACE_IDS"),
            allowed_channel_ids=_id_set("SLACK_AGENT_RUNTIME_ALLOWED_CHANNEL_IDS"),
            allowed_user_ids=_id_set("SLACK_AGENT_RUNTIME_ALLOWED_USER_IDS"),
            admin_user_ids=_id_set("SLACK_AGENT_RUNTIME_ADMIN_USER_IDS"),
            request_limit=_non_negative_int("SLACK_AGENT_RUNTIME_REQUEST_LIMIT"),
            request_window_seconds=_positive_float(
                "SLACK_AGENT_RUNTIME_REQUEST_WINDOW_SECONDS",
                default=60.0,
            ),
            token_budget=_non_negative_int("SLACK_AGENT_RUNTIME_TOKEN_BUDGET"),
            token_window_seconds=_positive_float(
                "SLACK_AGENT_RUNTIME_TOKEN_WINDOW_SECONDS",
                default=3600.0,
            ),
            max_concurrent_requests=_non_negative_int(
                "SLACK_AGENT_RUNTIME_MAX_CONCURRENT_REQUESTS"
            ),
            stream_update_interval_seconds=_positive_float(
                "SLACK_AGENT_RUNTIME_STREAM_UPDATE_INTERVAL_SECONDS",
                default=1.0,
            ),
            thread_state_backend=thread_state_backend,
            thread_state_url=thread_state_url,
            plugins=_name_list("SLACK_AGENT_RUNTIME_PLUGINS"),
            builtin_plugins=_builtin_plugin_list("SLACK_AGENT_RUNTIME_BUILTIN_PLUGINS"),
            mcp_config_path=os.getenv("SLACK_AGENT_RUNTIME_MCP_CONFIG", "").strip(),
            mcp_max_steps=_positive_int("SLACK_AGENT_RUNTIME_MCP_MAX_STEPS", default=8),
            mcp_tool_timeout_seconds=_positive_float(
                "SLACK_AGENT_RUNTIME_MCP_TOOL_TIMEOUT_SECONDS",
                default=30.0,
            ),
            mcp_approval_timeout_seconds=_positive_float(
                "SLACK_AGENT_RUNTIME_MCP_APPROVAL_TIMEOUT_SECONDS",
                default=300.0,
            ),
            mcp_max_result_chars=_positive_int(
                "SLACK_AGENT_RUNTIME_MCP_MAX_RESULT_CHARS",
                default=20_000,
            ),
            coding_github_token=coding_token,
            coding_repositories=coding_repositories,
            coding_github_api_url=os.getenv(
                "SLACK_AGENT_RUNTIME_GITHUB_API_URL",
                "https://api.github.com",
            )
            .strip()
            .rstrip("/"),
            coding_event_type=coding_event_type,
            coding_workflow_filename=coding_workflow_filename,
            coding_max_prompt_chars=_positive_int(
                "SLACK_AGENT_RUNTIME_CODING_MAX_PROMPT_CHARS",
                default=8_000,
            ),
            review_event_type=review_event_type,
            review_workflow_filename=review_workflow_filename,
            review_page_size=review_page_size,
            security_audit_event_type=security_audit_event_type,
            security_audit_workflow_filename=security_audit_workflow_filename,
        )
