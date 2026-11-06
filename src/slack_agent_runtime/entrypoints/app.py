import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(secret_values=configured_secret_values())
    configure_telemetry()
    try:
        settings = SlackAgentRuntimeSettings.from_env()
        persistence = await create_thread_state_persistence(
            settings.thread_state_backend,
            settings.thread_state_url,
        )
        try:
            bot = SlackAgentRuntime(settings, persistence=persistence)
        except Exception:
            await persistence.close()
            raise
        socket_task = asyncio.create_task(bot.run(), name="slack-socket-mode")
        app.state.slack_bot = bot
        app.state.socket_task = socket_task

        try:
            await asyncio.sleep(0)
            if socket_task.done():
                socket_task.result()

            logger.info("Slack Agent Runtime started", extra={"model": settings.model})
            yield
        finally:
            try:
                await bot.close()
            finally:
                socket_task.cancel()
                await asyncio.gather(socket_task, return_exceptions=True)
                logger.info("Slack Agent Runtime stopped")
    finally:
        shutdown_telemetry()


app = FastAPI(
    title="Slack Agent Runtime",
    description="Connects Slack to the Inference Router",
    version="0.2.0",
    lifespan=lifespan,
)


@app.get("/healthcheck")
async def healthcheck() -> JSONResponse:
    socket_task = getattr(app.state, "socket_task", None)
    healthy = socket_task is not None and not socket_task.done()
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "unhealthy"},
    )
