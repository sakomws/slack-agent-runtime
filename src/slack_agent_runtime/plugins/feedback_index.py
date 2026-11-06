import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from opentelemetry import metrics

logger = logging.getLogger(__name__)
_FEEDBACK_COUNTER = metrics.get_meter(__name__).create_counter(
    "slack_agent_runtime_feedback",
    description="Slack reactions to Inference Router bot responses",
)
_MAX_FEEDBACK_RECORDS = 5_000


@dataclass(frozen=True)
class FeedbackRecord:
    model: str
    request_id: str | None
    channel: str
    thread_ts: str


class FeedbackIndex:
    """Bounded lookup from delivered Slack messages to inference metadata."""

    def __init__(self) -> None:
        self._records: OrderedDict[tuple[str, str], FeedbackRecord] = OrderedDict()

    def index(
        self,
        *,
        message_timestamps: list[str],
        model: str,
        request_id: str | None,
        channel: str,
        thread_ts: str,
    ) -> None:
        record = FeedbackRecord(
            model=model,
            request_id=request_id,
            channel=channel,
            thread_ts=thread_ts,
        )
        for message_ts in message_timestamps:
            key = (channel, message_ts)
            self._records[key] = record
            self._records.move_to_end(key)
        while len(self._records) > _MAX_FEEDBACK_RECORDS:
            self._records.popitem(last=False)

    def record(
        self,
        sentiment: str,
        *,
        channel: str,
        message_ts: str,
        user: Any,
    ) -> bool:
        record = self._records.get((channel, message_ts))
        if record is None:
            return False

        attributes = {
            "reaction": sentiment,
            "model": record.model,
            "request_id": record.request_id,
            "channel": record.channel,
            "thread_ts": record.thread_ts,
            "slack_user_id": user,
        }
        logger.info(
            "Slack Agent Runtime feedback",
            extra={"slack_bot_feedback": attributes},
        )
        _FEEDBACK_COUNTER.add(
            1,
            {
                "backend_component": "slack_agent_runtime",
                "group_name": "feedback",
                "reaction": sentiment,
                "model": record.model,
            },
        )
        return True

    def remove(self, channel: str, message_ts: str) -> None:
        self._records.pop((channel, message_ts), None)

    def clear_thread(self, channel: str, thread_ts: str) -> None:
        stale = [
            key
            for key, record in self._records.items()
            if (record.channel, record.thread_ts) == (channel, thread_ts)
        ]
        for key in stale:
            del self._records[key]
