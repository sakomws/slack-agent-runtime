"""Provider-neutral coding loop for an isolated GitHub Actions checkout."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from slack_agent_runtime.coding.tools import CodingToolError, WorkspaceTools

if TYPE_CHECKING:
    from slack_agent_runtime.inference.client import (
        InferenceClient,
        InferenceMessage,
        InferenceToolCall,
    )

_SECRET_LIKE = re.compile(
    r"(?:github_pat_|gh[pousr]_|xox[baprs]-|sk-[A-Za-z0-9_-]{12,})[A-Za-z0-9_-]*",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CodingWorkerResult:
    summary: str
    pr_title: str
    pr_body: str
    changed_files: tuple[str, ...]
    test_report: tuple[str, ...]


class CodingWorker:
    def __init__(
        self,
        inference_client: InferenceClient,
        tools: WorkspaceTools,
        *,
        max_steps: int = 48,
    ) -> None:
        self._client = inference_client
        self._tools = tools
        self._max_steps = max_steps

    async def run(self, prompt: str, *, model: str) -> CodingWorkerResult:
        messages: list[InferenceMessage] = [
            {
                "role": "system",
                "content": (
                    "You are a coding worker in an isolated repository checkout. "
                    "Implement the requested feature completely and run relevant tests. "
                    "Use only the provided tools. Never access secrets, .git, environment "
                    "files, or .github/workflows. Do not commit, push, open, approve, merge, "
                    "or close pull requests. Finish with a concise summary and test results."
                ),
            },
            {"role": "user", "content": prompt},
        ]
        for step in range(self._max_steps):
            remaining_steps = self._max_steps - step
            if remaining_steps == 8:
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "Eight model/tool rounds remain. Stop broad exploration, finish "
                            "the requested edits, run the highest-value remaining validation, "
                            "and then return the required final summary without tool calls."
                        ),
                    }
                )
            result = await self._client.complete(
                messages=messages,
                model=model,
                tools=self._tools.schemas,
            )
            if not result.tool_calls:
                changed = await self._tools.validate_diff()
                summary = _sanitize(result.content, limit=8_000)
                if not summary:
                    raise CodingToolError("The coding model returned no final summary")
                title = _pr_title(summary)
                tests = tuple(self._tools.test_report)
                body = _sanitize(
                    summary
                    + "\n\n## Validation\n"
                    + ("\n".join(f"- {item}" for item in tests) if tests else "- Not reported"),
                    limit=20_000,
                )
                return CodingWorkerResult(summary, title, body, changed, tests)
            messages.append(_assistant_message(result.content, result.tool_calls))
            for call in result.tool_calls:
                if call.name not in {
                    "list_files",
                    "search_text",
                    "read_file",
                    "write_file",
                    "delete_file",
                    "run_command",
                }:
                    raise CodingToolError(f"Unknown coding tool `{call.name}`")
                tool_result = await self._tools.execute(call.name, call.arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": tool_result.content,
                    }
                )
        raise CodingToolError("Coding worker step limit exceeded")


def _assistant_message(
    content: str,
    calls: tuple[InferenceToolCall, ...],
) -> InferenceMessage:
    return {
        "role": "assistant",
        "content": content or None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(
                        call.arguments,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            }
            for call in calls
        ],
    }


def _sanitize(value: str, *, limit: int) -> str:
    cleaned = "".join(
        character for character in value if character in "\n\t" or ord(character) >= 32
    )
    cleaned = _SECRET_LIKE.sub("[REDACTED]", cleaned).strip()
    return cleaned[:limit]


def _pr_title(summary: str) -> str:
    first_line = next((line.strip() for line in summary.splitlines() if line.strip()), "")
    first_line = first_line.removeprefix("#").strip()
    return (first_line or "Implement requested feature")[:120]
