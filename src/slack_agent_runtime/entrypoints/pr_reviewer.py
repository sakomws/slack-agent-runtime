"""CLI entry point for an isolated pull request review."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from slack_agent_runtime.coding.reviewer import PullRequestReviewer
from slack_agent_runtime.inference.client import InferenceRouterClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Review one pull request diff")
    parser.add_argument("--context-file", type=Path, required=True)
    parser.add_argument("--output-file", type=Path, required=True)
    parser.add_argument("--model")
    parser.add_argument("--max-diff-chars", type=int, default=200_000)
    parser.add_argument("--timeout-seconds", type=float, default=600)
    return parser


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


async def run(args: argparse.Namespace) -> None:
    try:
        context: Any = json.loads(args.context_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("Review context is invalid") from exc
    if not isinstance(context, dict):
        raise RuntimeError("Review context is invalid")
    diff = context.get("diff")
    title = context.get("title")
    body = context.get("body")
    if not isinstance(diff, str) or not isinstance(title, str) or not isinstance(body, str):
        raise RuntimeError("Review context is invalid")
    if args.max_diff_chars < 1 or args.timeout_seconds <= 0:
        raise RuntimeError("Reviewer limits must be greater than zero")
    model = (args.model or os.environ.get("INFERENCE_ROUTER_MODEL", "")).strip()
    if not model:
        raise RuntimeError("A review model is required")

    client = InferenceRouterClient(
        base_url=_required_env("INFERENCE_ROUTER_BASE_URL").rstrip("/"),
        api_key=_required_env("INFERENCE_ROUTER_API_KEY"),
        model=model,
    )
    try:
        async with asyncio.timeout(args.timeout_seconds):
            review = await PullRequestReviewer(
                client,
                max_diff_chars=args.max_diff_chars,
            ).review(
                diff=diff,
                title=title,
                body=body,
                model=model,
            )
    finally:
        await client.close()
    args.output_file.parent.mkdir(parents=True, exist_ok=True)
    args.output_file.write_text(
        json.dumps(review.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    try:
        asyncio.run(run(build_parser().parse_args()))
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        raise SystemExit(f"error: {exc}") from exc


if __name__ == "__main__":
    main()
