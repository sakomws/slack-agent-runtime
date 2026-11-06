"""Strict, credential-safe configuration for direct MCP connections."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

_ENV_REFERENCE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
_SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    name: str
    transport: Literal["stdio", "streamable-http"]
    command: str | None = None
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env: tuple[tuple[str, str], ...] = ()
    url: str | None = None
    headers: tuple[tuple[str, str], ...] = ()
    auto_approve: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class McpConfig:
    servers: tuple[McpServerConfig, ...] = ()


def load_mcp_config(path: str) -> McpConfig:
    """Load a JSON config, expanding only explicit environment references."""

    if not path:
        return McpConfig()
    config_path = Path(path).expanduser()
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Could not read MCP config: {config_path}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"MCP config is not valid JSON: {config_path}") from exc
    if not isinstance(payload, dict) or set(payload) != {"mcpServers"}:
        raise RuntimeError("MCP config must contain only an `mcpServers` object")
    servers_payload = payload["mcpServers"]
    if not isinstance(servers_payload, dict):
        raise RuntimeError("MCP config `mcpServers` must be an object")
    return McpConfig(tuple(_parse_server(name, value) for name, value in servers_payload.items()))


def _parse_server(name: object, value: object) -> McpServerConfig:
    if not isinstance(name, str) or not _SERVER_NAME.fullmatch(name):
        raise RuntimeError("MCP server names may contain only letters, numbers, `_`, and `-`")
    if not isinstance(value, dict):
        raise RuntimeError(f"MCP server `{name}` must be an object")
    transport = value.get("transport")
    if transport == "stdio":
        allowed = {"transport", "command", "args", "cwd", "env", "autoApprove"}
        _reject_unknown(name, value, allowed)
        command = _expanded_string(value.get("command"), f"{name}.command")
        if not command:
            raise RuntimeError(f"MCP server `{name}` requires `command`")
        return McpServerConfig(
            name=name,
            transport="stdio",
            command=command,
            args=_string_tuple(value.get("args", []), f"{name}.args"),
            cwd=_optional_string(value.get("cwd"), f"{name}.cwd"),
            env=_string_mapping(value.get("env", {}), f"{name}.env"),
            auto_approve=frozenset(
                _string_tuple(value.get("autoApprove", []), f"{name}.autoApprove")
            ),
        )
    if transport == "streamable-http":
        allowed = {"transport", "url", "headers", "autoApprove"}
        _reject_unknown(name, value, allowed)
        url = _expanded_string(value.get("url"), f"{name}.url")
        if not url or not url.startswith(("http://", "https://")):
            raise RuntimeError(f"MCP server `{name}` requires an HTTP(S) `url`")
        return McpServerConfig(
            name=name,
            transport="streamable-http",
            url=url,
            headers=_string_mapping(value.get("headers", {}), f"{name}.headers"),
            auto_approve=frozenset(
                _string_tuple(value.get("autoApprove", []), f"{name}.autoApprove")
            ),
        )
    raise RuntimeError(f"MCP server `{name}` transport must be `stdio` or `streamable-http`")


def _reject_unknown(name: str, value: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise RuntimeError(
            f"MCP server `{name}` has unknown settings: {', '.join(sorted(unknown))}"
        )


def _expand(value: str, field: str) -> str:
    def replace(match: re.Match[str]) -> str:
        variable = match.group(1)
        replacement = os.getenv(variable)
        if replacement is None:
            raise RuntimeError(f"MCP config `{field}` references unset variable `{variable}`")
        return replacement

    return _ENV_REFERENCE.sub(replace, value)


def _expanded_string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"MCP config `{field}` must be a string")
    return _expand(value, field)


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    expanded = _expanded_string(value, field)
    return expanded or None


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuntimeError(f"MCP config `{field}` must be an array of strings")
    return tuple(_expand(item, field) for item in value)


def _string_mapping(value: object, field: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise RuntimeError(f"MCP config `{field}` must be an object of strings")
    return tuple((key, _expand(item, f"{field}.{key}")) for key, item in value.items())
