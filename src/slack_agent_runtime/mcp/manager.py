"""MCP session lifecycle, discovery, and bounded tool execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

if TYPE_CHECKING:
    from slack_agent_runtime.mcp.config import McpConfig, McpServerConfig

logger = logging.getLogger(__name__)
_FUNCTION_CHARACTER = re.compile(r"[^A-Za-z0-9_-]")
_MAX_TOOLS = 256
_MAX_SCHEMA_CHARS = 64_000


@dataclass(frozen=True, slots=True)
class McpTool:
    function_name: str
    server_name: str
    tool_name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    auto_approved: bool

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.function_name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


@dataclass(frozen=True, slots=True)
class McpToolResult:
    content: str
    is_error: bool


@dataclass(slots=True)
class _Connection:
    config: McpServerConfig
    stack: AsyncExitStack
    session: ClientSession
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class McpManager:
    """Own direct MCP connections and expose a stable model-facing tool registry."""

    def __init__(
        self,
        config: McpConfig,
        *,
        tool_timeout_seconds: float = 30.0,
        max_result_chars: int = 20_000,
    ) -> None:
        self._config = config
        self._tool_timeout_seconds = tool_timeout_seconds
        self._max_result_chars = max_result_chars
        self._connections: dict[str, _Connection] = {}
        self._tools: dict[str, McpTool] = {}
        self._started = False

    @property
    def enabled(self) -> bool:
        return bool(self._config.servers)

    @property
    def tools(self) -> tuple[McpTool, ...]:
        return tuple(self._tools[name] for name in sorted(self._tools))

    @property
    def server_names(self) -> tuple[str, ...]:
        return tuple(server.name for server in self._config.servers)

    async def start(self) -> None:
        if self._started:
            return
        try:
            for server in self._config.servers:
                async with asyncio.timeout(self._tool_timeout_seconds):
                    connection = await self._connect(server)
                    self._connections[server.name] = connection
                    await self._discover(connection)
        except BaseException:
            await self.close()
            raise
        self._started = True

    async def close(self) -> None:
        connections = tuple(reversed(self._connections.values()))
        self._connections.clear()
        self._tools.clear()
        self._started = False
        for connection in connections:
            try:
                await connection.stack.aclose()
            except Exception:
                logger.exception(
                    "Failed to close MCP server",
                    extra={"server": connection.config.name},
                )

    async def call_tool(
        self,
        function_name: str,
        arguments: dict[str, Any],
    ) -> McpToolResult:
        tool = self._tools.get(function_name)
        if tool is None:
            raise RuntimeError(f"Unknown MCP tool `{function_name}`")
        for attempt in range(2):
            connection = self._connections.get(tool.server_name)
            if connection is None:
                raise RuntimeError(f"MCP server `{tool.server_name}` is unavailable")
            try:
                async with connection.lock, asyncio.timeout(self._tool_timeout_seconds):
                    result = await connection.session.call_tool(tool.tool_name, arguments)
                return self._bounded_result(result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if attempt:
                    raise RuntimeError(
                        f"MCP tool `{tool.function_name}` failed after reconnect"
                    ) from exc
                logger.warning(
                    "MCP tool call failed; reconnecting once",
                    extra={"server": tool.server_name, "tool": tool.tool_name},
                )
                await self._reconnect(tool.server_name)
                if not tool.read_only:
                    raise RuntimeError(
                        f"MCP tool `{tool.function_name}` failed; it was not retried"
                    ) from exc
        raise AssertionError("unreachable")

    async def _connect(self, config: McpServerConfig) -> _Connection:
        stack = AsyncExitStack()
        try:
            if config.transport == "stdio":
                parameters = StdioServerParameters(
                    command=config.command or "",
                    args=list(config.args),
                    cwd=config.cwd,
                    env={**_safe_process_environment(), **dict(config.env)},
                )
                read_stream, write_stream = await stack.enter_async_context(
                    stdio_client(parameters)
                )
            else:
                http_client = await stack.enter_async_context(
                    httpx.AsyncClient(headers=dict(config.headers), timeout=None)
                )
                read_stream, write_stream, _ = await stack.enter_async_context(
                    streamable_http_client(config.url or "", http_client=http_client)
                )
            session = await stack.enter_async_context(
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=self._tool_timeout_seconds),
                )
            )
            await session.initialize()
            return _Connection(config, stack, session)
        except BaseException:
            await stack.aclose()
            raise

    async def _discover(self, connection: _Connection) -> None:
        cursor: str | None = None
        discovered: list[McpTool] = []
        discovered_names: set[str] = set()
        other_names = {
            name for name, tool in self._tools.items() if tool.server_name != connection.config.name
        }
        while True:
            response = await connection.session.list_tools(cursor=cursor)
            for item in response.tools:
                if not item.name:
                    raise RuntimeError(
                        f"MCP server `{connection.config.name}` returned an unnamed tool"
                    )
                if len(discovered) + len(other_names) >= _MAX_TOOLS:
                    raise RuntimeError(f"MCP tool limit of {_MAX_TOOLS} was exceeded")
                function_name = _function_name(connection.config.name, item.name)
                if function_name in discovered_names:
                    raise RuntimeError(f"MCP tool name collision: `{function_name}`")
                discovered_names.add(function_name)
                annotations = item.annotations
                read_only = bool(annotations and annotations.readOnlyHint is True)
                input_schema = dict(item.inputSchema)
                if len(json.dumps(input_schema, separators=(",", ":"))) > _MAX_SCHEMA_CHARS:
                    raise RuntimeError(f"MCP tool `{function_name}` has an oversized schema")
                discovered.append(
                    McpTool(
                        function_name=function_name,
                        server_name=connection.config.name,
                        tool_name=item.name,
                        description=(item.description or item.title or item.name)[:1024],
                        input_schema=input_schema,
                        read_only=read_only,
                        auto_approved=(read_only and item.name in connection.config.auto_approve),
                    )
                )
            cursor = response.nextCursor
            if not cursor:
                break
        for tool in discovered:
            if tool.function_name in other_names:
                raise RuntimeError(f"MCP tool name collision: `{tool.function_name}`")
        self._tools = {
            name: tool
            for name, tool in self._tools.items()
            if tool.server_name != connection.config.name
        }
        self._tools.update((tool.function_name, tool) for tool in discovered)

    async def _reconnect(self, server_name: str) -> None:
        current = self._connections.pop(server_name)
        await current.stack.aclose()
        async with asyncio.timeout(self._tool_timeout_seconds):
            connection = await self._connect(current.config)
            self._connections[server_name] = connection
            await self._discover(connection)

    def _bounded_result(self, result: Any) -> McpToolResult:
        payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(content) > self._max_result_chars:
            suffix = "\n[tool result truncated]"
            if self._max_result_chars <= len(suffix):
                content = suffix[: self._max_result_chars]
            else:
                content = content[: self._max_result_chars - len(suffix)] + suffix
        return McpToolResult(content=content, is_error=bool(result.isError))


def _function_name(server_name: str, tool_name: str) -> str:
    raw = f"mcp__{server_name}__{tool_name}"
    safe = _FUNCTION_CHARACTER.sub("_", raw)
    if len(safe) <= 64:
        return safe
    digest = hashlib.sha256(raw.encode()).hexdigest()[:10]
    return f"{safe[:53]}_{digest}"


def _safe_process_environment() -> dict[str, str]:
    inherited = ("PATH", "HOME", "USER", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT")
    return {name: os.environ[name] for name in inherited if name in os.environ}
