"""Admin-only Slack orchestration for pull request listing and review jobs."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from slack_agent_runtime.coding.github import CodingJobError
from slack_agent_runtime.domain.commands import HelpEntry
from slack_agent_runtime.inference.client import InferenceRouterError
from slack_agent_runtime.plugins.api import ActionContext, CommandContext, PluginRoute
from slack_agent_runtime.slack.ui import ACTION_PR_REVIEW, pull_request_blocks

if TYPE_CHECKING:
    from slack_agent_runtime.coding.github import GitHubCodingClient, PullRequestSummary
    from slack_agent_runtime.domain.policy import InMemoryPolicy
    from slack_agent_runtime.inference.client import InferenceClient
    from slack_agent_runtime.plugins.api import PluginHost


class PullRequestReviewPlugin:
    name = "reviews"
    help_entries = (
        HelpEntry(
            "Pull requests",
            "prs owner/repository or GitHub repository URL [open|closed|all|draft] [page N]",
        ),
        HelpEntry(
            "Pull requests",
            "review owner/repository#number or GitHub PR URL [model MODEL]",
        ),
        HelpEntry("Pull requests", "review status"),
    )

    def __init__(
        self,
        client: GitHubCodingClient,
        inference_client: InferenceClient,
        *,
        policy: InMemoryPolicy | None,
        event_type: str,
        workflow_filename: str,
        page_size: int = 10,
    ) -> None:
        self._client = client
        self._inference_client = inference_client
        self._policy = policy
        self._event_type = event_type
        self._workflow_filename = workflow_filename
        self._page_size = page_size
        self.routes = (
            PluginRoute.command("prs", self._prs),
            PluginRoute.command("review", self._review),
            PluginRoute.action(ACTION_PR_REVIEW, self._review_action),
        )

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        await self._client.close()

    async def _prs(self, context: CommandContext) -> None:
        if not await self._require_admin(context.user_id, context.say, context.thread_ts):
            return
        try:
            repository, state, drafts_only, page = _parse_prs(context.prompt)
            pull_requests = await self._client.list_pull_requests(
                repository,
                state=state,
                page=page,
                per_page=self._page_size,
            )
        except CodingJobError as exc:
            await context.say(text=str(exc), thread_ts=context.thread_ts)
            return
        if drafts_only:
            pull_requests = tuple(item for item in pull_requests if item.draft)
        if not pull_requests:
            await context.say(
                text=f"No matching pull requests found in `{repository}` on page {page}.",
                thread_ts=context.thread_ts,
            )
            return
        await context.say(
            text=f"Pull requests for {repository}",
            blocks=pull_request_blocks(
                repository,
                pull_requests,
                model=context.thread.model,
                page=page,
            ),
            thread_ts=context.thread_ts,
        )

    async def _review(self, context: CommandContext) -> None:
        if not await self._require_admin(context.user_id, context.say, context.thread_ts):
            return
        words = context.prompt.split()
        if len(words) == 2 and words[1].lower() == "status":
            await self._status(context)
            return
        try:
            repository, number, model = _parse_review(words, context.thread.model)
            await self._validate_model(model)
            pull_request = await self._client.pull_request(repository, number)
        except (CodingJobError, ValueError, InferenceRouterError) as exc:
            await context.say(text=str(exc), thread_ts=context.thread_ts)
            return
        await self._dispatch(
            repository=repository,
            pull_request=pull_request,
            model=model,
            channel=context.thread_key[0],
            thread_ts=context.thread_ts,
            requested_by=context.user_id,
            host=context.host,
            state_key=context.thread_key,
            say=context.say,
        )

    async def _review_action(self, context: ActionContext) -> None:
        if not await self._require_admin(
            context.user_id,
            context.say,
            context.action.thread_ts,
        ):
            return
        try:
            payload: Any = json.loads(context.action.value)
            if not isinstance(payload, dict):
                raise ValueError
            repository = payload.get("repository")
            number = payload.get("number")
            model = payload.get("model")
            if (
                not isinstance(repository, str)
                or not isinstance(number, int)
                or not isinstance(model, str)
            ):
                raise ValueError
            await self._validate_model(model)
            pull_request = await self._client.pull_request(repository, number)
        except (ValueError, json.JSONDecodeError):
            await context.say(
                text="The pull request review action is invalid.",
                thread_ts=context.action.thread_ts,
            )
            return
        except (CodingJobError, InferenceRouterError) as exc:
            await context.say(text=str(exc), thread_ts=context.action.thread_ts)
            return
        if context.host is None:
            await context.say(
                text="Review state is unavailable; use the `review` command.",
                thread_ts=context.action.thread_ts,
            )
            return
        await self._dispatch(
            repository=repository,
            pull_request=pull_request,
            model=model,
            channel=context.action.channel,
            thread_ts=context.action.thread_ts,
            requested_by=context.user_id,
            host=context.host,
            state_key=(context.action.channel, context.action.thread_ts),
            say=context.say,
        )

    async def _dispatch(
        self,
        *,
        repository: str,
        pull_request: PullRequestSummary,
        model: str,
        channel: str,
        thread_ts: str,
        requested_by: str | None,
        host: PluginHost,
        state_key: tuple[str, str],
        say: Any,
    ) -> None:
        if pull_request.state != "open":
            await say(text="Only open pull requests can be reviewed.", thread_ts=thread_ts)
            return
        job_id = uuid.uuid4().hex
        try:
            await self._client.dispatch(
                repository,
                {
                    "job_id": job_id,
                    "pull_request_number": str(pull_request.number),
                    "expected_head_sha": pull_request.head_sha,
                    "model": model,
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "requested_by": requested_by or "",
                },
                event_type=self._event_type,
            )
        except CodingJobError as exc:
            await say(text=str(exc), thread_ts=thread_ts)
            return
        await host.set_plugin_state(
            state_key,
            self.name,
            {
                "job_id": job_id,
                "repository": repository,
                "pull_request_number": pull_request.number,
                "head_sha": pull_request.head_sha,
                "model": model,
                "queued_at": datetime.now(UTC).isoformat(),
            },
        )
        await say(
            text=(
                f"Review job `{job_id}` queued for "
                f"`{repository}#{pull_request.number}` using `{model}`."
            ),
            thread_ts=thread_ts,
        )

    async def _status(self, context: CommandContext) -> None:
        state = await context.host.get_plugin_state(context.thread_key, self.name)
        if not isinstance(state, dict):
            await context.say(
                text="No pull request review is recorded for this thread.",
                thread_ts=context.thread_ts,
            )
            return
        job_id = state.get("job_id")
        repository = state.get("repository")
        if not isinstance(job_id, str) or not isinstance(repository, str):
            await context.say(
                text="The recorded pull request review is invalid.",
                thread_ts=context.thread_ts,
            )
            return
        try:
            run = await self._client.status(
                repository,
                job_id,
                workflow_filename=self._workflow_filename,
            )
        except CodingJobError as exc:
            await context.say(text=str(exc), thread_ts=context.thread_ts)
            return
        if run is None:
            text = f"Review job `{job_id}` is queued; its workflow run is not visible yet."
        else:
            outcome = run.conclusion or run.status
            text = f"Review job `{job_id}` is *{outcome}*: <{run.url}|workflow run>"
        await context.say(text=text, thread_ts=context.thread_ts)

    async def _validate_model(self, model: str) -> None:
        models = await self._inference_client.list_models()
        if model not in models:
            raise ValueError(f"Model `{model}` is not available")

    async def _require_admin(self, user_id: str | None, say: Any, thread_ts: str) -> bool:
        if self._policy is None:
            return True
        decision = self._policy.check_admin(user_id)
        if decision.allowed:
            return True
        await say(text=decision.message, thread_ts=thread_ts)
        return False


def _parse_prs(prompt: str) -> tuple[str, str, bool, int]:
    words = prompt.split()
    if len(words) < 2:
        raise CodingJobError(
            "Use `prs owner/repository [open|closed|all|draft] [page N]` or "
            "`prs https://github.com/owner/repository [open|closed|all|draft] [page N]`."
        )
    repository = _parse_repository_target(words[1])
    state = "open"
    drafts_only = False
    page = 1
    index = 2
    if index < len(words) and words[index].lower() in {"open", "closed", "all", "draft"}:
        selected = words[index].lower()
        drafts_only = selected == "draft"
        state = "open" if drafts_only else selected
        index += 1
    if index < len(words):
        if len(words) != index + 2 or words[index].lower() != "page":
            raise CodingJobError(
                "Use `prs owner/repository [open|closed|all|draft] [page N]` or "
                "`prs https://github.com/owner/repository "
                "[open|closed|all|draft] [page N]`."
            )
        try:
            page = int(words[index + 1])
        except ValueError as exc:
            raise CodingJobError("Pull request page must be a positive integer") from exc
    return repository, state, drafts_only, page


def _parse_review(words: list[str], default_model: str) -> tuple[str, int, str]:
    if len(words) not in {2, 4} or (len(words) == 4 and words[2].lower() != "model"):
        raise ValueError(
            "Use `review owner/repository#number [model MODEL]` or "
            "`review https://github.com/owner/repository/pull/number [model MODEL]`."
        )
    repository, raw_number = _parse_pull_request_target(words[1])
    try:
        number = int(raw_number)
    except ValueError as exc:
        raise ValueError("Pull request number must be a positive integer") from exc
    if number < 1:
        raise ValueError("Pull request number must be a positive integer")
    return repository, number, words[3] if len(words) == 4 else default_model


def _parse_pull_request_target(target: str) -> tuple[str, str]:
    target = _unwrap_slack_link(target)
    if "://" in target:
        parsed = urlsplit(target)
        if (
            parsed.scheme != "https"
            or parsed.netloc.lower() != "github.com"
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Pull request URL must use `https://github.com/owner/repository/pull/number`."
            )
        parts = parsed.path.strip("/").split("/")
        if len(parts) != 4 or parts[2] != "pull" or not parts[0] or not parts[1]:
            raise ValueError(
                "Pull request URL must use `https://github.com/owner/repository/pull/number`."
            )
        return f"{parts[0]}/{parts[1]}", parts[3]

    repository, separator, raw_number = target.rpartition("#")
    if not separator or not repository:
        raise ValueError("Pull request must use `owner/repository#number` or a full GitHub PR URL.")
    return repository, raw_number


def _parse_repository_target(target: str) -> str:
    target = _unwrap_slack_link(target)
    if "://" not in target:
        return target
    parsed = urlsplit(target)
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or parsed.query
        or parsed.fragment
    ):
        raise CodingJobError("Repository URL must use `https://github.com/owner/repository`.")
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise CodingJobError("Repository URL must use `https://github.com/owner/repository`.")
    return f"{parts[0]}/{parts[1]}"


def _unwrap_slack_link(target: str) -> str:
    if target.startswith("<") and target.endswith(">"):
        return target[1:-1].split("|", 1)[0]
    return target
