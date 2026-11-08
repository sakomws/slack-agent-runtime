import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from slack_agent_runtime.coding.security_audit import SecurityAuditResult
from slack_agent_runtime.entrypoints import security_audit_worker


def _args(tmp_path: Path):
    target = tmp_path / "target"
    skill = tmp_path / "skill"
    target.mkdir()
    skill.mkdir()
    return security_audit_worker.build_parser().parse_args(
        [
            "--target",
            str(target),
            "--skill-dir",
            str(skill),
            "--output-dir",
            str(tmp_path / "output"),
            "--result-file",
            str(tmp_path / "result.json"),
            "--repository",
            "owner/repo",
            "--source-ref",
            "a" * 40,
            "--profile",
            "quick",
            "--scope",
            ".",
            "--max-agents",
            "8",
        ]
    )


@pytest.mark.asyncio
async def test_security_audit_entrypoint_runs_worker_and_writes_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _args(tmp_path)
    monkeypatch.setenv("INFERENCE_ROUTER_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("INFERENCE_ROUTER_API_KEY", "test-key")
    monkeypatch.setenv("INFERENCE_ROUTER_MODEL", "test-model")
    client = AsyncMock()
    client_factory = Mock(return_value=client)
    worker = Mock()
    worker.run = AsyncMock(return_value=SecurityAuditResult("done", ("REPORT.md",), 8))
    worker_factory = Mock(return_value=worker)
    monkeypatch.setattr(security_audit_worker, "InferenceRouterClient", client_factory)
    monkeypatch.setattr(security_audit_worker, "SecurityAuditWorker", worker_factory)

    await security_audit_worker.run(args)

    client_factory.assert_called_once_with(
        base_url="https://api.example.com/v1",
        api_key="test-key",
        model="test-model",
    )
    worker.run.assert_awaited_once_with(
        repository="owner/repo",
        source_ref="a" * 40,
        profile="quick",
        scope=".",
        model="test-model",
    )
    client.close.assert_awaited_once()
    assert json.loads(args.result_file.read_text(encoding="utf-8")) == {
        "summary": "done",
        "output_files": ["REPORT.md"],
        "agent_invocations": 8,
    }


@pytest.mark.asyncio
async def test_security_audit_entrypoint_rejects_output_inside_target(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path)
    args.output_dir = args.target / "audit"

    with pytest.raises(RuntimeError, match="outside the target"):
        await security_audit_worker.run(args)
