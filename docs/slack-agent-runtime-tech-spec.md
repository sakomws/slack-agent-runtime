# Slack Agent Runtime — Technical Specification

**Author:** Sako M

**Created:** July 3, 2026

## Overview

This specification is for software engineers, platform teams, security
reviewers, and operators evaluating or extending Slack Agent Runtime. Its
purpose is to define the product boundary, implemented architecture, security
model, acceptance criteria, rollout work, and unresolved decisions for turning
Slack threads into reviewed GitHub pull requests.

**Document Status:** Draft

**Owner:** Sako M

**Reviewers:** Not assigned

**Related Epic:** Not created

**Last Updated:** November 6, 2026

### Recommendation

Continue developing Slack Agent Runtime as a Slack-focused, provider-neutral
engineering harness. Keep model-facing work separated from GitHub and Slack
credentials, require human merge approval, and postpone multi-channel and
multi-replica operation until a concrete adoption requirement justifies those
changes.

### Facts

- The released package version is `0.1.0`.
- The runtime supports Python 3.11 and 3.12.
- Slack Socket Mode is the only interaction transport.
- The inference boundary is an OpenAI-compatible API. The current deployment
  uses You.com for inference and search.
- MCP servers provide governed access to Notion, Linear, Slack, GitHub, and
  You.com tools when configured.
- GitHub Actions executes bounded coding, pull-request review, and source-only
  security-audit workers.
- GitHub stores branches, commits, diffs, reviews, and pull requests.
- Generated pull requests require human review and are never auto-merged.

### Assumptions

- Operators control the Slack app, MCP configuration, GitHub workflows, and
  repository allowlists.
- Target repositories and installed MCP servers are trusted. GitHub Actions
  and MCP are containment boundaries, not sandboxes for hostile code.
- One runtime process serves one Slack app.
- Production repositories enforce branch protection and required checks
  independently of this runtime.

### Decisions

- Slack remains in the project name and product scope for the `0.x` series.
- The reusable runtime remains inference-provider-neutral.
- Credentials remain separated by workflow step and responsibility.
- Durable storage may persist thread state, but active tasks, approvals,
  locks, and policy counters remain process-local.
- Automatic MCP execution requires both the tool's original name in the
  server's `autoApprove` list and the individual tool's
  `readOnlyHint: true` annotation.

## Background

### What

Slack Agent Runtime is an asynchronous Python service that connects Slack
Socket Mode to an OpenAI-compatible inference endpoint. It supports threaded
conversation, model and generation controls, direct MCP tools, specification
review, GitHub coding jobs, independent pull-request reviews, and skill-driven
source security audits.

The system uses Slack as the human interaction harness, MCP as the governed
data layer, GitHub Actions as bounded execution, and GitHub as the durable
code-artifact layer.

### Why

Chat interfaces are easy to build, but engineering actions require stronger
boundaries. A useful engineering agent must retain thread context, retrieve
source documents, request approval for tools, change code, run validation,
publish a pull request, and report results without exposing broad credentials
to an untrusted model process.

The project addresses that problem by separating conversation, data access,
model execution, credentialed publication, and human approval.

## Goals

### In Scope

- Direct messages and `@app` mentions through Slack Socket Mode.
- Streaming, multi-turn inference with bounded thread history.
- Per-thread model, search, generation, and display settings.
- Operator-controlled access, administrator, quota, and concurrency policy.
- Durable thread state through memory, SQLite, Redis, or PostgreSQL.
- Direct stdio and Streamable HTTP MCP servers with approval controls.
- Grounded Notion specification review and coding-job dispatch.
- Allowlisted GitHub coding jobs that create reviewable pull requests.
- Independent pull-request review with deterministic publication policy.
- Source-only security audits using a pinned Cloudflare skill, fresh model
  contexts, validated findings, and GitHub Actions report artifacts.
- Typed plugin contracts and explicit plugin loading.
- Local, Docker, Compose, and single-replica Helm deployment.
- Structured logs, OpenTelemetry traces and metrics, release artifacts,
  checksums, SBOMs, signatures, and provenance attestations.

