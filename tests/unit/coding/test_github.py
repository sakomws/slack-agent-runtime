import json

import httpx
import pytest

from slack_agent_runtime.coding.github import CodingJobError, GitHubCodingClient


@pytest.mark.asyncio
async def test_dispatch_uses_repository_dispatch_and_safe_headers() -> None:
    request_seen: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_seen
        request_seen = request
        return httpx.Response(204)

    http = httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    client = GitHubCodingClient(
        token="github_pat_secret",
        allowed_repositories=frozenset({"owner/repo"}),
        api_url="https://api.github.test",
        http_client=http,
    )

    await client.dispatch("owner/repo", {"job_id": "job-1"})

    assert request_seen is not None
    assert request_seen.url.path == "/repos/owner/repo/dispatches"
    assert json.loads(request_seen.content) == {
        "event_type": "slack_agent_coding_job",
        "client_payload": {"job_id": "job-1"},
    }
    await http.aclose()


@pytest.mark.asyncio
async def test_status_finds_matching_durable_workflow_run() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/actions/workflows/coding-agent.yml/runs")
        return httpx.Response(
            200,
            json={
                "workflow_runs": [
                    {
                        "display_title": "Slack coding abc123",
                        "status": "completed",
                        "conclusion": "success",
                        "html_url": "https://github.test/actions/1",
                        "id": 1,
                    }
                ]
            },
        )

    http = httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    client = GitHubCodingClient(
        token="token",
        allowed_repositories=frozenset({"owner/repo"}),
        http_client=http,
    )

    run = await client.status("owner/repo", "abc123")

    assert run is not None
    assert run.conclusion == "success"
    await http.aclose()


@pytest.mark.asyncio
async def test_repository_allowlist_and_upstream_errors_fail_safely() -> None:
    client = GitHubCodingClient(
        token="secret-token",
        allowed_repositories=frozenset({"owner/repo"}),
        http_client=httpx.AsyncClient(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(lambda _: httpx.Response(403, text="secret body")),
        ),
    )

    with pytest.raises(CodingJobError, match="not allowlisted"):
        await client.dispatch("other/repo", {})
    with pytest.raises(CodingJobError, match="HTTP 403") as error:
        await client.dispatch("owner/repo", {})
    assert "secret body" not in str(error.value)
    await client.close()


@pytest.mark.asyncio
async def test_pull_request_listing_and_lookup_are_typed() -> None:
    pull_request = {
        "number": 42,
        "title": "Add feature",
        "user": {"login": "octocat"},
        "draft": False,
        "state": "open",
        "updated_at": "2026-11-06T00:00:00Z",
        "html_url": "https://github.test/owner/repo/pull/42",
        "head": {"sha": "a" * 40},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[pull_request] if request.url.path.endswith("/pulls") else pull_request,
        )

    http = httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    client = GitHubCodingClient(
        token="token",
        allowed_repositories=frozenset({"owner/repo"}),
        http_client=http,
    )

    listed = await client.list_pull_requests("owner/repo", page=1)
    selected = await client.pull_request("owner/repo", 42)

    assert listed == (selected,)
    assert selected.head_sha == "a" * 40
    assert selected.author == "octocat"
    await http.aclose()
