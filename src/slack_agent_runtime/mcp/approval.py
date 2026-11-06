"""Process-local, opaque approval coordination for MCP tool calls."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from slack_agent_runtime.domain.models import ThreadKey
    from slack_agent_runtime.mcp.manager import McpTool


@dataclass(frozen=True, slots=True)
class PendingApproval:
    id: str
    tool: McpTool
    requester_id: str | None
    thread_key: ThreadKey


@dataclass(slots=True)
class _Pending:
    approval: PendingApproval
    future: asyncio.Future[bool]


class ApprovalBroker:
    """Keep sensitive tool arguments out of Slack action payloads."""

    def __init__(self, *, timeout_seconds: float) -> None:
        self._timeout_seconds = timeout_seconds
        self._pending: dict[str, _Pending] = {}

    def create(
        self,
        tool: McpTool,
        *,
        requester_id: str | None,
        thread_key: ThreadKey,
    ) -> PendingApproval:
        identifier = secrets.token_urlsafe(18)
        approval = PendingApproval(identifier, tool, requester_id, thread_key)
        self._pending[identifier] = _Pending(
            approval,
            asyncio.get_running_loop().create_future(),
        )
        return approval

    async def wait(self, identifier: str) -> bool:
        pending = self._pending.get(identifier)
        if pending is None:
            return False
        try:
            async with asyncio.timeout(self._timeout_seconds):
                return await pending.future
        except TimeoutError:
            return False
        finally:
            self._pending.pop(identifier, None)

    def resolve(
        self,
        identifier: str,
        *,
        approved: bool,
        thread_key: ThreadKey,
    ) -> bool:
        pending = self._pending.get(identifier)
        if pending is None or pending.approval.thread_key != thread_key or pending.future.done():
            return False
        pending.future.set_result(approved)
        return True

    def cancel_all(self) -> None:
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.cancel()
        self._pending.clear()