### Success Criteria

The implementation is acceptable when all of the following remain true:

- A Slack message receives a final response in the originating thread.
- Thread history never exceeds 20 messages or 50,000 characters.
- An MCP tool runs automatically only when it is explicitly allowlisted and
  marked read-only by its server.
- A coding worker cannot read protected secret paths, modify workflow files,
  commit, push, create a pull request, or receive GitHub or Slack credentials.
- Coding output is rejected when it contains no changes, exceeds 50 changed
  files, or exceeds 500,000 diff characters.
- Pull-request publication rechecks the exact head SHA before publishing.
- Self-authored approvals and change requests are published as comments rather
  than failing or bypassing GitHub policy.
- No workflow automatically merges a pull request.
- Ruff, formatting, mypy, pytest, dependency audit, Helm validation, and
  container checks pass in CI.
- User-visible errors exclude credentials and upstream response bodies.

## Non-Goals

### Out of Scope

- Microsoft Teams, Discord, web, or other interaction adapters in `0.1`.
- Horizontal scaling or shared coordination across runtime replicas.
- Executing code from hostile repositories without stronger runner isolation.
- Treating MCP servers or installed plugins as untrusted code.
- Replacing repository branch protection, required CI, or human merge review.
- Autonomous merge, deployment, or production rollback.
- A hosted multi-tenant control plane.
- Organization-specific ownership, incident response, or compliance policy.

## Requirements

### Functional Requirements

#### Slack interaction

- Accept direct messages and app mentions without a public inbound webhook.
- Preserve channel and thread identity across replies.
- Ignore bot-generated events and normalize Slack mention markup.
- Provide commands and interactive controls for models, generation,
  cancellation, regeneration, history, and feedback.
- Throttle streaming previews and split final responses at Slack-safe
  boundaries.
- Run only one process per Slack app because Socket Mode distributes events
  among active connections.

#### Inference

- Discover available models from `GET /v1/models`.
- Support non-streaming and OpenAI-compatible streaming chat completions.
- Keep provider selection behind the `InferenceClient` protocol.
- Charge token budgets from the final result delivered to the coordinator.
  For MCP runs, that final result aggregates usage reported across all model
  rounds.
- Support server-side web search without embedding provider-specific logic
  throughout the service.

#### State and policy

- Scope settings and history to a Slack channel and thread.
- Allow memory, SQLite, Redis, and PostgreSQL thread-state backends.
- Validate and version snapshots before restore.
- Permit only one active request per thread.
- Apply configured workspace, channel, user, repository, and administrator
  allowlists.
- Keep rate limits, concurrency controls, active tasks, locks, and approval
  waits process-local.

#### MCP data access

- Load MCP servers only from an explicitly referenced JSON configuration.
- Support stdio and Streamable HTTP transports.
- Namespace model-facing tool names by server.
- Bound tool rounds, execution time, approval time, schema size, and result
  size. Current MCP defaults are eight rounds, 30 seconds per tool call,
  300 seconds for approval, and 20,000 result characters.
- Keep tool arguments and results out of buttons, durable state, logs, traces,
  and metrics.
- Require administrator approval for write-capable, unknown, or non-allowlisted
  tools.
- Require at least one successful Notion tool call before dispatching a
  Notion-grounded coding job.
- Keep MCP credentials inside the Slack runtime.

#### Coding jobs

- Dispatch jobs only to repositories in
  `SLACK_AGENT_RUNTIME_CODING_REPOSITORIES`.
- Use GitHub Actions as the durable job-status source.
- Give the model-facing worker inference credentials only.
- Restrict worker commands to an executable allowlist and read-only Git
  subcommands.
- Protect `.git`, `.github/workflows`, environment files, secret files, and
  private keys.
- Enforce current coding-worker defaults of 48 model/tool rounds, 20 command
  executions, 50 changed files, 500,000 diff characters, and a 30-minute
  worker timeout.
- Perform commit, push, pull-request creation, and Slack notification only in
  later credentialed Actions steps after the worker exits.

#### Pull-request reviews

