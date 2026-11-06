"""Workspace-bounded tools exposed to the coding model."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PROTECTED_PARTS = {".git", ".env", ".secrets", "secrets"}
_PROTECTED_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
_ALLOWED_EXECUTABLES = {
    "cargo",
    "go",
    "make",
    "mypy",
    "npm",
    "pnpm",
    "pytest",
    "python",
    "python3",
    "ruff",
    "uv",
    "yarn",
    "git",
}
_GIT_SUBCOMMANDS = {"diff", "status", "ls-files"}


class CodingToolError(RuntimeError):
    """Safe local-tool boundary failure."""


@dataclass(frozen=True, slots=True)
class LocalToolResult:
    content: str
    is_error: bool = False


class WorkspaceTools:
    def __init__(
        self,
        root: Path,
        *,
        command_timeout_seconds: float = 300,
        max_output_chars: int = 20_000,
        max_file_chars: int = 200_000,
        max_changed_files: int = 50,
        max_diff_chars: int = 500_000,
        max_commands: int = 20,
    ) -> None:
        self.root = root.resolve(strict=True)
        self.command_timeout_seconds = command_timeout_seconds
        self.max_output_chars = max_output_chars
        self.max_file_chars = max_file_chars
        self.max_changed_files = max_changed_files
        self.max_diff_chars = max_diff_chars
        self.max_commands = max_commands
        self.command_count = 0
        self.test_report: list[str] = []

    @property
    def schemas(self) -> tuple[dict[str, Any], ...]:
        return (
            _schema("list_files", "List repository files", {"path": {"type": "string"}}),
            _schema(
                "search_text",
                "Search text in repository files",
                {
                    "query": {"type": "string"},
                    "path": {"type": "string"},
                },
                required=("query",),
            ),
            _schema("read_file", "Read a UTF-8 repository file", {"path": {"type": "string"}}),
            _schema(
                "write_file",
                "Create or replace a UTF-8 repository file",
                {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                required=("path", "content"),
            ),
            _schema("delete_file", "Delete a repository file", {"path": {"type": "string"}}),
            _schema(
                "run_command",
                "Run one allowlisted command without a shell",
                {"args": {"type": "array", "items": {"type": "string"}}},
                required=("args",),
            ),
        )

    async def execute(self, name: str, arguments: dict[str, Any]) -> LocalToolResult:
        try:
            if name == "list_files":
                content = self.list_files(_string(arguments, "path", default="."))
                return LocalToolResult(_bounded(content, self.max_output_chars))
            if name == "search_text":
                content = self.search_text(
                    _string(arguments, "query"),
                    _string(arguments, "path", default="."),
                )
                return LocalToolResult(_bounded(content, self.max_output_chars))
            if name == "read_file":
                content = self.read_file(_string(arguments, "path"))
                return LocalToolResult(_bounded(content, self.max_output_chars))
            if name == "write_file":
                self.write_file(
                    _string(arguments, "path"),
                    _string(arguments, "content"),
                )
                return LocalToolResult("File written.")
            if name == "delete_file":
                self.delete_file(_string(arguments, "path"))
                return LocalToolResult("File deleted.")
            if name == "run_command":
                args = arguments.get("args")
                if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
                    raise CodingToolError("`args` must be an array of strings")
                return LocalToolResult(await self.run_command(args))
            raise CodingToolError(f"Unknown coding tool `{name}`")
        except CodingToolError as exc:
            return LocalToolResult(str(exc), True)

    def list_files(self, relative: str) -> str:
        base = self._path(relative, allow_directory=True)
        if not base.is_dir():
            raise CodingToolError("List path is not a directory")
        files = [
            str(path.relative_to(self.root))
            for path in base.rglob("*")
            if path.is_file() and self._is_allowed_existing(path)
        ][:1000]
        return "\n".join(sorted(files)) or "(no files)"

    def search_text(self, query: str, relative: str) -> str:
        if not query or len(query) > 500:
            raise CodingToolError("Search query is invalid")
        base = self._path(relative, allow_directory=True)
        matches: list[str] = []
        paths = (base,) if base.is_file() else base.rglob("*")
        for path in paths:
            if not path.is_file() or not self._is_allowed_existing(path):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if query in line:
                    matches.append(f"{path.relative_to(self.root)}:{number}:{line[:500]}")
                    if len(matches) >= 500:
                        return "\n".join(matches)
        return "\n".join(matches) or "(no matches)"

    def read_file(self, relative: str) -> str:
        path = self._path(relative)
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise CodingToolError("File is missing or is not UTF-8 text") from exc
        if len(content) > self.max_file_chars:
            raise CodingToolError("File exceeds the configured size limit")
        return content

    def write_file(self, relative: str, content: str) -> None:
        if len(content) > self.max_file_chars:
            raise CodingToolError("File exceeds the configured size limit")
        path = self._path(relative, must_exist=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def delete_file(self, relative: str) -> None:
        path = self._path(relative)
        if not path.is_file():
            raise CodingToolError("Delete path is not a file")
        path.unlink()

    async def run_command(self, args: list[str], *, count: bool = True) -> str:
        if not args or len(args) > 64 or any(len(item) > 1000 for item in args):
            raise CodingToolError("Command arguments are invalid")
        executable = Path(args[0]).name
        if args[0] != executable or executable not in _ALLOWED_EXECUTABLES:
            raise CodingToolError(f"Executable `{executable}` is not allowed")
        if executable == "git" and (len(args) < 2 or args[1] not in _GIT_SUBCOMMANDS):
            raise CodingToolError("Only read-only git commands are allowed")
        if count and self.command_count >= self.max_commands:
            raise CodingToolError("Command limit exceeded")
        if count:
            self.command_count += 1
        environment = {
            name: os.environ[name]
            for name in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "CI")
            if name in os.environ
        }
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=self.root,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            async with asyncio.timeout(self.command_timeout_seconds):
                output, _ = await process.communicate()
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise CodingToolError("Command timed out") from exc
        text = output.decode("utf-8", errors="replace")
        bounded = _bounded(text, self.max_output_chars)
        if count:
            status = "passed" if process.returncode == 0 else f"failed ({process.returncode})"
            self.test_report.append(f"`{' '.join(args[:8])}`: {status}")
        return f"exit_code={process.returncode}\n{bounded}"

    async def validate_diff(self) -> tuple[str, ...]:
        visible = await self.run_command(
            ["git", "ls-files", "--modified", "--others", "--exclude-standard"],
            count=False,
        )
        deleted = await self.run_command(
            ["git", "diff", "--name-only", "--diff-filter=D"],
            count=False,
        )
        names = {
            line.strip()
            for output in (visible, deleted)
            for line in output.partition("\n")[2].splitlines()
            if line.strip()
        }
        changed = tuple(sorted(names))
        if not changed:
            raise CodingToolError("The coding job produced no changes")
        if len(changed) > self.max_changed_files:
            raise CodingToolError("Changed-file limit exceeded")
        for name in changed:
            self._path(name, must_exist=False)
        diff = await self.run_command(["git", "diff", "--no-ext-diff"], count=False)
        untracked_chars = sum(
            path.stat().st_size for name in changed if (path := self.root / name).is_file()
        )
        if len(diff) + untracked_chars > self.max_diff_chars:
            raise CodingToolError("Diff size limit exceeded")
        return changed

    def _path(
        self,
        relative: str,
        *,
        must_exist: bool = True,
        allow_directory: bool = False,
    ) -> Path:
        if not relative or "\x00" in relative:
            raise CodingToolError("Path is invalid")
        candidate = (self.root / relative).resolve(strict=False)
        if not candidate.is_relative_to(self.root) or self._is_protected(candidate):
            raise CodingToolError("Path is outside the allowed workspace")
        if must_exist and not candidate.exists():
            raise CodingToolError("Path does not exist")
        if not allow_directory and candidate.exists() and candidate.is_dir():
            raise CodingToolError("Path is a directory")
        return candidate

    def _is_protected(self, path: Path) -> bool:
        relative = path.relative_to(self.root)
        parts = set(relative.parts)
        if (
            parts & _PROTECTED_PARTS
            or any(part.startswith(".env") for part in parts)
            or path.suffix.lower() in _PROTECTED_SUFFIXES
        ):
            return True
        return len(relative.parts) >= 2 and relative.parts[:2] == (".github", "workflows")

    def _is_allowed_existing(self, path: Path) -> bool:
        resolved = path.resolve(strict=False)
        return resolved.is_relative_to(self.root) and not self._is_protected(resolved)


def _schema(
    name: str,
    description: str,
    properties: dict[str, Any],
    *,
    required: tuple[str, ...] = ("path",),
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(required),
                "additionalProperties": False,
            },
        },
    }


def _string(arguments: dict[str, Any], name: str, *, default: str | None = None) -> str:
    value = arguments.get(name, default)
    if not isinstance(value, str):
        raise CodingToolError(f"`{name}` must be a string")
    return value


def _bounded(value: str, limit: int) -> str:
    suffix = "\n[output truncated]"
    if len(value) <= limit:
        return value
    if limit <= len(suffix):
        return suffix[:limit]
    return value[: limit - len(suffix)] + suffix
