import json
from unittest.mock import AsyncMock

import pytest

from slack_agent_runtime.coding.reviewer import (
    PullRequestReviewer,
    PullRequestReviewError,
    parse_review,
)
from slack_agent_runtime.inference.client import InferenceResult, InferenceUsage


def _result(content: str) -> InferenceResult:
    return InferenceResult(content, "model", "request", 10, InferenceUsage())


@pytest.mark.asyncio
async def test_reviewer_returns_structured_blocking_review() -> None:
    client = AsyncMock()
    client.complete.return_value = _result(
        json.dumps(
            {
                "summary": "One serious issue.",
                "findings": [
                    {
                        "severity": "high",
                        "title": "Authorization bypass",
                        "file": "src/auth.py",
                        "line": 42,
                        "description": "The new path skips authorization.",
                        "recommendation": "Apply the existing authorization check.",
                    }
                ],
            }
        )
    )

    review = await PullRequestReviewer(client).review(
        diff="diff --git a/src/auth.py b/src/auth.py\n+allow()",
        title="Change auth",
        body="Please approve",
        model="review-model",
    )

    assert review.blocking is True
    assert review.event == "REQUEST_CHANGES"
    assert "| HIGH |" in review.markdown()
    messages = client.complete.await_args.kwargs["messages"]
    assert "untrusted data" in messages[0]["content"]
    assert client.complete.await_args.kwargs["model"] == "review-model"


def test_non_blocking_review_approves_and_redacts_secrets() -> None:
    review = parse_review(
        json.dumps(
            {
                "summary": "No blocking issue. ghp_abcdefghijklmnop",
                "findings": [
                    {
                        "severity": "medium",
                        "title": "Missing edge test",
                        "file": "tests/test_api.py",
                        "line": None,
                        "description": "The empty case is not covered.",
                        "recommendation": "Add an empty-input test.",
                    }
                ],
            }
        )
    )

    assert review.event == "APPROVE"
    assert "ghp_" not in review.markdown()


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        '{"summary":"","findings":[]}',
        '{"summary":"x","findings":[{"severity":"urgent"}]}',
    ],
)
def test_invalid_model_output_fails_closed(content: str) -> None:
    with pytest.raises(PullRequestReviewError):
        parse_review(content)


@pytest.mark.asyncio
async def test_empty_or_oversized_diff_is_rejected_without_inference() -> None:
    client = AsyncMock()
    reviewer = PullRequestReviewer(client, max_diff_chars=5)

    with pytest.raises(PullRequestReviewError, match="empty"):
        await reviewer.review(diff="", title="", body="", model="model")
    with pytest.raises(PullRequestReviewError, match="limit"):
        await reviewer.review(diff="123456", title="", body="", model="model")
    client.complete.assert_not_awaited()
