"""Process-local access, administration, rate, token, and concurrency policy."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class PolicyConfig:
    allowed_workspace_ids: frozenset[str] = frozenset()
    allowed_channel_ids: frozenset[str] = frozenset()
    allowed_user_ids: frozenset[str] = frozenset()
    admin_user_ids: frozenset[str] = frozenset()
    request_limit: int = 0
    request_window_seconds: float = 60.0
    token_budget: int = 0
    token_window_seconds: float = 3600.0
    max_concurrent_requests: int = 0


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    message: str | None = None


@dataclass(frozen=True)
class InferencePermit:
    user_id: str
    sequence: int


@dataclass
class InMemoryPolicy:
    config: PolicyConfig = field(default_factory=PolicyConfig)
    clock: Callable[[], float] = time.monotonic
    _requests: dict[str, deque[float]] = field(
        default_factory=lambda: defaultdict(deque), init=False
    )
    _tokens: dict[str, deque[tuple[float, int]]] = field(
        default_factory=lambda: defaultdict(deque), init=False
    )
    _active: dict[str, int] = field(default_factory=dict, init=False)
    _active_permits: set[InferencePermit] = field(default_factory=set, init=False)
    _next_sequence: int = field(default=0, init=False)

    def check_access(
        self,
        *,
        workspace_id: str | None,
        channel_id: str | None,
        user_id: str | None,
    ) -> PolicyDecision:
        if (
            self.config.allowed_workspace_ids
            and workspace_id not in self.config.allowed_workspace_ids
        ):
            return PolicyDecision(False, "This workspace is not allowed to use this app.")
        if self.config.allowed_channel_ids and channel_id not in self.config.allowed_channel_ids:
            return PolicyDecision(False, "This channel is not allowed to use this app.")
        if self.config.allowed_user_ids and user_id not in self.config.allowed_user_ids:
            return PolicyDecision(False, "You are not allowed to use this app.")
        return PolicyDecision(True)

    def check_admin(self, user_id: str | None) -> PolicyDecision:
        if user_id is None or user_id not in self.config.admin_user_ids:
            return PolicyDecision(
                False,
                "Only an app administrator can change model or generation settings.",
            )
        return PolicyDecision(True)

    def acquire_inference(
        self, user_id: str | None
    ) -> tuple[PolicyDecision, InferencePermit | None]:
        if not user_id:
            return PolicyDecision(False, "I couldn't identify your Slack user account."), None

        now = self.clock()
        requests = self._requests[user_id]
        self._expire_times(requests, now - self.config.request_window_seconds)
        if self.config.request_limit and len(requests) >= self.config.request_limit:
            return (
                PolicyDecision(False, "You've reached the request limit. Please try again later."),
                None,
            )

        tokens = self._tokens[user_id]
        self._expire_tokens(tokens, now - self.config.token_window_seconds)
        if (
            self.config.token_budget
            and sum(count for _, count in tokens) >= self.config.token_budget
        ):
            return (
                PolicyDecision(False, "You've reached the token budget. Please try again later."),
                None,
            )

        active = self._active.get(user_id, 0)
        if self.config.max_concurrent_requests and active >= self.config.max_concurrent_requests:
            return (
                PolicyDecision(
                    False,
                    "You already have the maximum number of inference requests running.",
                ),
                None,
            )

        requests.append(now)
        self._active[user_id] = active + 1
        self._next_sequence += 1
        permit = InferencePermit(user_id, self._next_sequence)
        self._active_permits.add(permit)
        return PolicyDecision(True), permit

    def release_inference(
        self, permit: InferencePermit, *, total_tokens: int | None = None
    ) -> None:
        if permit not in self._active_permits:
            return
        self._active_permits.remove(permit)
        active = self._active.get(permit.user_id, 0)
        if active <= 1:
            self._active.pop(permit.user_id, None)
        else:
            self._active[permit.user_id] = active - 1
        if total_tokens is not None and total_tokens > 0:
            self._tokens[permit.user_id].append((self.clock(), total_tokens))

    @staticmethod
    def _expire_times(values: deque[float], cutoff: float) -> None:
        while values and values[0] <= cutoff:
            values.popleft()

    @staticmethod
    def _expire_tokens(values: deque[tuple[float, int]], cutoff: float) -> None:
        while values and values[0][0] <= cutoff:
            values.popleft()
