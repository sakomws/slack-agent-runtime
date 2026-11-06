"""Validated thread snapshots and interchangeable persistence adapters.

Only durable conversation data belongs here. Tasks, locks, credentials, and
telemetry remain process-local and are intentionally absent from snapshots.
"""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import unquote, urlparse

from slack_agent_runtime.domain.models import (
    MAX_HISTORY_CHARS,
    MAX_HISTORY_MESSAGES,
    MAX_RESPONSE_GROUPS,
    ChatMessage,
    JsonValue,
    ThreadKey,
    ThreadState,
)
from slack_agent_runtime.domain.validation import is_plugin_name

SNAPSHOT_VERSION = 3
MAX_SNAPSHOT_BYTES = 128_000
MAX_PARAM_COUNT = 32
MAX_PARAM_NAME_CHARS = 64
MAX_TIMESTAMP_CHARS = 64
MAX_PLUGIN_COUNT = 64
MAX_PLUGIN_NAME_CHARS = 64
MAX_PLUGIN_STATE_DEPTH = 8
MAX_PLUGIN_STATE_ITEMS = 1_000
_TABLE = "thread_state"


class SnapshotError(ValueError):
    """Raised when persisted state is corrupt, oversized, or unsupported."""


@dataclass(frozen=True)
class ThreadStateSnapshot:
    """Portable, size-bounded representation of one Slack thread."""

    version: int
    model: str | None
    web_search: bool
    streaming_enabled: bool
    usage_enabled: bool
    params: dict[str, str | int | float | bool | None]
    plugin_state: dict[str, JsonValue]
    messages: list[ChatMessage]
    response_timestamp_groups: list[list[str]]
    history_hydrated: bool

    @classmethod
    def from_state(cls, state: ThreadState) -> ThreadStateSnapshot:
        # Serialize and parse once before returning. That round trip applies
        # exactly the same validation to newly created and loaded snapshots.
        snapshot = cls(
            version=SNAPSHOT_VERSION,
            model=state.model,
            web_search=state.web_search,
            streaming_enabled=state.streaming_enabled,
            usage_enabled=state.usage_enabled,
            params=dict(state.params),
            plugin_state=dict(state.plugin_state),
            messages=[dict(message) for message in state.messages],
            response_timestamp_groups=[list(group) for group in state.response_timestamp_groups],
            history_hydrated=state.history_hydrated,
        )
        return cls.from_json(snapshot.to_json())

    def restore(self, state: ThreadState) -> None:
        state.model = self.model
        state.web_search = self.web_search
        state.streaming_enabled = self.streaming_enabled
        state.usage_enabled = self.usage_enabled
        state.params = dict(self.params)
        state.plugin_state = dict(self.plugin_state)
        state.restore_history(
            [dict(message) for message in self.messages],
            [list(group) for group in self.response_timestamp_groups],
        )
        state.history_hydrated = self.history_hydrated

    def to_json(self) -> str:
        payload = {
            "version": self.version,
            "model": self.model,
            "web_search": self.web_search,
            "streaming_enabled": self.streaming_enabled,
            "usage_enabled": self.usage_enabled,
            "params": self.params,
            "plugin_state": self.plugin_state,
            "messages": self.messages,
            "response_timestamp_groups": self.response_timestamp_groups,
            "history_hydrated": self.history_hydrated,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        if len(encoded.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise SnapshotError("thread state snapshot is too large")
        return encoded

    @classmethod
    def from_json(cls, raw: str | bytes) -> ThreadStateSnapshot:
        raw_bytes = raw.encode("utf-8") if isinstance(raw, str) else raw
        if len(raw_bytes) > MAX_SNAPSHOT_BYTES:
            raise SnapshotError("thread state snapshot is too large")
        try:
            value = json.loads(
                raw_bytes,
                parse_constant=lambda constant: (_ for _ in ()).throw(
                    SnapshotError(f"invalid JSON constant: {constant}")
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SnapshotError("thread state snapshot is invalid JSON") from exc
        if not isinstance(value, dict):
            raise SnapshotError("thread state snapshot must be an object")
        # Version 1 predates feature plugins. Treat its missing plugin state as
        # empty and normalize the in-memory object to the current version.
        expected_v1 = {
            "version",
            "model",
            "web_search",
            "usage_enabled",
            "params",
            "messages",
            "response_timestamp_groups",
            "history_hydrated",
        }
        expected_v2 = {*expected_v1, "plugin_state"}
        expected_v3 = {*expected_v2, "streaming_enabled"}
        version = value.get("version")
        expected = expected_v1 if version == 1 else expected_v2 if version == 2 else expected_v3
        if set(value) != expected:
            raise SnapshotError("thread state snapshot has invalid fields")
        if version not in {1, 2, SNAPSHOT_VERSION}:
            raise SnapshotError("thread state snapshot version is unsupported")
        model = value["model"]
        if model is not None and (not isinstance(model, str) or len(model) > 256):
            raise SnapshotError("thread state model is invalid")
        for field in ("web_search", "usage_enabled", "history_hydrated"):
            if not isinstance(value[field], bool):
                raise SnapshotError(f"thread state {field} is invalid")
        streaming_enabled = value.get("streaming_enabled", True)
        if not isinstance(streaming_enabled, bool):
            raise SnapshotError("thread state streaming_enabled is invalid")
        params = _validate_params(value["params"])
        plugin_state = _validate_plugin_state(value.get("plugin_state", {}))
        messages = _validate_messages(value["messages"])
        groups = _validate_timestamp_groups(value["response_timestamp_groups"])
        return cls(
            version=SNAPSHOT_VERSION,
            model=model,
            web_search=value["web_search"],
            streaming_enabled=streaming_enabled,
            usage_enabled=value["usage_enabled"],
            params=params,
            plugin_state=plugin_state,
            messages=messages,
            response_timestamp_groups=groups,
            history_hydrated=value["history_hydrated"],
        )


class ThreadStatePersistence(Protocol):
    async def load(self, key: ThreadKey) -> ThreadStateSnapshot | None: ...

    async def save(self, key: ThreadKey, snapshot: ThreadStateSnapshot) -> None: ...

    async def delete(self, key: ThreadKey) -> None: ...

    async def close(self) -> None: ...


class MemoryThreadStatePersistence:
    def __init__(self) -> None:
        self._snapshots: dict[ThreadKey, str] = {}

    async def load(self, key: ThreadKey) -> ThreadStateSnapshot | None:
        raw = self._snapshots.get(key)
        return ThreadStateSnapshot.from_json(raw) if raw is not None else None

    async def save(self, key: ThreadKey, snapshot: ThreadStateSnapshot) -> None:
        self._snapshots[key] = snapshot.to_json()

    async def delete(self, key: ThreadKey) -> None:
        self._snapshots.pop(key, None)

    async def close(self) -> None:
        return None


class SQLiteThreadStatePersistence:
    def __init__(self, url: str) -> None:
        path = _sqlite_path(url)
        self._path = path if path == ":memory:" else str(Path(path).expanduser())
        self._connection: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def _connect(self) -> sqlite3.Connection:
        if self._connection is None:
            self._connection = await asyncio.to_thread(self._open)
        return self._connection

    def _open(self) -> sqlite3.Connection:
        if self._path != ":memory:":
            Path(self._path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path, check_same_thread=False)
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
            "channel_id TEXT NOT NULL, thread_ts TEXT NOT NULL, snapshot TEXT NOT NULL,"
            "PRIMARY KEY (channel_id, thread_ts))"
        )
        connection.commit()
        return connection

    async def load(self, key: ThreadKey) -> ThreadStateSnapshot | None:
        async with self._lock:
            connection = await self._connect()
            row = await asyncio.to_thread(
                lambda: connection.execute(
                    f"SELECT snapshot FROM {_TABLE} WHERE channel_id = ? AND thread_ts = ?",
                    key,
                ).fetchone()
            )
        return ThreadStateSnapshot.from_json(row[0]) if row is not None else None

    async def save(self, key: ThreadKey, snapshot: ThreadStateSnapshot) -> None:
        raw = snapshot.to_json()
        async with self._lock:
            connection = await self._connect()

            def write() -> None:
                connection.execute(
                    f"INSERT INTO {_TABLE} (channel_id, thread_ts, snapshot) VALUES (?, ?, ?) "
                    "ON CONFLICT(channel_id, thread_ts) DO UPDATE SET snapshot = excluded.snapshot",
                    (*key, raw),
                )
                connection.commit()

            await asyncio.to_thread(write)

    async def delete(self, key: ThreadKey) -> None:
        async with self._lock:
            connection = await self._connect()

            def remove() -> None:
                connection.execute(
                    f"DELETE FROM {_TABLE} WHERE channel_id = ? AND thread_ts = ?", key
                )
                connection.commit()

            await asyncio.to_thread(remove)

    async def close(self) -> None:
        async with self._lock:
            if self._connection is not None:
                await asyncio.to_thread(self._connection.close)
                self._connection = None


class RedisThreadStatePersistence:
    def __init__(self, client: Any, *, prefix: str = "inference-router:thread-state:") -> None:
        self._client = client
        self._prefix = prefix

    @classmethod
    async def from_url(cls, url: str) -> RedisThreadStatePersistence:
        try:
            from redis.asyncio import Redis
        except ImportError as exc:
            raise RuntimeError("Redis persistence requires the 'redis' extra") from exc
        return cls(Redis.from_url(url, decode_responses=False))

    def _key(self, key: ThreadKey) -> str:
        return f"{self._prefix}{key[0]}:{key[1]}"

    async def load(self, key: ThreadKey) -> ThreadStateSnapshot | None:
        raw = await self._client.get(self._key(key))
        return ThreadStateSnapshot.from_json(raw) if raw is not None else None

    async def save(self, key: ThreadKey, snapshot: ThreadStateSnapshot) -> None:
        await self._client.set(self._key(key), snapshot.to_json())

    async def delete(self, key: ThreadKey) -> None:
        await self._client.delete(self._key(key))

    async def close(self) -> None:
        close = getattr(self._client, "aclose", None)
        if close is not None:
            await close()


class PostgresThreadStatePersistence:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    async def from_url(cls, url: str) -> PostgresThreadStatePersistence:
        try:
            import asyncpg
        except ImportError as exc:
            raise RuntimeError("PostgreSQL persistence requires the 'postgres' extra") from exc
        pool = await asyncpg.create_pool(url)
        adapter = cls(pool)
        await adapter._initialize()
        return adapter

    async def _initialize(self) -> None:
        await self._pool.execute(
            f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
            "channel_id TEXT NOT NULL, thread_ts TEXT NOT NULL, snapshot JSONB NOT NULL,"
            "PRIMARY KEY (channel_id, thread_ts))"
        )

    async def load(self, key: ThreadKey) -> ThreadStateSnapshot | None:
        raw = await self._pool.fetchval(
            f"SELECT snapshot::text FROM {_TABLE} WHERE channel_id = $1 AND thread_ts = $2",
            *key,
        )
        return ThreadStateSnapshot.from_json(raw) if raw is not None else None

    async def save(self, key: ThreadKey, snapshot: ThreadStateSnapshot) -> None:
        await self._pool.execute(
            f"INSERT INTO {_TABLE} (channel_id, thread_ts, snapshot) VALUES ($1, $2, $3::jsonb) "
            "ON CONFLICT(channel_id, thread_ts) DO UPDATE SET snapshot = EXCLUDED.snapshot",
            *key,
            snapshot.to_json(),
        )

    async def delete(self, key: ThreadKey) -> None:
        await self._pool.execute(
            f"DELETE FROM {_TABLE} WHERE channel_id = $1 AND thread_ts = $2", *key
        )

    async def close(self) -> None:
        await self._pool.close()


async def create_thread_state_persistence(
    backend: str = "memory", url: str = ""
) -> ThreadStatePersistence:
    normalized = backend.strip().lower() or "memory"
    if normalized == "memory":
        return MemoryThreadStatePersistence()
    if normalized not in {"sqlite", "redis", "postgres"}:
        raise RuntimeError("THREAD_STATE_BACKEND must be memory, sqlite, redis, or postgres")
    if not url.strip():
        raise RuntimeError("THREAD_STATE_URL is required for non-memory persistence")
    if normalized == "sqlite":
        return SQLiteThreadStatePersistence(url)
    if normalized == "redis":
        return await RedisThreadStatePersistence.from_url(url)
    return await PostgresThreadStatePersistence.from_url(url)


def _validate_params(value: object) -> dict[str, str | int | float | bool | None]:
    if not isinstance(value, dict) or len(value) > MAX_PARAM_COUNT:
        raise SnapshotError("thread state params are invalid")
    result: dict[str, str | int | float | bool | None] = {}
    for name, item in value.items():
        if not isinstance(name, str) or not name or len(name) > MAX_PARAM_NAME_CHARS:
            raise SnapshotError("thread state parameter name is invalid")
        if item is not None and not isinstance(item, (str, int, float, bool)):
            raise SnapshotError("thread state parameter value is invalid")
        if isinstance(item, float) and not math.isfinite(item):
            raise SnapshotError("thread state parameter value is invalid")
        result[name] = item
    return result


def _validate_plugin_state(value: object) -> dict[str, JsonValue]:
    """Validate namespaced plugin data without trusting type annotations."""

    if not isinstance(value, dict) or len(value) > MAX_PLUGIN_COUNT:
        raise SnapshotError("thread plugin state is invalid")
    remaining = [MAX_PLUGIN_STATE_ITEMS]
    result: dict[str, JsonValue] = {}
    for name, item in value.items():
        if (
            not isinstance(name, str)
            or len(name) > MAX_PLUGIN_NAME_CHARS
            or not is_plugin_name(name)
        ):
            raise SnapshotError("thread plugin name is invalid")
        result[name] = _validate_json_value(item, depth=0, remaining=remaining)
    return result


def _validate_json_value(value: object, *, depth: int, remaining: list[int]) -> JsonValue:
    # ``remaining`` is shared across recursion to cap total complexity, not
    # merely the size of each individual list or object.
    remaining[0] -= 1
    if remaining[0] < 0 or depth > MAX_PLUGIN_STATE_DEPTH:
        raise SnapshotError("thread plugin state is too complex")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SnapshotError("thread plugin state contains an invalid number")
        return value
    if isinstance(value, list):
        return [_validate_json_value(item, depth=depth + 1, remaining=remaining) for item in value]
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 256:
                raise SnapshotError("thread plugin state contains an invalid key")
            result[key] = _validate_json_value(item, depth=depth + 1, remaining=remaining)
        return result
    raise SnapshotError("thread plugin state contains an unsupported value")


def _validate_messages(value: object) -> list[ChatMessage]:
    if not isinstance(value, list) or len(value) > MAX_HISTORY_MESSAGES:
        raise SnapshotError("thread state messages are invalid")
    result: list[ChatMessage] = []
    total_chars = 0
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"role", "content"}:
            raise SnapshotError("thread state message is invalid")
        role, content = item["role"], item["content"]
        if role not in {"user", "assistant"} or not isinstance(content, str):
            raise SnapshotError("thread state message is invalid")
        total_chars += len(content)
        result.append({"role": cast("str", role), "content": content})
    if total_chars > MAX_HISTORY_CHARS:
        raise SnapshotError("thread state messages are too large")
    return result


def _validate_timestamp_groups(value: object) -> list[list[str]]:
    if not isinstance(value, list) or len(value) > MAX_RESPONSE_GROUPS:
        raise SnapshotError("thread state response timestamps are invalid")
    groups: list[list[str]] = []
    for group in value:
        if not isinstance(group, list) or len(group) > MAX_HISTORY_MESSAGES:
            raise SnapshotError("thread state response timestamps are invalid")
        if any(
            not isinstance(timestamp, str) or not timestamp or len(timestamp) > MAX_TIMESTAMP_CHARS
            for timestamp in group
        ):
            raise SnapshotError("thread state response timestamp is invalid")
        groups.append(cast("list[str]", list(group)))
    return groups


def _sqlite_path(url: str) -> str:
    if url == ":memory:" or url == "sqlite:///:memory:":
        return ":memory:"
    if not url.startswith("sqlite:///"):
        raise RuntimeError("THREAD_STATE_URL must be a sqlite:/// URL")
    parsed = urlparse(url)
    if parsed.scheme != "sqlite" or parsed.netloc not in {"", "localhost"}:
        raise RuntimeError("THREAD_STATE_URL must be a sqlite:/// URL")
    path = unquote(parsed.path)
    if not path:
        raise RuntimeError("THREAD_STATE_URL must include a SQLite path")
    if parsed.netloc == "localhost" or path.startswith("//"):
        return f"/{path.lstrip('/')}"
    return path.lstrip("/")
