import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from slack_agent_runtime.domain.models import ThreadState, ThreadStateStore
from slack_agent_runtime.inference.client import InferenceResult, InferenceUsage
from slack_agent_runtime.services.conversation_coordinator import ConversationCoordinator
from slack_agent_runtime.storage.persistence import (
    MemoryThreadStatePersistence,
    PostgresThreadStatePersistence,
    RedisThreadStatePersistence,
    SnapshotError,
    SQLiteThreadStatePersistence,
    ThreadStateSnapshot,
)


def _snapshot() -> ThreadStateSnapshot:
    state = ThreadState(
        model="example-model",
        web_search=True,
        streaming_enabled=False,
        usage_enabled=True,
        params={"temperature": 0.5, "reasoning_effort": "high"},
        history_hydrated=True,
    )
    state.record_exchange("question", "answer")
    state.record_response_timestamps(["100.1", "100.2"])
    state.set_plugin_state("example-notes", {"enabled": True, "labels": ["one", "two"]})
    return ThreadStateSnapshot.from_state(state)


@pytest.mark.asyncio
async def test_snapshot_roundtrip_excludes_process_local_state() -> None:
    state = ThreadState()
    task = asyncio.create_task(asyncio.sleep(60))
    try:
        state.active_task = task
        state.active_placeholder_ts = "progress"
        snapshot = ThreadStateSnapshot.from_state(state)

        payload = json.loads(snapshot.to_json())

        assert set(payload) == {
            "version",
            "model",
            "web_search",
            "streaming_enabled",
            "usage_enabled",
            "params",
            "plugin_state",
            "messages",
            "response_timestamp_groups",
            "history_hydrated",
        }
        assert "active" not in snapshot.to_json()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_snapshot_version_one_restores_with_empty_plugin_state() -> None:
    snapshot = ThreadStateSnapshot.from_json(
        json.dumps(
            {
                "version": 1,
                "model": "example-model",
                "web_search": False,
                "usage_enabled": False,
                "params": {},
                "messages": [],
                "response_timestamp_groups": [],
                "history_hydrated": True,
            }
        )
    )
    state = ThreadState()

    snapshot.restore(state)

    assert snapshot.version == 3
    assert state.plugin_state == {}
    assert state.streaming_enabled is True


def test_snapshot_version_two_enables_streaming_by_default() -> None:
    snapshot = ThreadStateSnapshot.from_json(
        json.dumps(
            {
                "version": 2,
                "model": None,
                "web_search": False,
                "usage_enabled": False,
                "params": {},
                "plugin_state": {},
                "messages": [],
                "response_timestamp_groups": [],
                "history_hydrated": True,
            }
        )
    )

    assert snapshot.version == 3
    assert snapshot.streaming_enabled is True


def test_snapshot_rejects_non_json_plugin_state() -> None:
    state = ThreadState()
    state.plugin_state["example"] = {"invalid": float("nan")}

    with pytest.raises(SnapshotError, match=r"invalid JSON constant|invalid number"):
        ThreadStateSnapshot.from_state(state)


@pytest.mark.parametrize(
    "payload",
    (
        "not-json",
        '{"version":999}',
        '{"version":1}',
        json.dumps(
            {
                "version": 1,
                "model": None,
                "web_search": False,
                "usage_enabled": False,
                "params": {},
                "messages": [{"role": "system", "content": "invalid"}],
                "response_timestamp_groups": [],
                "history_hydrated": True,
            }
        ),
    ),
)
def test_snapshot_rejects_corruption_and_unsupported_versions(payload: str) -> None:
    with pytest.raises(SnapshotError):
        ThreadStateSnapshot.from_json(payload)


@pytest.mark.asyncio
async def test_memory_contract_save_load_delete() -> None:
    persistence = MemoryThreadStatePersistence()
    key = ("C1", "100")

    await persistence.save(key, _snapshot())
    assert await persistence.load(key) == _snapshot()

    await persistence.delete(key)
    assert await persistence.load(key) is None
    await persistence.close()


@pytest.mark.asyncio
async def test_sqlite_roundtrip_survives_adapter_restart(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'threads.db'}"
    key = ("C1", "100")
    first = SQLiteThreadStatePersistence(url)
    await first.save(key, _snapshot())
    await first.close()

    second = SQLiteThreadStatePersistence(url)
    assert await second.load(key) == _snapshot()
    await second.delete(key)
    assert await second.load(key) is None
    await second.close()


@pytest.mark.asyncio
async def test_sqlite_three_slash_url_uses_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    persistence = SQLiteThreadStatePersistence("sqlite:///state/threads.db")

    await persistence.save(("C1", "100"), _snapshot())

    assert (tmp_path / "state" / "threads.db").is_file()
    await persistence.close()


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.closed = False

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str) -> None:
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_redis_adapter_contract_with_fake_client() -> None:
    client = _FakeRedis()
    persistence = RedisThreadStatePersistence(client)
    key = ("C1", "100")

    await persistence.save(key, _snapshot())
    assert await persistence.load(key) == _snapshot()
    await persistence.delete(key)
    assert await persistence.load(key) is None
    await persistence.close()
    assert client.closed


class _FakePostgresPool:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.closed = False

    async def execute(self, query: str, *args: str) -> None:
        if query.startswith("INSERT"):
            self.values[(args[0], args[1])] = args[2]
        elif query.startswith("DELETE"):
            self.values.pop((args[0], args[1]), None)

    async def fetchval(self, query: str, *args: str) -> str | None:
        return self.values.get((args[0], args[1]))

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_postgres_adapter_contract_with_fake_pool() -> None:
    pool = _FakePostgresPool()
    persistence = PostgresThreadStatePersistence(pool)
    key = ("C1", "100")

    await persistence.save(key, _snapshot())
    assert await persistence.load(key) == _snapshot()
    await persistence.delete(key)
    assert await persistence.load(key) is None
    await persistence.close()
    assert pool.closed


def _result(content: str) -> InferenceResult:
    return InferenceResult(
        content=content,
        model="example-model",
        request_id="request-1",
        duration_ms=1,
        usage=InferenceUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


@pytest.mark.asyncio
async def test_service_restart_restores_state_and_clear_deletes_it() -> None:
    persistence = MemoryThreadStatePersistence()
    first_client = AsyncMock()
    first_client.complete.return_value = _result("first answer")
    first = ConversationCoordinator(
        first_client,
        default_model="example-model",
        persistence=persistence,
    )
    await first.handle(
        {"text": "first question", "ts": "100", "channel": "C1"},
        AsyncMock(),
    )

    second_client = AsyncMock()
    second_client.complete.return_value = _result("second answer")
    states = ThreadStateStore()
    second = ConversationCoordinator(
        second_client,
        default_model="example-model",
        persistence=persistence,
        state_store=states,
    )
    await second.handle(
        {"text": "follow up", "ts": "101", "thread_ts": "100", "channel": "C1"},
        AsyncMock(),
    )

    assert second_client.complete.await_args.kwargs["messages"] == [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "follow up"},
    ]

    await second.handle(
        {"text": "clear", "ts": "102", "thread_ts": "100", "channel": "C1"},
        AsyncMock(),
    )
    assert await persistence.load(("C1", "100")) is None
