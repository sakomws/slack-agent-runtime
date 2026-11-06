from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from slack_agent_runtime.mcp.config import McpConfig, McpServerConfig
from slack_agent_runtime.mcp.manager import McpManager, McpTool, _Connection, _function_name


class FakeStack:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def _listed_tool(
    name: str,
    *,
    description: str | None = None,
    title: str | None = None,
    read_only: bool = False,
):
    annotations = SimpleNamespace(readOnlyHint=True) if read_only else None
    return SimpleNamespace(
        name=name,
        description=description,
        title=title,
        inputSchema={"type": "object"},
        annotations=annotations,
    )


def _call_result(*, text: str = "ok", is_error: bool = False):
    payload = {"content": [{"type": "text", "text": text}], "isError": is_error}
    return SimpleNamespace(
        isError=is_error,
        model_dump=lambda **_: payload,
    )


@pytest.mark.asyncio
async def test_stdio_connection_initializes_with_safe_environment_only(monkeypatch) -> None:
    monkeypatch.setenv("INHERITED", "yes")
    monkeypatch.setenv("PATH", "/safe/bin")
    session = AsyncMock()
    session.initialize = AsyncMock()
    seen = {}

    @asynccontextmanager
    async def fake_stdio(parameters):
        seen["parameters"] = parameters
        yield "read", "write"

    @asynccontextmanager
    async def fake_session(read, write, **kwargs):
        seen["session"] = (read, write, kwargs)
        yield session

    config = McpServerConfig(
        "local",
        "stdio",
        command="server",
        args=("--flag",),
        cwd="/tmp",
        env=(("EXTRA", "value"),),
    )
    manager = McpManager(McpConfig((config,)), tool_timeout_seconds=7)

    with (
        patch("slack_agent_runtime.mcp.manager.stdio_client", fake_stdio),
        patch("slack_agent_runtime.mcp.manager.ClientSession", fake_session),
    ):
        connection = await manager._connect(config)

    parameters = seen["parameters"]
    assert parameters.command == "server"
    assert parameters.args == ["--flag"]
    assert parameters.cwd == "/tmp"
    assert parameters.env["PATH"] == "/safe/bin"
    assert "INHERITED" not in parameters.env
    assert parameters.env["EXTRA"] == "value"
    assert seen["session"][0:2] == ("read", "write")
    assert seen["session"][2]["read_timeout_seconds"].total_seconds() == 7
    session.initialize.assert_awaited_once_with()
    await connection.stack.aclose()


