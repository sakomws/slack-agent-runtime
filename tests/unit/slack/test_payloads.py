from slack_agent_runtime.slack.payloads import (
    parse_action,
    parse_view,
    user_id,
    workspace_id,
)


def test_payload_identity_parsing_accepts_slack_shapes() -> None:
    assert user_id({"user": {"id": "U1"}}) == "U1"
    assert user_id({"user": "U2"}) == "U2"
    assert workspace_id({"team": {"id": "T1"}}) == "T1"
    assert workspace_id({"team_id": "T2"}) == "T2"


def test_action_parser_uses_container_thread_fallback() -> None:
    action = parse_action(
        "assistant.clear",
        {
            "channel": {"id": "C1"},
            "message": {"ts": "2"},
            "container": {"thread_ts": "1"},
        },
    )
    assert action is not None
    assert (action.channel, action.thread_ts, action.message_ts) == ("C1", "1", "2")


def test_action_parser_preserves_opaque_mcp_approval_value() -> None:
    action = parse_action(
        "mcp.approve",
        {
            "channel": {"id": "C1"},
            "message": {"ts": "2", "thread_ts": "1"},
            "actions": [{"action_id": "mcp.approve", "value": "opaque+/=_-token"}],
        },
    )

    assert action is not None
    assert action.action_id == "mcp.approve"
    assert action.value == "opaque+/=_-token"


def test_action_parser_defaults_missing_or_non_string_value_to_empty() -> None:
    base = {
        "channel": {"id": "C1"},
        "message": {"ts": "2"},
    }

    missing = parse_action("mcp.deny", base)
    invalid = parse_action("mcp.deny", {**base, "actions": [{"value": {"secret": True}}]})

    assert missing is not None and missing.value == ""
    assert invalid is not None and invalid.value == ""


def test_payload_parsers_reject_partial_shapes() -> None:
    assert parse_action("assistant.clear", {"channel": {"id": "C1"}}) is None
    assert parse_view({"view": "invalid"}) is None
    assert parse_view({"view": {"id": "V1"}}) == {"id": "V1"}
