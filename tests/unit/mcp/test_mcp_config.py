import json

import pytest

from slack_agent_runtime.mcp.config import McpConfig, load_mcp_config


def _write_config(tmp_path, payload: object) -> str:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_empty_path_disables_mcp() -> None:
    assert load_mcp_config("") == McpConfig()


def test_loads_both_transports_and_expands_only_explicit_environment_references(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("MCP_TOKEN", "secret-token")
    monkeypatch.setenv("MCP_ROOT", "/workspace")
    path = _write_config(
        tmp_path,
        {
            "mcpServers": {
                "local_server": {
                    "transport": "stdio",
                    "command": "${MCP_ROOT}/bin/server",
                    "args": ["--token=${MCP_TOKEN}", "$HOME", "${lowercase}"],
                    "cwd": "${MCP_ROOT}",
                    "env": {"TOKEN": "Bearer ${MCP_TOKEN}"},
                    "autoApprove": ["read"],
                },
                "remote-server": {
                    "transport": "streamable-http",
                    "url": "https://example.test/${MCP_TOKEN}",
                    "headers": {"Authorization": "Bearer ${MCP_TOKEN}"},
                    "autoApprove": ["lookup"],
                },
            }
        },
    )

    config = load_mcp_config(path)

    local, remote = config.servers
    assert local.command == "/workspace/bin/server"
    assert local.args == ("--token=secret-token", "$HOME", "${lowercase}")
    assert local.cwd == "/workspace"
    assert local.env == (("TOKEN", "Bearer secret-token"),)
    assert local.auto_approve == frozenset({"read"})
    assert remote.url == "https://example.test/secret-token"
    assert remote.headers == (("Authorization", "Bearer secret-token"),)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "contain only"),
        ({"mcpServers": {}, "extra": True}, "contain only"),
        ({"mcpServers": []}, "must be an object"),
        ({"mcpServers": {"bad name": {}}}, "server names"),
        (
            {"mcpServers": {"one": {"transport": "stdio", "command": "x", "extra": 1}}},
            "unknown settings: extra",
        ),
        (
            {"mcpServers": {"one": {"transport": "stdio", "command": 1}}},
            "one.command.*must be a string",
        ),
        (
            {"mcpServers": {"one": {"transport": "stdio", "command": "x", "args": "bad"}}},
            "one.args.*array of strings",
        ),
        (
            {"mcpServers": {"one": {"transport": "stdio", "command": "x", "env": {"A": 1}}}},
            "one.env.*object of strings",
        ),
        (
            {"mcpServers": {"one": {"transport": "streamable-http", "url": "file:///tmp"}}},
            "requires an HTTP",
        ),
        (
            {"mcpServers": {"one": {"transport": "websocket", "url": "https://example.test"}}},
            "transport must be",
        ),
    ],
)
def test_rejects_non_strict_configuration(tmp_path, payload, message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        load_mcp_config(_write_config(tmp_path, payload))


def test_rejects_unset_environment_reference(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("MISSING_MCP_SECRET", raising=False)
    path = _write_config(
        tmp_path,
        {
            "mcpServers": {
                "remote": {
                    "transport": "streamable-http",
                    "url": "https://example.test",
                    "headers": {"Authorization": "Bearer ${MISSING_MCP_SECRET}"},
                }
            }
        },
    )

    with pytest.raises(RuntimeError, match="references unset variable `MISSING_MCP_SECRET`"):
        load_mcp_config(path)


def test_wraps_file_and_json_errors_without_exposing_parser_details(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="Could not read MCP config"):
        load_mcp_config(str(tmp_path / "missing.json"))

    path = tmp_path / "invalid.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not valid JSON"):
        load_mcp_config(str(path))
