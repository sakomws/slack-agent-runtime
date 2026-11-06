import os
import subprocess
from pathlib import Path

import pytest

from slack_agent_runtime.coding.tools import CodingToolError, WorkspaceTools


def _repository(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "tracked.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
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


def _commit_deleted_file(root: Path) -> None:
    (root / "deleted.txt").write_text("delete\n", encoding="utf-8")
    subprocess.run(["git", "add", "deleted.txt"], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "add deletion",
        ],
        cwd=root,
        check=True,
    )
    os.remove(root / "deleted.txt")


def test_paths_are_confined_and_protected(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    tools = WorkspaceTools(root)

    with pytest.raises(CodingToolError, match="outside"):
        tools.read_file("../outside.txt")
    with pytest.raises(CodingToolError, match="outside"):
        tools.write_file(".github/workflows/attack.yml", "bad")
    with pytest.raises(CodingToolError, match="outside"):
        tools.write_file(".env", "SECRET=value")
    with pytest.raises(CodingToolError, match="outside"):
        tools.write_file("private.pem", "key")


def test_symlink_escape_and_file_limit_are_rejected(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "private.txt").write_text("do-not-read", encoding="utf-8")
    (root / "escape").symlink_to(outside, target_is_directory=True)
    tools = WorkspaceTools(root, max_file_chars=4)

    with pytest.raises(CodingToolError, match="outside"):
        tools.write_file("escape/file.txt", "x")
    assert "do-not-read" not in tools.search_text("do-not-read", ".")
    with pytest.raises(CodingToolError, match="size"):
        tools.write_file("large.txt", "12345")


@pytest.mark.asyncio
async def test_commands_are_allowlisted_bounded_and_redact_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path)
    tools = WorkspaceTools(root, max_commands=1, max_output_chars=20)
    monkeypatch.setenv("SHOULD_NOT_LEAK", "secret-value")

    with pytest.raises(CodingToolError, match="not allowed"):
        await tools.run_command(["sh", "-c", "echo bad"])
    with pytest.raises(CodingToolError, match="read-only"):
        await tools.run_command(["git", "commit"])
    output = await tools.run_command(
        ["python3", "-c", "import os; print(os.getenv('SHOULD_NOT_LEAK'))"]
    )
    assert "secret-value" not in output
    with pytest.raises(CodingToolError, match="limit"):
        await tools.run_command(["python3", "-c", "print('again')"])


@pytest.mark.asyncio
async def test_command_timeout_and_output_truncation(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    timeout_tools = WorkspaceTools(root, command_timeout_seconds=0.01)
    with pytest.raises(CodingToolError, match="timed out"):
        await timeout_tools.run_command(["python3", "-c", "import time; time.sleep(1)"])

    output_tools = WorkspaceTools(root, max_output_chars=30)
    output = await output_tools.run_command(["python3", "-c", "print('x' * 100)"])
    assert "truncated" in output


@pytest.mark.asyncio
async def test_diff_validation_includes_modified_untracked_and_deleted_files(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    (root / "tracked.txt").write_text("after\n", encoding="utf-8")
    (root / "new.txt").write_text("new\n", encoding="utf-8")
    _commit_deleted_file(root)

    changed = await WorkspaceTools(root).validate_diff()

    assert changed == ("deleted.txt", "new.txt", "tracked.txt")
