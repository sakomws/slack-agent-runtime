"""Feedback plugin: reaction handling and feedback indexing."""

from collections.abc import Mapping

from slack_agent_runtime.plugins.api import (
    EventContext,
    PluginRoute,
)
from slack_agent_runtime.plugins.feedback_index import FeedbackIndex

_POSITIVE_REACTIONS = frozenset({"+1", "thumbsup"})
_NEGATIVE_REACTIONS = frozenset({"-1", "thumbsdown"})


class FeedbackPlugin:
    name = "feedback"

    def __init__(self, feedback: FeedbackIndex) -> None:
        self._feedback = feedback
        self.routes = (PluginRoute.event("reaction_added", self._reaction),)

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def _reaction(self, context: EventContext) -> None:
        event = context.event
        reaction = event.get("reaction")
        if reaction in _POSITIVE_REACTIONS:
            sentiment = "positive"
        elif reaction in _NEGATIVE_REACTIONS:
            sentiment = "negative"
        else:
            return

        item = event.get("item")
        if not isinstance(item, Mapping) or item.get("type") != "message":
            return
        channel = item.get("channel")
        message_ts = item.get("ts")
        if not isinstance(channel, str) or not isinstance(message_ts, str):
            return
        user = event.get("user")
        self._feedback.record(
            sentiment,
            channel=channel,
            message_ts=message_ts,
            user=user if isinstance(user, str) else None,
        )


def feedback_plugin(feedback: FeedbackIndex) -> FeedbackPlugin:
    return FeedbackPlugin(feedback)
