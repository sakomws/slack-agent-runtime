"""CLI entry point used by the reusable coding-job workflow."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from pathlib import Path

from slack_agent_runtime.coding.tools import WorkspaceTools
from slack_agent_runtime.coding.worker import CodingWorker
from slack_agent_runtime.inference.client import InferenceRouterClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one bounded coding job")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--output-file", type=Path, required=True)
    parser.add_argument("--max-steps", type=int, default=48)
    parser.add_argument("--max-commands", type=int, default=20)
    parser.add_argument("--max-changed-files", type=int, default=50)
    parser.add_argument("--max-diff-chars", type=int, default=500_000)
    parser.add_argument("--timeout-seconds", type=float, default=1_800)
    return parser


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


async def run(args: argparse.Namespace) -> None:
    workspace = args.workspace.resolve(strict=True)
    output_file = args.output_file.resolve(strict=False)
    if output_file.is_relative_to(workspace):
        raise RuntimeError("--output-file must be outside the repository workspace")
    prompt = args.prompt_file.read_text(encoding="utf-8").strip()
    if not prompt or len(prompt) > 8_000:
        raise RuntimeError("Prompt must contain between 1 and 8,000 characters")
    if (
        min(
            args.max_steps,
            args.max_commands,
            args.max_changed_files,
            args.max_diff_chars,
            args.timeout_seconds,
        )
        <= 0
    ):
        raise RuntimeError("Worker limits must be greater than zero")

    client = InferenceRouterClient(
        base_url=_required_env("INFERENCE_ROUTER_BASE_URL").rstrip("/"),
        api_key=_required_env("INFERENCE_ROUTER_API_KEY"),
        model=_required_env("INFERENCE_ROUTER_MODEL"),
    )
    tools = WorkspaceTools(
        workspace,
        max_commands=args.max_commands,
        max_changed_files=args.max_changed_files,
        max_diff_chars=args.max_diff_chars,
    )
    try:
        async with asyncio.timeout(args.timeout_seconds):
            result = await CodingWorker(client, tools, max_steps=args.max_steps).run(
                prompt,
                model=_required_env("INFERENCE_ROUTER_MODEL"),
            )
    finally:
        await client.close()
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(
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
