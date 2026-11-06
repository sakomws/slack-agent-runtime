"""Bounded conversation state."""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, TypeAlias

ThreadKey: TypeAlias = tuple[str, str]
ChatMessage: TypeAlias = dict[str, str]
JsonValue: TypeAlias = str | int | float | bool | list["JsonValue"] | dict[str, "JsonValue"] | None
ParameterValue: TypeAlias = str | int | float | bool | None

MAX_HISTORY_MESSAGES = 20
MAX_HISTORY_CHARS = 50_000
MAX_RESPONSE_GROUPS = MAX_HISTORY_MESSAGES // 2


@dataclass
class ThreadState:
    model: str | None = None
    web_search: bool = False
    streaming_enabled: bool = True
    usage_enabled: bool = False
    params: dict[str, ParameterValue] = field(default_factory=dict)
    plugin_state: dict[str, JsonValue] = field(default_factory=dict)
    messages: list[ChatMessage] = field(default_factory=list)
    response_timestamp_groups: list[list[str]] = field(default_factory=list)
    active_task: asyncio.Task[Any] | None = None
    active_placeholder_ts: str | None = None
    history_hydrated: bool = False
    hydration_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    persistence_loaded: bool = False
    persistence_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def messages_with(self, prompt: str) -> list[ChatMessage]:
        return [*self.messages, {"role": "user", "content": prompt}]

    def record_exchange(self, prompt: str, answer: str) -> None:
        self.messages.extend(
            ({"role": "user", "content": prompt}, {"role": "assistant", "content": answer})
        )
        self._trim_history()

    def pop_last_exchange(self) -> str | None:
        if len(self.messages) < 2:
            return None
        user, assistant = self.messages[-2:]
        if user.get("role") != "user" or assistant.get("role") != "assistant":
            return None
        self.messages = self.messages[:-2]
        return user.get("content")

    def reset_settings(self) -> None:
        self.model = None
        self.web_search = self.usage_enabled = False
        self.streaming_enabled = True
        self.params.clear()

    def clear(self) -> None:
        self.reset_settings()
        self.messages.clear()
        self.response_timestamp_groups.clear()
        self.plugin_state.clear()
        self.history_hydrated = True

    def get_plugin_state(self, plugin_name: str) -> JsonValue | None:
        return self.plugin_state.get(plugin_name)

    def set_plugin_state(self, plugin_name: str, value: JsonValue) -> None:
        self.plugin_state[plugin_name] = value

    def clear_plugin_state(self, plugin_name: str) -> None:
        self.plugin_state.pop(plugin_name, None)

    def restore_history(self, messages: list[ChatMessage], groups: list[list[str]]) -> None:
        self.messages = messages
        self.response_timestamp_groups = groups[-MAX_RESPONSE_GROUPS:]
        self._trim_history()
        self.history_hydrated = True

    def record_response_timestamps(self, timestamps: list[str]) -> None:
        if timestamps:
            self.response_timestamp_groups.append(timestamps)
            del self.response_timestamp_groups[:-MAX_RESPONSE_GROUPS]

    def _trim_history(self) -> None:
        while len(self.messages) > MAX_HISTORY_MESSAGES:
            del self.messages[:2]
        while (
            len(self.messages) > 2
            and sum(len(message.get("content", "")) for message in self.messages)
            > MAX_HISTORY_CHARS
        ):
            del self.messages[:2]


class ThreadStateStore:
    def __init__(self, max_threads: int = 1_000) -> None:
        self._max_threads = max_threads
        self._states: OrderedDict[ThreadKey, ThreadState] = OrderedDict()

    def get(self, key: ThreadKey) -> ThreadState:
        state = self._states.get(key)
        if state is None:
            state = self._states[key] = ThreadState()
        else:
            self._states.move_to_end(key)
        while len(self._states) > self._max_threads:
            idle = next(
                (item for item in self._states.items() if item[1].active_task is None), None
            )
            if idle is None:
                break
            del self._states[idle[0]]
        return state

    def discard(self, key: ThreadKey) -> None:
        self._states.pop(key, None)
