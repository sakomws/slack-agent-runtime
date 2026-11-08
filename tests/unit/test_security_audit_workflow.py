from pathlib import Path

ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "security-audit-reusable.yml"


def test_security_audit_workflow_pins_skill_and_separates_credentials() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "ref: c1c8a8c1471069fb0e188eeaff69b8e8db6564a8" in text
    worker_step = text.split(
        "- name: Run skill-driven source security audit",
        maxsplit=1,
    )[1].split("- name: Validate audit artifacts", maxsplit=1)[0]
    assert "INFERENCE_ROUTER_API_KEY" in worker_step
    assert "SLACK_CODING_BOT_TOKEN" not in worker_step
    assert "GH_TOKEN" not in worker_step


def test_security_audit_workflow_validates_before_upload() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    validate_position = text.index("- name: Validate audit artifacts")
    upload_position = text.index("- name: Upload validated security audit report")
    assert validate_position < upload_position
    assert "validate-findings.cjs" in text
    assert "validate-coverage-ledger.cjs" in text
    assert "secret-like value found" in text