- Treat pull-request titles, bodies, comments, code, and diffs as untrusted.
- Review the exact requested head SHA and verify it again before publication.
- Validate structured findings before deriving the review event.
- Map critical or high findings to `REQUEST_CHANGES`.
- Map medium or low findings, or no findings, to `APPROVE` only when required
  checks pass.
- Downgrade approvals for drafts or unsuccessful checks to `COMMENT`.
- Downgrade self-authored `APPROVE` and `REQUEST_CHANGES` events to `COMMENT`
  and explain the reason in GitHub and Slack.
- Never merge a pull request.

#### Plugins

- Always load core plugins. Load optional built-ins according to the built-in
  selector, then load explicitly selected external plugins.
- Reject duplicate plugin names, route conflicts, and invalid namespaces.
- Provide typed contexts and namespaced JSON-compatible plugin state.
- Roll back started plugins when lifecycle startup fails.
- Treat selected plugins as trusted Python code.

### Non-Functional Requirements

#### Security

- Never commit real credentials.
- Redact known secret values and sensitive fields from structured logs.
- Never attach prompt or completion bodies, Slack message bodies, MCP
  arguments, or MCP results to logs, traces, or metrics. Token counts and other
  bounded operational metadata may be recorded.
- Use least-privilege, purpose-specific GitHub and Slack credentials.
- Keep reusable workflow references pinned to a release tag or commit in
  production installations.
- Run containers as a non-root user with restricted security contexts.

#### Reliability

- Fail startup when required configuration is missing or malformed.
- Close network clients, MCP sessions, persistence backends, and plugin
  lifecycles during shutdown.
- Preserve durable thread snapshots across process restarts.
- Report durable coding and review status from GitHub Actions after Slack
  runtime restarts.
- Keep final Slack messages readable through Markdown conversion and bounded
  chunking.

#### Performance and limits

- Throttle Slack progress updates to one second by default.
- Bound history, prompts, MCP output, command output, changed files, and diffs.
- Limit each worker command to five minutes inside the 30-minute worker
  timeout.
- Limit the complete coding Actions job to 35 minutes.
- Avoid loading or persisting unbounded conversation or tool data.

#### Observability

- Emit structured JSON logs.
- Export inference request, error, latency, token, model, search, and optional
  cost telemetry through standard OpenTelemetry configuration.
- Remain operational when no telemetry exporter is configured.
- Correlate user-visible usage with the inference gateway request ID without
  exposing provider internals.

#### Testability and compatibility

- Use constructor injection at network boundaries.
- Require parser and behavior tests for every new or changed command.
- Maintain strict type checking and formatting.
- Support Python 3.11 and 3.12.
- Keep event fixtures for representative Slack interactions.
- Validate Helm rendering and non-root container behavior in CI.

## Proposed Solution

Retain the existing five-part architecture:

1. **Slack interaction harness:** captures identity, channel, thread,
   approvals, commands, and notifications.
2. **Slack Agent Runtime:** coordinates policy, state, inference, plugins, MCP,
   coding dispatch, and review dispatch.
3. **MCP data layer:** provides approved access to external sources including
   Notion, Linear, Slack, GitHub, and You.com.
4. **GitHub Actions bounded execution:** runs coding, review, or source-audit
   workers with finite rounds, agent calls, commands, file changes, diff size,
   and time as applicable.
5. **GitHub code-artifact layer:** stores commits, branches, pull requests,
   reviews, checks, and durable job status.

The detailed architecture image is maintained in
[`slack-engineering-agent-architecture.svg`](images/slack-engineering-agent-architecture.svg).

## Approach

### Components

The Python package layout under `src/slack_agent_runtime/` is:

- `domain/`: commands, thread state, policy, and shared validation.
- `config.py`: environment loading and validation.
- `inference/`: provider protocol, OpenAI-compatible client, telemetry, and
  cost estimation.
- `slack/`: Bolt listeners, payload parsing, history, formatting, delivery,
  and Block Kit UI.
- `services/`: conversation, command, completion, agent-loop, and thread-state
  coordination.
