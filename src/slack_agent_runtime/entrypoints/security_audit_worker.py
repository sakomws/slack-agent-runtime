"""CLI entry point for a source-only, skill-driven security audit."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from pathlib import Path

from slack_agent_runtime.coding.security_audit import SecurityAuditWorker
from slack_agent_runtime.inference.client import InferenceRouterClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one isolated source security audit")
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--skill-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--result-file", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--profile", choices=("quick", "standard", "deep"), required=True)
    parser.add_argument("--scope", default=".")
    parser.add_argument("--max-agents", type=int, default=24)
    parser.add_argument("--max-steps", type=int, default=120)
    parser.add_argument("--max-agent-steps", type=int, default=32)
    parser.add_argument("--timeout-seconds", type=float, default=10_800)
    return parser


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


async def run(args: argparse.Namespace) -> None:
    target = args.target.resolve(strict=True)
    skill_dir = args.skill_dir.resolve(strict=True)
    output_dir = args.output_dir.resolve(strict=False)
    result_file = args.result_file.resolve(strict=False)
    if output_dir.is_relative_to(target) or result_file.is_relative_to(target):
        raise RuntimeError("Audit outputs must be outside the target repository")
    if not args.repository or not args.source_ref or not args.scope:
        raise RuntimeError("Audit metadata is incomplete")
    if (
        min(
            args.max_agents,
            args.max_steps,
            args.max_agent_steps,
            args.timeout_seconds,
        )
        <= 0
    ):
        raise RuntimeError("Audit limits must be greater than zero")
    model = _required_env("INFERENCE_ROUTER_MODEL")
    client = InferenceRouterClient(
        base_url=_required_env("INFERENCE_ROUTER_BASE_URL").rstrip("/"),
        api_key=_required_env("INFERENCE_ROUTER_API_KEY"),
        model=model,
    )
    try:
        async with asyncio.timeout(args.timeout_seconds):
            result = await SecurityAuditWorker(
                client,
                target=target,
                skill_dir=skill_dir,
                output_dir=output_dir,
                max_steps=args.max_steps,
                max_agent_steps=args.max_agent_steps,
                max_agents=args.max_agents,
            ).run(
                repository=args.repository,
                source_ref=args.source_ref,
                profile=args.profile,
                scope=args.scope,
                model=model,
            )
    finally:
        await client.close()
    result_file.parent.mkdir(parents=True, exist_ok=True)
    result_file.write_text(
        json.dumps(asdict(result), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    try:
        asyncio.run(run(build_parser().parse_args()))
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        raise SystemExit(f"error: {exc}") from exc


if __name__ == "__main__":
    main()