@pytest.mark.asyncio
async def test_streamable_http_connection_passes_headers_and_shared_client() -> None:
    session = AsyncMock()
    seen = {}

    class FakeHttpClient:
        def __init__(self, **kwargs) -> None:
            seen["http_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    @asynccontextmanager
    async def fake_streamable(url, *, http_client):
        seen["streamable"] = (url, http_client)
        yield "read", "write", "session-id"

    @asynccontextmanager
    async def fake_session(*_, **__):
        yield session

    config = McpServerConfig(
        "remote",
        "streamable-http",
        url="https://mcp.example.test/rpc",
        headers=(("Authorization", "Bearer token"),),
    )
    manager = McpManager(McpConfig((config,)))

    with (
        patch("slack_agent_runtime.mcp.manager.httpx.AsyncClient", FakeHttpClient),
        patch("slack_agent_runtime.mcp.manager.streamable_http_client", fake_streamable),
        patch("slack_agent_runtime.mcp.manager.ClientSession", fake_session),
    ):
        connection = await manager._connect(config)

    assert seen["http_kwargs"] == {
        "headers": {"Authorization": "Bearer token"},
        "timeout": None,
    }
    assert seen["streamable"][0] == "https://mcp.example.test/rpc"
    assert seen["streamable"][1] is not None
    session.initialize.assert_awaited_once_with()
    await connection.stack.aclose()


@pytest.mark.asyncio
async def test_discovery_paginates_namespaces_and_only_auto_approves_read_only_tools() -> None:
    session = AsyncMock()
    session.list_tools.side_effect = [
        SimpleNamespace(
            tools=[
                _listed_tool("read.file", description="Read it", read_only=True),
                _listed_tool("write", title="Write it"),
            ],
            nextCursor="next",
        ),
        SimpleNamespace(
            tools=[_listed_tool("plain", read_only=True)],
            nextCursor=None,
        ),
    ]
    config = McpServerConfig(
        "server-one",
        "stdio",
        command="x",
        auto_approve=frozenset({"read.file", "write"}),
    )
    manager = McpManager(McpConfig((config,)))

    await manager._discover(_Connection(config, FakeStack(), session))

    assert session.list_tools.await_args_list[0].kwargs == {"cursor": None}
    assert session.list_tools.await_args_list[1].kwargs == {"cursor": "next"}
    assert [tool.function_name for tool in manager.tools] == [
        "mcp__server-one__plain",
        "mcp__server-one__read_file",
        "mcp__server-one__write",
    ]
    by_name = {tool.tool_name: tool for tool in manager.tools}
    assert by_name["read.file"].description == "Read it"
    assert by_name["read.file"].auto_approved is True
    assert by_name["write"].description == "Write it"
    assert by_name["write"].auto_approved is False
    assert by_name["plain"].description == "plain"
    assert by_name["plain"].auto_approved is False


@pytest.mark.asyncio
async def test_discovery_rejects_sanitized_name_collisions() -> None:
    session = AsyncMock()
    session.list_tools.return_value = SimpleNamespace(
        tools=[_listed_tool("same.name"), _listed_tool("same/name")],
        nextCursor=None,
    )
    config = McpServerConfig("server", "stdio", command="x")
    manager = McpManager(McpConfig((config,)))

    with pytest.raises(RuntimeError, match="tool name collision"):
        await manager._discover(_Connection(config, FakeStack(), session))

    assert manager.tools == ()


def test_function_names_are_stable_and_bounded() -> None:
    first = _function_name("server", "a" * 100)
    second = _function_name("server", "a" * 100)

    assert first == second
    assert len(first) == 64
    assert first.startswith("mcp__server__")
    assert _function_name("server", "a.b/c") == "mcp__server__a_b_c"


@pytest.mark.asyncio
async def test_result_is_bounded_and_preserves_error_flag() -> None:
    manager = McpManager(McpConfig(), max_result_chars=50)

    result = manager._bounded_result(_call_result(text="x" * 100, is_error=True))

    assert result.is_error is True
    assert result.content.endswith("\n[tool result truncated]")
    assert len(result.content) == 50


@pytest.mark.asyncio
async def test_read_only_call_reconnects_once_and_retries() -> None:
    config = McpServerConfig("server", "stdio", command="x")
    tool = McpTool(
        "mcp__server__read",
        "server",
        "read",
        "Read",
        {},
        read_only=True,
        auto_approved=True,
    )
    old_session = AsyncMock()
    old_session.call_tool.side_effect = ConnectionError("lost")
    new_session = AsyncMock()
    new_session.call_tool.return_value = _call_result(text="recovered")
    old = _Connection(config, FakeStack(), old_session)
    new = _Connection(config, FakeStack(), new_session)
    manager = McpManager(McpConfig((config,)))
    manager._connections["server"] = old
    manager._tools[tool.function_name] = tool
    manager._connect = AsyncMock(return_value=new)
    manager._discover = AsyncMock()

    result = await manager.call_tool(tool.function_name, {"q": "value"})

    assert "recovered" in result.content
    assert old.stack.closed
    new_session.call_tool.assert_awaited_once_with("read", {"q": "value"})
    manager._discover.assert_awaited_once_with(new)


@pytest.mark.asyncio
async def test_mutating_call_reconnects_but_is_never_retried() -> None:
    config = McpServerConfig("server", "stdio", command="x")
    tool = McpTool("mcp__server__write", "server", "write", "Write", {}, False, False)
    old_session = AsyncMock()
    old_session.call_tool.side_effect = ConnectionError("uncertain outcome")
    new_session = AsyncMock()
    old = _Connection(config, FakeStack(), old_session)
    new = _Connection(config, FakeStack(), new_session)
    manager = McpManager(McpConfig((config,)))
    manager._connections["server"] = old
    manager._tools[tool.function_name] = tool
    manager._connect = AsyncMock(return_value=new)
    manager._discover = AsyncMock()

    with pytest.raises(RuntimeError, match="it was not retried"):
        await manager.call_tool(tool.function_name, {"dangerous": True})

    new_session.call_tool.assert_not_awaited()
    assert manager._connections["server"] is new


@pytest.mark.asyncio
async def test_start_failure_closes_already_open_connections() -> None:
    first_config = McpServerConfig("first", "stdio", command="x")
    second_config = McpServerConfig("second", "stdio", command="x")
    first = _Connection(first_config, FakeStack(), AsyncMock())
    manager = McpManager(McpConfig((first_config, second_config)))
    manager._connect = AsyncMock(side_effect=[first, ConnectionError("cannot connect")])
    manager._discover = AsyncMock()

    with pytest.raises(ConnectionError, match="cannot connect"):
        await manager.start()

    assert first.stack.closed
    assert manager.tools == ()
    assert manager._started is False
