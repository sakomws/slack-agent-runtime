"""Thread state loading, persistence, plugin state, and shutdown."""

import asyncio
import logging
from copy import deepcopy
from typing import Any

from slack_agent_runtime.domain.models import (
    JsonValue,
    ThreadKey,
    ThreadState,
    ThreadStateStore,
)
from slack_agent_runtime.storage.persistence import (
    ThreadStatePersistence,
    ThreadStateSnapshot,
)

logger = logging.getLogger(__name__)


class ThreadRepository:
    def __init__(
        self,
        states: ThreadStateStore,
        persistence: ThreadStatePersistence,
    ) -> None:
        self._states = states
        self._persistence = persistence
        self._active_tasks: set[asyncio.Task[Any]] = set()

    async def state(self, key: ThreadKey) -> ThreadState:
        state = self._states.get(key)
        if state.persistence_loaded:
            return state
        async with state.persistence_lock:
            if state.persistence_loaded:
                return state
            try:
                snapshot = await self._persistence.load(key)
                if snapshot is not None:
                    snapshot.restore(state)
            except Exception:
                logger.warning(
                    "Could not restore persisted thread state",
                    exc_info=True,
                    extra={"channel": key[0], "thread_ts": key[1]},
                )
            finally:
                state.persistence_loaded = True
        return state

    async def get_plugin_state(self, key: ThreadKey, plugin_name: str) -> JsonValue | None:
        # Return a copy so a plugin cannot mutate persisted state without
        # calling set_plugin_state(), where validation occurs.
        value: JsonValue | None = deepcopy((await self.state(key)).get_plugin_state(plugin_name))
        return value

    async def set_plugin_state(
        self,
        key: ThreadKey,
        plugin_name: str,
        value: JsonValue,
    ) -> None:
        state = await self.state(key)
        previous = state.plugin_state
        state.plugin_state = dict(previous)
        state.set_plugin_state(plugin_name, value)
        try:
            # Snapshot construction is the single validation boundary for
            # plugin names, JSON values, depth, count, and total encoded size.
            snapshot = ThreadStateSnapshot.from_state(state)
        except Exception:
            # Keep the previously valid in-memory state when validation fails.
            state.plugin_state = previous
            raise
        state.plugin_state = dict(snapshot.plugin_state)
        try:
            await self._persistence.save(key, snapshot)
        except Exception:
            logger.warning(
                "Could not persist thread state",
                exc_info=True,
                extra={"channel": key[0], "thread_ts": key[1]},
            )

    async def clear_plugin_state(self, key: ThreadKey, plugin_name: str) -> None:
        state = await self.state(key)
        state.clear_plugin_state(plugin_name)
        await self.save(key, state)

    async def save(self, key: ThreadKey, state: ThreadState) -> None:
        try:
            await self._persistence.save(key, ThreadStateSnapshot.from_state(state))
        except Exception:
            logger.warning(
                "Could not persist thread state",
                exc_info=True,
                extra={"channel": key[0], "thread_ts": key[1]},
            )

    async def delete(self, key: ThreadKey) -> None:
        try:
            await self._persistence.delete(key)
        except Exception:
            logger.warning(
                "Could not delete persisted thread state",
                exc_info=True,
                extra={"channel": key[0], "thread_ts": key[1]},
            )

    def track(self, task: asyncio.Task[Any]) -> None:
        self._active_tasks.add(task)

    def untrack(self, task: asyncio.Task[Any]) -> None:
        self._active_tasks.discard(task)

    async def close(self) -> None:
        tasks = tuple(task for task in self._active_tasks if not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._persistence.close()