- `mcp/`: configuration, sessions, discovery, tool bounds, and approvals.
- `coding/`: GitHub dispatch, bounded workspace tools, coding worker,
  structured reviewer, and source-only security-audit orchestrator.
- `plugins/`: typed plugin contracts, registry, lifecycle, and built-ins.
- `storage/`: versioned persistence contracts and adapters.
- `entrypoints/`: CLI, coding worker, reviewer, security-audit worker, and ASGI
  lifecycles.

### Conversation flow

1. Slack sends a Socket Mode event.
2. The runtime validates policy and normalizes the prompt.
3. The runtime restores bounded thread state.
4. A command, plugin, MCP agent loop, or inference completion handles the
   request.
5. Slack receives throttled progress and a formatted final response.
6. Only final conversation content and settings are persisted.

### Coding flow

1. An administrator submits an allowlisted repository and request in Slack.
2. The runtime dispatches a validated `repository_dispatch` event.
3. GitHub Actions checks out the target repository and runtime.
4. The coding worker receives the prompt and inference credentials.
5. The worker edits within the checkout and runs bounded validation.
6. A later step validates the diff, commits, pushes, and creates a pull
   request with GitHub credentials.
7. Another later step posts the result to the originating Slack thread.

### Review flow

1. An administrator selects a pull request and model.
2. The runtime records the exact head SHA and dispatches a review workflow.
3. The inference-only reviewer emits structured findings.
4. A credentialed publisher rechecks the head SHA and repository state.
5. Deterministic code derives `APPROVE`, `COMMENT`, or `REQUEST_CHANGES`.
6. GitHub receives the review and Slack receives the result or failure reason.

### Security-audit flow

1. An administrator selects an allowlisted repository, profile, agent budget,
   and optional repository-relative scope.
2. The runtime dispatches a security-audit workflow and stores only its durable
   pointer in thread state.
3. GitHub Actions checks out the target, runtime, and the Cloudflare
   security-audit skill pinned to an exact commit.
4. An inference-only parent follows the six skill phases and delegates
   independent work to fresh read-only model contexts.
5. The worker exposes source and skill reading plus canonical report writes,
   but no target-code execution or repository-write tool.
6. Later trusted steps run both upstream validators, reject secret-like output,
   upload the seven canonical reports, and post the workflow link to Slack.

## Pros

- Keeps Slack interaction close to existing engineering collaboration.
- Separates model execution from GitHub and Slack credentials.
- Supports multiple inference providers through one protocol.
- Gives external data access an explicit discovery and approval boundary.
- Produces normal GitHub artifacts that fit existing review and CI processes.
- Preserves human merge authority.
- Offers local development and production packaging without requiring an
  inbound Slack webhook.

## Cons

- Single-process Socket Mode prevents horizontal availability.
- Process-local approvals and policy counters reset during restart.
- MCP servers and plugins remain trusted-code dependencies.
- Coding jobs can execute target-repository code through allowed runtimes.
- Model severity classification can produce incorrect review verdicts.
- Operators must configure Slack, GitHub, inference, MCP, and repository
  workflows correctly.
- The current health endpoint does not prove Slack, inference, or MCP
  reachability.

## Alternatives Considered

### Alternative 1: Generalize to multiple interaction channels now

Extract a channel-neutral adapter interface and ship Slack, Microsoft Teams,
Discord, CLI, and web adapters together.

#### Pros

- Broadens the potential user base.
- Forces channel-neutral interfaces early.

#### Cons

- Multiplies authentication, identity, threading, formatting, approval, and
  test requirements before another channel has a confirmed adopter.
- Dilutes the current Slack-specific product hook.

#### Decision

Do not implement now. Keep new core services channel-neutral where practical
and extract an adapter when a concrete second-channel requirement exists.

### Alternative 2: Run the entire agent inside GitHub Actions

Use Slack only to dispatch workflows and receive final notifications.

#### Pros

- Makes every action durable in GitHub.
- Reduces long-running local runtime responsibilities.

#### Cons

