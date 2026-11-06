import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from slack_agent_runtime.coding.tools import CodingToolError, WorkspaceTools
from slack_agent_runtime.coding.worker import CodingWorker
from slack_agent_runtime.inference.client import (
    InferenceResult,
    InferenceToolCall,
    InferenceUsage,
)


def _result(
    content: str,
    calls: tuple[InferenceToolCall, ...] = (),
) -> InferenceResult:
    return InferenceResult(content, "model", None, 1, InferenceUsage(), calls)


def _repository(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "README.md").write_text("# Project\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "initial",
        ],
        cwd=tmp_path,
        check=True,
    )
    return tmp_path


@pytest.mark.asyncio
async def test_worker_runs_tool_loop_and_returns_sanitized_metadata(tmp_path: Path) -> None:
    client = AsyncMock()
    client.complete.side_effect = [
        _result(
            "",
            (
                InferenceToolCall(
                    "call-1",
                    "write_file",
                    {"path": "feature.py", "content": "VALUE = 1\n"},
                ),
            ),
        ),
        _result("Add feature support\n\nCompleted safely. Token: ghp_abcdefghijklmnop"),
    ]

    result = await CodingWorker(client, WorkspaceTools(_repository(tmp_path))).run(
        "Add feature support",
        model="model",
    )

    assert result.changed_files == ("feature.py",)
    assert result.pr_title == "Add feature support"
    assert "ghp_" not in result.pr_body
    second_messages = client.complete.await_args_list[1].kwargs["messages"]
    assert second_messages[-1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "File written.",
    }


@pytest.mark.asyncio
async def test_worker_warns_model_to_finalize_near_step_limit(tmp_path: Path) -> None:
    client = AsyncMock()
    client.complete.side_effect = [
        _result(
            "",
            (
                InferenceToolCall(
                    "call-1",
                    "write_file",
                    {"path": "feature.py", "content": "VALUE = 1\n"},
                ),
            ),
        ),
        _result("Finish dependency update"),
    ]

    await CodingWorker(
        client,
        WorkspaceTools(_repository(tmp_path)),
        max_steps=8,
    ).run("Update dependencies", model="model")

    messages = client.complete.await_args_list[0].kwargs["messages"]
    assert any(
        message["role"] == "system" and "Eight model/tool rounds remain" in message["content"]
        for message in messages
    )


@pytest.mark.asyncio
async def test_worker_rejects_unknown_tools_and_step_exhaustion(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    unknown_client = AsyncMock()
    unknown_client.complete.return_value = _result(
        "",
        (InferenceToolCall("call", "shell", {}),),
    )
    with pytest.raises(CodingToolError, match="Unknown"):
        await CodingWorker(unknown_client, WorkspaceTools(root)).run("request", model="model")

    looping_client = AsyncMock()
    looping_client.complete.return_value = _result(
        "",
        (InferenceToolCall("call", "list_files", {"path": "."}),),
    )
    with pytest.raises(CodingToolError, match="step limit"):
        await CodingWorker(looping_client, WorkspaceTools(root), max_steps=1).run(
            "request",
            model="model",
        )


@pytest.mark.asyncio
async def test_worker_requires_a_non_empty_valid_diff(tmp_path: Path) -> None:
    client = AsyncMock()
    client.complete.return_value = _result("Done")

    with pytest.raises(CodingToolError, match="no changes"):
        await CodingWorker(client, WorkspaceTools(_repository(tmp_path))).run(
            "request",
            model="model",
        )
