import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from slack_agent_runtime.coding.security_audit import (
    SecurityAuditError,
    SecurityAuditWorker,
)
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


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    target = tmp_path / "target"
    skill = tmp_path / "skill"
    target.mkdir()
    skill.mkdir()
    (target / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (target / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (skill / "SKILL.md").write_text("# Security Audit\n", encoding="utf-8")
    return target, skill


def _outputs() -> dict[str, str]:
    return {
        "run-metadata.json": json.dumps({"run_status": "complete"}),
        "architecture.md": "# Architecture",
        "coverage-ledger.json": "[]",
        "findings.json": "[]",
        "REPORT.md": "# Report",
        "FINDINGS-DETAIL.md": "# Finding details",
        "NEEDS-VALIDATION.md": "# Needs validation",
    }


@pytest.mark.asyncio
async def test_security_audit_worker_writes_only_canonical_outputs(tmp_path: Path) -> None:
    target, skill = _roots(tmp_path)
    outputs = _outputs()
    calls = tuple(
        InferenceToolCall(
            f"call-{index}",
            "write_audit_file",
            {"path": name, "content": content},
        )
        for index, (name, content) in enumerate(outputs.items())
    )
    client = AsyncMock()
    client.complete.side_effect = [_result("", calls), _result("Audit complete")]

    result = await SecurityAuditWorker(
        client,
        target=target,
        skill_dir=skill,
        output_dir=tmp_path / "output",
    ).run(
        repository="owner/repo",
        source_ref="a" * 40,
        profile="quick",
        scope=".",
        model="model",
    )

    assert result.output_files == tuple(outputs)
    assert result.agent_invocations == 0
    assert (tmp_path / "output" / "REPORT.md").read_text(encoding="utf-8") == "# Report\n"


@pytest.mark.asyncio
async def test_security_audit_worker_forces_summary_after_finalization_budget(
    tmp_path: Path,
) -> None:
    target, skill = _roots(tmp_path)
    calls = tuple(
        InferenceToolCall(
            f"call-{index}",
            "write_audit_file",
            {"path": name, "content": content},
        )
        for index, (name, content) in enumerate(_outputs().items())
    )
    client = AsyncMock()
    client.complete.side_effect = [_result("", calls), _result("Bounded audit complete")]

    result = await SecurityAuditWorker(
        client,
        target=target,
        skill_dir=skill,
        output_dir=tmp_path / "output",
        max_steps=1,
    ).run(
        repository="owner/repo",
        source_ref="a" * 40,
        profile="quick",
        scope=".",
        model="model",
    )

    assert result.summary == "Bounded audit complete"
    first_tool_names = {
        schema["function"]["name"] for schema in client.complete.await_args_list[0].kwargs["tools"]
    }
    assert first_tool_names == {
        "read_audit_file",
        "write_audit_file",
        "validate_audit_files",
    }
    assert client.complete.await_args_list[1].kwargs["tools"] == ()


@pytest.mark.asyncio
async def test_security_audit_agent_cannot_read_protected_target_files(
    tmp_path: Path,
) -> None:
    target, skill = _roots(tmp_path)
    client = AsyncMock()
    client.complete.side_effect = [
        _result(
            "",
            (
                InferenceToolCall(
                    "read-secret",
                    "read_file",
                    {"path": ".env"},
                ),
            ),
        ),
        _result("No secret was available."),
    ]
    worker = SecurityAuditWorker(
        client,
        target=target,
        skill_dir=skill,
        output_dir=tmp_path / "output",
        max_agents=1,
    )

    result = await worker._spawn_agent(role="research", prompt="Inspect source", model="model")

    assert result == "No secret was available."
    tool_message = client.complete.await_args_list[1].kwargs["messages"][-1]
    assert tool_message["content"] == "error: Protected target file cannot be read"


@pytest.mark.asyncio
async def test_security_audit_worker_requires_complete_artifact_set(tmp_path: Path) -> None:
    target, skill = _roots(tmp_path)
    client = AsyncMock()
    client.complete.return_value = _result("Done")

    with pytest.raises(SecurityAuditError, match="outputs are missing"):
        await SecurityAuditWorker(
            client,
            target=target,
            skill_dir=skill,
            output_dir=tmp_path / "output",
        ).run(
            repository="owner/repo",
            source_ref="a" * 40,
            profile="quick",
            scope=".",
            model="model",
        )


@pytest.mark.asyncio
async def test_security_audit_worker_runs_pinned_validators_with_sanitized_env(
    tmp_path: Path,
) -> None:
    target, skill = _roots(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    (output / "findings.json").write_text("[]\n", encoding="utf-8")
    (output / "coverage-ledger.json").write_text("[]\n", encoding="utf-8")
    validator = (
        "if (process.env.INFERENCE_ROUTER_API_KEY) process.exit(2);\nconsole.log('valid');\n"
    )
    (skill / "validate-findings.cjs").write_text(validator, encoding="utf-8")
    (skill / "validate-coverage-ledger.cjs").write_text(validator, encoding="utf-8")
    worker = SecurityAuditWorker(
        AsyncMock(),
        target=target,
        skill_dir=skill,
        output_dir=output,
    )

    result = await worker._run_validators()

    assert result.count("exit_code=0") == 2
    assert result.count("\nvalid\n") == 2


@pytest.mark.asyncio
async def test_security_audit_worker_reports_missing_validator_input(tmp_path: Path) -> None:
    target, skill = _roots(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    worker = SecurityAuditWorker(
        AsyncMock(),
        target=target,
        skill_dir=skill,
        output_dir=output,
    )

    with pytest.raises(SecurityAuditError, match="input is missing"):
        await worker._run_validators()
