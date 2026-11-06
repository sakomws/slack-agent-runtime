from unittest.mock import MagicMock

import pytest

from slack_agent_runtime.entrypoints.app import app, healthcheck


@pytest.mark.asyncio
async def test_healthcheck_reports_live_socket_task() -> None:
    socket_task = MagicMock()
    socket_task.done.return_value = False
    app.state.socket_task = socket_task

    response = await healthcheck()

    assert response.status_code == 200
    assert response.body == b'{"status":"ok"}'


@pytest.mark.asyncio
async def test_healthcheck_fails_when_socket_task_stops() -> None:
    socket_task = MagicMock()
    socket_task.done.return_value = True
    app.state.socket_task = socket_task

    response = await healthcheck()

    assert response.status_code == 503
    assert response.body == b'{"status":"unhealthy"}'
