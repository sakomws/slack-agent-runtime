from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv

from slack_agent_runtime.config import SlackAgentRuntimeSettings
from slack_agent_runtime.inference.telemetry import (
    configure_logging,
    configure_telemetry,
    configured_secret_values,
    shutdown_telemetry,
)
from slack_agent_runtime.slack.bot import SlackAgentRuntime
from slack_agent_runtime.storage.persistence import create_thread_state_persistence

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Slack Agent Runtime over Socket Mode",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Environment file to load without overriding exported variables",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate Slack and inference credentials, then exit",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    return parser


async def run(*, check_only: bool) -> None:
    settings = SlackAgentRuntimeSettings.from_env()
    configure_telemetry()
    try:
        persistence = await create_thread_state_persistence(
            settings.thread_state_backend,
            settings.thread_state_url,
        )
        try:
            bot = SlackAgentRuntime(settings, persistence=persistence)
        except Exception:
            await persistence.close()
            raise
        try:
            identity, models = await bot.check_readiness()
            logger.info(
                "Connections ready: Slack=%s model=%s available_models=%d",
                identity,
                settings.model,
                len(models),
            )
            if check_only:
                return
            logger.info("Listening for Slack messages; press Ctrl+C to stop")
            await bot.run()
        finally:
            await bot.close()
    finally:
        shutdown_telemetry()


def main() -> None:
    args = build_parser().parse_args()
    load_dotenv(args.env_file, override=False)
    configure_logging(
        level=getattr(logging, args.log_level),
        secret_values=configured_secret_values(),
    )
    try:
        asyncio.run(run(check_only=args.check))
    except KeyboardInterrupt:
        logger.info("Stopped")
    except RuntimeError as exc:
        raise SystemExit(f"error: {exc}") from exc