- Weakens interactive streaming, cancellation, approvals, and thread state.
- Sends more conversation and data-source context into repository-visible
  Actions metadata.
- Increases latency and workflow usage for ordinary chat.

#### Decision

Reject. Keep conversation and MCP access in the Slack runtime; use Actions for
bounded code and review work.

## Risks

- **Duplicate Socket Mode processes:** Slack may route commands to an instance
  with different configuration. Mitigation: operate one process per Slack app
  and fail deployments that request multiple replicas.
- **Credential leakage:** Logs or worker environments could expose secrets.
  Mitigation: redaction, separate workflow steps, protected paths, and no
  message or completion telemetry.
- **Prompt injection through external data:** Notion pages, MCP output, and
  pull-request content can contain instructions. Mitigation: treat external
  content as untrusted, restrict tools by source, require approvals, and derive
  control decisions in code.
- **Unsafe repository execution:** Allowed test commands can execute repository
  code. Mitigation: use trusted repositories, isolated runners, strict
  allowlists, finite time, and human review.
- **Incorrect automated review:** Model findings or severity may be wrong.
  Mitigation: required checks, deterministic publication rules, branch
  protection, and human merge review.
- **Provider or API drift:** Slack, inference, MCP, and GitHub behavior may
  change. Mitigation: pinned dependencies, contract validation, event fixtures,
  and release CI.
- **Single-instance outage:** A process or host failure interrupts active work.
  Mitigation: durable thread state and GitHub job status; shared active-work
  coordination remains future work.

## Rollout Plan

### Owner

Sako M owns the draft, implementation decisions, and release coordination.
Production operations, security review, and incident-response owners are not
yet assigned.

### Timeline

No production deadline is approved. Rollout should proceed through these
ordered phases:

1. **Local validation:** run Slack chat, MCP approval, Notion review, coding,
   and pull-request review flows against test resources.
2. **Trusted repository pilot:** enable one repository with branch protection,
   required checks, and separate coding and reviewer identities.
3. **Operational hardening:** configure durable storage, OpenTelemetry,
   backups, runbooks, and secret rotation.
4. **Production review:** obtain platform and security approval before adding
   more repositories or users.
5. **Controlled expansion:** add repositories, MCP servers, and users through
   explicit allowlist changes.

## Execution Plan

- Create an epic for production readiness and link it from this document.
- Assign platform, security, and operations reviewers.
- Pin target-repository reusable workflows to an immutable release tag or
  commit.
- Define approved Slack workspaces, channels, users, and administrators.
- Define approved GitHub repositories and separate coding and reviewer
  credentials.
- Define approved MCP servers and per-tool `autoApprove` policy.
- Run end-to-end tests for chat, Notion review, coding, review publication,
  cancellation, restart recovery, and Slack failure reporting.
- Add deeper readiness checks for Slack, inference, persistence, and MCP.
- Write deployment, rollback, credential-rotation, and incident-response
  runbooks.
- Complete a security review before production expansion.
- Publish a release only after CI, image, Helm, SBOM, signature, and provenance
  checks pass.

## Open Questions

- Who are the required platform, security, and operations reviewers?
- Which epic will track production readiness?
- What is the target pilot date?
- Which Slack workspace, channels, users, and administrators are approved?
- Which repositories are approved for coding and review jobs?
- Which MCP servers and read-only tools may run automatically?
- Is single-instance operation acceptable for the first production rollout?
- What availability target would justify shared coordination and horizontal
  scaling?
- Should automated pull-request approval remain enabled, or should production
  reviews be comment-only?
- What runner isolation is required for repositories outside the trusted set?
- Which durable state backend and backup policy will production use?
- When should a second interaction adapter such as Microsoft Teams be
  considered?

## References

- [README](../README.md)
- [Contributor guide](../AGENTS.md)
- [30-minute architecture talk](slack-agent-runtime-30-minute-talk.md)
- [MCP examples](../examples/mcp/README.md)
- [Coding-job setup](../examples/coding-job/README.md)
- [Helm deployment](../helm/slack-agent-runtime/README.md)
- [Release workflow](../.github/workflows/release.yml)
