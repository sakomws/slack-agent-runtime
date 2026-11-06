from unittest.mock import Mock, patch

from slack_agent_runtime.plugins.feedback_index import FeedbackIndex


def test_record_returns_false_for_an_unindexed_message() -> None:
    index = FeedbackIndex()

    with patch("slack_agent_runtime.plugins.feedback_index._FEEDBACK_COUNTER.add") as metric:
        assert not index.record(
            "positive",
            channel="C1",
            message_ts="1",
            user="U1",
        )

    metric.assert_not_called()


def test_index_records_every_delivered_chunk_with_shared_metadata() -> None:
    index = FeedbackIndex()
    index.index(
        message_timestamps=["1", "2"],
        model="model-a",
        request_id="req-1",
        channel="C1",
        thread_ts="root",
    )
    metric = Mock()

    with patch("slack_agent_runtime.plugins.feedback_index._FEEDBACK_COUNTER.add", metric):
        assert index.record("positive", channel="C1", message_ts="1", user="U1")
        assert index.record("negative", channel="C1", message_ts="2", user="U2")

    assert metric.call_count == 2
    assert all(call.args[1]["model"] == "model-a" for call in metric.call_args_list)


def test_remove_and_clear_thread_do_not_touch_other_feedback() -> None:
    index = FeedbackIndex()
    for channel, thread_ts, message_ts in (
        ("C1", "root-1", "1"),
        ("C1", "root-1", "2"),
        ("C1", "root-2", "3"),
        ("C2", "root-1", "4"),
    ):
        index.index(
            message_timestamps=[message_ts],
            model="model",
            request_id=None,
            channel=channel,
            thread_ts=thread_ts,
        )

    index.remove("C1", "1")
    index.clear_thread("C1", "root-1")

    with patch("slack_agent_runtime.plugins.feedback_index._FEEDBACK_COUNTER.add"):
        assert not index.record("positive", channel="C1", message_ts="1", user="U1")
        assert not index.record("positive", channel="C1", message_ts="2", user="U1")
        assert index.record("positive", channel="C1", message_ts="3", user="U1")
        assert index.record("positive", channel="C2", message_ts="4", user="U1")


def test_index_evicts_oldest_records_at_its_bound() -> None:
    index = FeedbackIndex()

    with patch("slack_agent_runtime.plugins.feedback_index._MAX_FEEDBACK_RECORDS", 2):
        index.index(
            message_timestamps=["1", "2", "3"],
            model="model",
            request_id=None,
            channel="C1",
            thread_ts="root",
        )

    with patch("slack_agent_runtime.plugins.feedback_index._FEEDBACK_COUNTER.add"):
        assert not index.record("positive", channel="C1", message_ts="1", user="U1")
        assert index.record("positive", channel="C1", message_ts="2", user="U1")
        assert index.record("positive", channel="C1", message_ts="3", user="U1")
