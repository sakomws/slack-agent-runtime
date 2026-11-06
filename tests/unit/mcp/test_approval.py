import asyncio

import pytest

from slack_agent_runtime.mcp.approval import ApprovalBroker
from slack_agent_runtime.mcp.manager import McpTool


def _tool() -> McpTool:
    return McpTool("mcp__server__read", "server", "read", "Read", {}, True, False)


@pytest.mark.asyncio
async def test_approval_resolves_once_for_matching_thread() -> None:
    broker = ApprovalBroker(timeout_seconds=1)
    pending = broker.create(_tool(), requester_id="U1", thread_key=("C1", "T1"))

    assert pending.requester_id == "U1"
    assert not broker.resolve(
        pending.id,
        approved=True,
        thread_key=("C1", "other"),
    )
    assert broker.resolve(
        pending.id,
        approved=True,
        thread_key=("C1", "T1"),
    )
    assert not broker.resolve(
        pending.id,
        approved=False,
        thread_key=("C1", "T1"),
    )
    assert await broker.wait(pending.id) is True
    assert await broker.wait(pending.id) is False


@pytest.mark.asyncio
async def test_approval_expires_and_cannot_be_resolved_later() -> None:
    broker = ApprovalBroker(timeout_seconds=0.001)
    pending = broker.create(_tool(), requester_id=None, thread_key=("C1", "T1"))

    assert await broker.wait(pending.id) is False
    assert not broker.resolve(
        pending.id,
        approved=True,
        thread_key=("C1", "T1"),
    )


@pytest.mark.asyncio
async def test_cancel_all_cancels_waiters_and_clears_pending() -> None:
    broker = ApprovalBroker(timeout_seconds=10)
    pending = broker.create(_tool(), requester_id=None, thread_key=("C1", "T1"))
    waiter = asyncio.create_task(broker.wait(pending.id))
    await asyncio.sleep(0)

    broker.cancel_all()

    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not broker.resolve(
        pending.id,
        approved=True,
        thread_key=("C1", "T1"),
    )
