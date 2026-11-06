from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from slack_agent_runtime.coding.github import PullRequestSummary
    from slack_agent_runtime.slack.payloads import SlackView

ACTION_REGENERATE = "assistant.regenerate"
ACTION_CANCEL = "assistant.cancel"
ACTION_CLEAR = "assistant.clear"
ACTION_WEB_TOGGLE = "assistant.web_toggle"
ACTION_STREAM_TOGGLE = "assistant.stream_toggle"
ACTION_MODEL_OPEN = "assistant.model_open"
ACTION_FEEDBACK_POSITIVE = "assistant.feedback_positive"
ACTION_FEEDBACK_NEGATIVE = "assistant.feedback_negative"
ACTION_MODEL_SELECT = "assistant.model_select"
VIEW_MODEL_SELECT = "assistant.model_select_view"
ACTION_MCP_APPROVE = "mcp.approve"
ACTION_MCP_DENY = "mcp.deny"
ACTION_PR_REVIEW = "reviews.run"

_MODEL_BLOCK_ID = "model"


def progress_blocks(status: str) -> list[dict[str, Any]]:
    return [
        _section(status),
        {
            "type": "actions",
            "elements": [
                _button("Cancel", ACTION_CANCEL, style="danger"),
            ],
        },
    ]


def response_blocks(
    text: str,
    *,
    web_enabled: bool,
    streaming_enabled: bool,
) -> list[dict[str, Any]]:
    web_label = "Web: on" if web_enabled else "Web: off"
    stream_label = "Stream: on" if streaming_enabled else "Stream: off"
    return [
        _section(text),
        {
            "type": "actions",
            "elements": [
                _button("Regenerate", ACTION_REGENERATE),
                _button(web_label, ACTION_WEB_TOGGLE),
                _button(stream_label, ACTION_STREAM_TOGGLE),
                _button("Model", ACTION_MODEL_OPEN),
                _button("Clear", ACTION_CLEAR, style="danger"),
            ],
        },
        {
            "type": "actions",
            "elements": [
                _button("👍", ACTION_FEEDBACK_POSITIVE),
                _button("👎", ACTION_FEEDBACK_NEGATIVE),
            ],
        },
    ]


def mcp_approval_blocks(
    *,
    approval_id: str,
    server_name: str,
    tool_name: str,
) -> list[dict[str, Any]]:
    return [
        _section(f"*MCP approval required*\nServer: `{server_name}`\nTool: `{tool_name}`"),
        {
            "type": "actions",
            "elements": [
                _button("Approve", ACTION_MCP_APPROVE, value=approval_id, style="primary"),
                _button("Deny", ACTION_MCP_DENY, value=approval_id, style="danger"),
            ],
        },
    ]


def pull_request_blocks(
    repository: str,
    pull_requests: tuple[PullRequestSummary, ...],
    *,
    model: str,
    page: int,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [_section(f"*Pull requests for `{repository}`* — page {page}")]
    for pull_request in pull_requests:
        draft = " · draft" if pull_request.draft else ""
        blocks.extend(
            [
                _section(
                    f"*<{pull_request.url}|#{pull_request.number} "
                    f"{_escape(pull_request.title)}>*\n"
                    f"By `{_escape(pull_request.author)}` · "
                    f"{pull_request.state}{draft} · updated {pull_request.updated_at}"
                ),
                {
                    "type": "actions",
                    "elements": [
                        _button(
                            "Review",
                            ACTION_PR_REVIEW,
                            value=json.dumps(
                                {
                                    "repository": repository,
                                    "number": pull_request.number,
                                    "model": model,
                                },
                                separators=(",", ":"),
                            ),
                        )
                    ],
                },
            ]
        )
    return blocks


def model_modal(
    models: tuple[str, ...],
    *,
    current_model: str,
    channel: str,
    thread_ts: str,
) -> dict[str, Any]:
    selectable_models = tuple(model for model in models if len(model) <= 150)[:100]
    if not selectable_models:
        raise ValueError("No Slack-compatible model IDs are available")
    options = [_option(model) for model in selectable_models]
    element: dict[str, Any] = {
        "type": "static_select",
        "action_id": ACTION_MODEL_SELECT,
        "placeholder": {"type": "plain_text", "text": "Choose a model"},
        "options": options,
    }
    if current_model in selectable_models:
        element["initial_option"] = _option(current_model)
    return {
        "type": "modal",
        "callback_id": VIEW_MODEL_SELECT,
        "private_metadata": json.dumps(
            {"channel": channel, "thread_ts": thread_ts},
            separators=(",", ":"),
        ),
        "title": {"type": "plain_text", "text": "Select model"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": _MODEL_BLOCK_ID,
                "label": {"type": "plain_text", "text": "Model"},
                "element": element,
            }
        ],
    }


def model_selection(view: SlackView) -> tuple[str, str, str] | None:
    try:
        metadata = json.loads(str(view["private_metadata"]))
        state = view["state"]
        if not isinstance(metadata, Mapping) or not isinstance(state, Mapping):
            return None
        values = state["values"]
        if not isinstance(values, Mapping):
            return None
        model_block = values[_MODEL_BLOCK_ID]
        if not isinstance(model_block, Mapping):
            return None
        model_action = model_block[ACTION_MODEL_SELECT]
        if not isinstance(model_action, Mapping):
            return None
        selected_option = model_action["selected_option"]
        if not isinstance(selected_option, Mapping):
            return None
        selected = selected_option["value"]
        channel = metadata["channel"]
        thread_ts = metadata["thread_ts"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not all(isinstance(value, str) and value for value in (selected, channel, thread_ts)):
        return None
    return selected, channel, thread_ts


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _button(
    text: str,
    action_id: str,
    *,
    value: str | None = None,
    style: str | None = None,
) -> dict[str, Any]:
    button: dict[str, Any] = {
        "type": "button",
        "text": {"type": "plain_text", "text": text},
        "action_id": action_id,
        "value": value or action_id,
    }
    if style is not None:
        button["style"] = style
    return button


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _option(model: str) -> dict[str, Any]:
    return {
        "text": {"type": "plain_text", "text": model[:75]},
        "value": model,
    }
