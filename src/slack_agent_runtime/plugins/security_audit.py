"""Admin-only orchestration for source-first repository security audits."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from slack_agent_runtime.coding.github import CodingJobError
from slack_agent_runtime.domain.commands import HelpEntry
from slack_agent_runtime.plugins.api import CommandContext, PluginRoute

if TYPE_CHECKING:
    from slack_agent_runtime.coding.github import GitHubCodingClient
    from slack_agent_runtime.domain.policy import InMemoryPolicy

_PROFILES = frozenset({"quick", "standard", "deep"})
_MIN_BUDGET = 8
_MAX_BUDGET = 64


class SecurityAuditPlugin:
    """Dispatch and track isolated Cloudflare security-audit-skill runs."""

    name = "security_audit"
    help_entries = (
        HelpEntry(
            "Security audits",
            (
                "security audit owner/repository or GitHub repository URL "
                "[profile quick|standard|deep] [budget N] [scope PATH]"
            ),
            "run a source-first security audit in GitHub Actions",
        ),
        HelpEntry(
            "Security audits",
            "security audit status",
            "show the latest security audit workflow status",
        ),
        HelpEntry(
            "Security audits",
            "security audit findings",
            "open the completed audit run and its report artifact",
        ),
    )

    def __init__(
        self,
        client: GitHubCodingClient,
        *,
        policy: InMemoryPolicy | None,
        event_type: str,
        workflow_filename: str,
    ) -> None:
        self._client = client
        self._policy = policy
        self._event_type = event_type
        self._workflow_filename = workflow_filename
        self.routes = (PluginRoute.command("security", self._security),)

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        await self._client.close()

    async def _security(self, context: CommandContext) -> None:
        if not await self._require_admin(context):
            return
        words = context.prompt.split()
        if len(words) == 3 and [word.lower() for word in words[:2]] == [
            "security",
            "audit",
        ]:
            action = words[2].lower()
            if action == "status":
                await self._status(context, findings=False)
                return
            if action == "findings":
                await self._status(context, findings=True)
                return
        try:
            repository, profile, budget, scope = _parse_audit(words)
        except (CodingJobError, ValueError) as exc:
            await context.say(text=str(exc), thread_ts=context.thread_ts)
            return
        job_id = uuid.uuid4().hex
        try:
            await self._client.dispatch(
                repository,
                {
                    "job_id": job_id,
                    "profile": profile,
                    "budget": str(budget),
                    "scope": scope,
                    "channel": context.thread_key[0],
                    "thread_ts": context.thread_ts,
                    "requested_by": context.user_id or "",
                },
                event_type=self._event_type,
            )
        except CodingJobError as exc:
            await context.say(text=str(exc), thread_ts=context.thread_ts)
            return
        await context.host.set_plugin_state(
            context.thread_key,
            self.name,
            {
                "job_id": job_id,
                "repository": repository,
                "profile": profile,
                "budget": budget,
                "scope": scope,
                "queued_at": datetime.now(UTC).isoformat(),
            },
        )
        await context.say(
            text=(
                f"Security audit `{job_id}` queued for `{repository}` with "
                f"profile `{profile}`, budget `{budget}`, and scope `{scope}`."
            ),
            thread_ts=context.thread_ts,
        )

    async def _status(self, context: CommandContext, *, findings: bool) -> None:
        state = await context.host.get_plugin_state(context.thread_key, self.name)
        if not isinstance(state, dict):
            await context.say(
                text="No security audit is recorded for this thread.",
                thread_ts=context.thread_ts,
            )
            return
        job_id = state.get("job_id")
        repository = state.get("repository")
        if not isinstance(job_id, str) or not isinstance(repository, str):
            await context.say(
                text="The recorded security audit is invalid.",
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
            text = f"Security audit `{job_id}` is queued; its workflow run is not visible yet."
        elif findings and run.conclusion == "success":
            text = (
                f"Security audit `{job_id}` completed: "
                f"<{run.url}|open the workflow run and download its report artifact>."
            )
        elif findings:
            outcome = run.conclusion or run.status
            text = (
                f"Security audit `{job_id}` has no completed report yet; "
                f"its workflow is *{outcome}*: <{run.url}|workflow run>."
            )
        else:
            outcome = run.conclusion or run.status
            text = f"Security audit `{job_id}` is *{outcome}*: <{run.url}|workflow run>."
        await context.say(text=text, thread_ts=context.thread_ts)

    async def _require_admin(self, context: CommandContext) -> bool:
        if self._policy is None:
            return True
        decision = self._policy.check_admin(context.user_id)
        if decision.allowed:
            return True
        await context.say(text=decision.message, thread_ts=context.thread_ts)
        return False


def _parse_audit(words: list[str]) -> tuple[str, str, int, str]:
    usage = (
        "Use `security audit owner/repository [profile quick|standard|deep] "
        "[budget N] [scope PATH]`."
    )
    if len(words) < 3 or [word.lower() for word in words[:2]] != ["security", "audit"]:
        raise ValueError(usage)
    repository = _parse_repository_target(words[2])
    profile = "standard"
    budget = 24
    scope = "."
    seen: set[str] = set()
    index = 3
    while index < len(words):
        option = words[index].lower()
        if option not in {"profile", "budget", "scope"} or option in seen:
            raise ValueError(usage)
        if index + 1 >= len(words):
            raise ValueError(usage)
        value = words[index + 1]
        seen.add(option)
        if option == "profile":
            profile = value.lower()
            if profile not in _PROFILES:
                raise ValueError("Security audit profile must be quick, standard, or deep.")
        elif option == "budget":
            try:
                budget = int(value)
            except ValueError as exc:
                raise ValueError(
                    f"Security audit budget must be {_MIN_BUDGET}-{_MAX_BUDGET}."
                ) from exc
            if not _MIN_BUDGET <= budget <= _MAX_BUDGET:
                raise ValueError(f"Security audit budget must be {_MIN_BUDGET}-{_MAX_BUDGET}.")
        else:
            scope = _validate_scope(value)
        index += 2
    return repository, profile, budget, scope


def _parse_repository_target(target: str) -> str:
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].split("|", 1)[0]
    if "://" not in target:
        return target
    parsed = urlsplit(target)
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Repository URL must use `https://github.com/owner/repository`.")
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError("Repository URL must use `https://github.com/owner/repository`.")
    return f"{parts[0]}/{parts[1]}"


def _validate_scope(value: str) -> str:
    if not value or len(value) > 200 or "\\" in value or "\x00" in value:
        raise ValueError("Security audit scope must be a repository-relative path.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Security audit scope must be a repository-relative path.")
    return str(path)
