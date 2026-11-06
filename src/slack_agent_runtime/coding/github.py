"""Minimal GitHub API client for durable Actions coding jobs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class CodingJobError(RuntimeError):
    """Safe GitHub coding-job failure."""


@dataclass(frozen=True, slots=True)
class CodingRun:
    status: str
    conclusion: str | None
    url: str
    run_id: int


@dataclass(frozen=True, slots=True)
class PullRequestSummary:
    number: int
    title: str
    author: str
    draft: bool
    state: str
    updated_at: str
    url: str
    head_sha: str


class GitHubCodingClient:
    def __init__(
        self,
        *,
        token: str,
        allowed_repositories: frozenset[str],
        api_url: str = "https://api.github.com",
        event_type: str = "slack_agent_coding_job",
        workflow_filename: str = "coding-agent.yml",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.allowed_repositories = allowed_repositories
        self.event_type = event_type
        self.workflow_filename = workflow_filename
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=api_url.rstrip("/"),
            timeout=httpx.Timeout(20.0),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "slack-agent-runtime",
            },
        )

    async def dispatch(
        self,
        repository: str,
        payload: dict[str, str],
        *,
        event_type: str | None = None,
    ) -> None:
        self._require_allowed(repository)
        try:
            response = await self._client.post(
                f"/repos/{repository}/dispatches",
                json={
                    "event_type": event_type or self.event_type,
                    "client_payload": payload,
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise CodingJobError(
                f"GitHub rejected the coding job with HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise CodingJobError("GitHub coding-job dispatch failed") from exc

    async def status(
        self,
        repository: str,
        job_id: str,
        *,
        workflow_filename: str | None = None,
    ) -> CodingRun | None:
        self._require_allowed(repository)
        try:
            response = await self._client.get(
                f"/repos/{repository}/actions/workflows/"
                f"{workflow_filename or self.workflow_filename}/runs",
                params={"event": "repository_dispatch", "per_page": 50},
            )
            response.raise_for_status()
            payload: Any = response.json()
        except httpx.HTTPStatusError as exc:
            raise CodingJobError(
                f"GitHub rejected the status request with HTTP {exc.response.status_code}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise CodingJobError("GitHub coding-job status failed") from exc
        runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
        if not isinstance(runs, list):
            raise CodingJobError("GitHub returned an invalid workflow-run list")
        for item in runs:
            if not isinstance(item, dict):
                continue
            title = item.get("display_title") or item.get("name")
            if not isinstance(title, str) or job_id not in title:
                continue
            status = item.get("status")
            url = item.get("html_url")
            run_id = item.get("id")
            conclusion = item.get("conclusion")
            if (
                isinstance(status, str)
                and isinstance(url, str)
                and isinstance(run_id, int)
                and (conclusion is None or isinstance(conclusion, str))
            ):
                return CodingRun(status, conclusion, url, run_id)
        return None

    async def list_pull_requests(
        self,
        repository: str,
        *,
        state: str = "open",
        page: int = 1,
        per_page: int = 10,
    ) -> tuple[PullRequestSummary, ...]:
        self._require_allowed(repository)
        if state not in {"open", "closed", "all"}:
            raise CodingJobError("Pull request state must be open, closed, or all")
        if page < 1 or per_page < 1 or per_page > 20:
            raise CodingJobError("Pull request pagination is invalid")
        try:
            response = await self._client.get(
                f"/repos/{repository}/pulls",
                params={
                    "state": state,
                    "page": page,
                    "per_page": per_page,
                    "sort": "updated",
                    "direction": "desc",
                },
            )
            response.raise_for_status()
            payload: Any = response.json()
        except httpx.HTTPStatusError as exc:
            raise CodingJobError(
                f"GitHub rejected the pull request list with HTTP {exc.response.status_code}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise CodingJobError("GitHub pull request listing failed") from exc
        if not isinstance(payload, list):
            raise CodingJobError("GitHub returned an invalid pull request list")
        return tuple(_pull_request(item) for item in payload if isinstance(item, dict))

    async def pull_request(self, repository: str, number: int) -> PullRequestSummary:
        self._require_allowed(repository)
        if number < 1:
            raise CodingJobError("Pull request number is invalid")
        try:
            response = await self._client.get(f"/repos/{repository}/pulls/{number}")
            response.raise_for_status()
            payload: Any = response.json()
        except httpx.HTTPStatusError as exc:
            raise CodingJobError(
                f"GitHub rejected the pull request lookup with HTTP {exc.response.status_code}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise CodingJobError("GitHub pull request lookup failed") from exc
        if not isinstance(payload, dict):
            raise CodingJobError("GitHub returned an invalid pull request")
        return _pull_request(payload)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _require_allowed(self, repository: str) -> None:
        if repository not in self.allowed_repositories:
            raise CodingJobError(f"Repository `{repository}` is not allowlisted")


def _pull_request(payload: dict[str, Any]) -> PullRequestSummary:
    user = payload.get("user")
    head = payload.get("head")
    values = {
        "number": payload.get("number"),
        "title": payload.get("title"),
        "author": user.get("login") if isinstance(user, dict) else None,
        "draft": payload.get("draft"),
        "state": payload.get("state"),
        "updated_at": payload.get("updated_at"),
        "url": payload.get("html_url"),
        "head_sha": head.get("sha") if isinstance(head, dict) else None,
    }
    if (
        not isinstance(values["number"], int)
        or not isinstance(values["title"], str)
        or not isinstance(values["author"], str)
        or not isinstance(values["draft"], bool)
        or not isinstance(values["state"], str)
        or not isinstance(values["updated_at"], str)
        or not isinstance(values["url"], str)
        or not isinstance(values["head_sha"], str)
    ):
        raise CodingJobError("GitHub returned an invalid pull request")
    return PullRequestSummary(
        number=values["number"],
        title=values["title"],
        author=values["author"],
        draft=values["draft"],
        state=values["state"],
        updated_at=values["updated_at"],
        url=values["url"],
        head_sha=values["head_sha"],
    )
