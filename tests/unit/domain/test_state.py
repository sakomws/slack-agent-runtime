from slack_agent_runtime.domain.models import (
    MAX_HISTORY_CHARS,
    MAX_HISTORY_MESSAGES,
    MAX_RESPONSE_GROUPS,
    ThreadState,
    ThreadStateStore,
)


def test_history_is_bounded_by_message_count() -> None:
    state = ThreadState()

    for index in range(MAX_HISTORY_MESSAGES):
        state.record_exchange(f"question {index}", f"answer {index}")

    assert len(state.messages) == MAX_HISTORY_MESSAGES
    assert state.messages[0]["content"] == f"question {MAX_HISTORY_MESSAGES // 2}"


def test_history_is_bounded_by_character_count() -> None:
    state = ThreadState()
    large = "x" * (MAX_HISTORY_CHARS // 2)

    state.record_exchange("first", large)
    state.record_exchange("second", large)

    assert state.messages == [
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": large},
    ]


def test_response_timestamp_groups_are_bounded() -> None:
    state = ThreadState()

    for index in range(MAX_RESPONSE_GROUPS + 1):
        state.record_response_timestamps([str(index)])

    assert len(state.response_timestamp_groups) == MAX_RESPONSE_GROUPS
    assert state.response_timestamp_groups[0] == ["1"]


def test_thread_store_evicts_oldest_idle_thread() -> None:
    store = ThreadStateStore(max_threads=2)
    first = store.get(("C1", "1"))
    store.get(("C2", "2"))

    store.get(("C3", "3"))

    assert store.get(("C1", "1")) is not first


def test_thread_store_discards_one_thread() -> None:
    store = ThreadStateStore()
    discarded = store.get(("C1", "1"))
    retained = store.get(("C2", "2"))

    store.discard(("C1", "1"))

    assert store.get(("C1", "1")) is not discarded
    assert store.get(("C2", "2")) is retained
