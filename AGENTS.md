# Contributor Guide

Slack Agent Runtime connects Slack Socket Mode to an OpenAI-compatible
inference API. Keep the reusable runtime provider-neutral. Do not add private
hostnames, organization names, internal headers, deployment paths, feature
flags, or real credentials.

## Code boundaries

All installable code belongs under `src/slack_agent_runtime/`.

- `domain/` contains pure commands, state, policy, and shared validation.
- `config.py` reads and validates environment configuration. Other modules must
  not read the environment directly.
- `inference/` defines the provider protocol, HTTP adapter, telemetry,
  opt-in cost estimation, and redacting structured logs.
- `slack/` owns listener registration, payload parsing, UI, history, delivery,
  and formatting.
- `plugins/` provides stable typed contracts, allowlisted entry-point
  discovery, route-conflict checks, lifecycle management, and failure
  isolation.
- `mcp/` owns strict server configuration, direct standard input/output
  (stdio) and Streamable HTTP sessions, tool discovery, bounded execution, and
  process-local approvals.
- `coding/` contains allowlisted GitHub dispatch/status access and bounded
  coding and review workers.
- `services/` separates conversation coordination, command execution,
  completion execution, and thread-state access. Services do not read the
  environment.
- `storage/` contains versioned thread snapshots, persistence contracts and
  adapters, and backend creation.
- `entrypoints/` contains local and server lifecycle entry points.

## Engineering rules

- Prefer constructor injection at network boundaries.
- Every new command needs parser and behavior tests.
- Every inference provider must implement both `InferenceClient.complete` and
  OpenAI-compatible `InferenceClient.stream_complete`. Do not branch on
  providers throughout the service.
- Keep streaming previews throttled and bounded. Store only final content in
  conversation history and feedback records.
- Plugin state must be JSON-compatible, namespaced by plugin name, and accessed
  through typed `PluginHost` methods.
- Treat installed plugins as trusted code. Never auto-load entry points or pass
  credentials through plugin configuration or logs.
- Model pricing may come only from the typed environment JSON mapping. Never
  embed or infer vendor pricing.
- OpenTelemetry Protocol configuration must use standard environment variables
  and remain safe when no exporter is configured.

## Credentials, logs, and user-visible errors

- Never commit real credentials.
- Keep tokens out of logs and error messages.
- Never attach prompts, messages, or completions to logs, spans, or metrics.
- User-visible failures must not include upstream response bodies.
- Persist conversation state only. Never persist runtime tasks, locks,
  credentials, or telemetry payloads.

## State and concurrency

- Policy counters are process-local. Do not imply that rate limits or token
  budgets are shared across replicas.
- Charge token budgets only from final reported inference usage.
- Active requests, cancellation handles, hydration locks, concurrency controls,
  and MCP approval waits remain process-local even when thread history and
  settings use durable persistence.

## MCP tools and external specifications

- Never log or persist MCP arguments or results.
- Keep model-facing MCP names namespaced and bounded.
- Truncate tool output before returning it to the model.
- Automatic execution requires both an explicit operator allowlist and the
  individual tool's `readOnlyHint` annotation.
- Unknown or write-capable tools require an app administrator's approval.
- Treat MCP servers as trusted code. MCP is not a sandbox.
- Retrieve external specifications inside the Slack runtime through a
  source-specific MCP tool allowlist and the existing approval broker.
- Require at least one successful source tool call before dispatch, bound the
  normalized result, and never send MCP credentials to a coding workflow.

## Coding jobs

- Confine resolved paths to the target checkout.
- Protect `.git`, `.github/workflows`, environment and secret files, and
  private keys.
- Keep command execution shell-free and allowlisted.
- The model-facing worker receives inference credentials only. It must never
  receive GitHub or Slack credentials, commit, push, or create pull requests.
- GitHub Actions must inject inference, GitHub, and Slack credentials into
  separate steps. A “later step” is a separate Actions step that starts after
  the untrusted AI worker process has exited.
- Generated pull requests always require human review and are never
  auto-merged.
- The bounded Actions runner is a containment boundary, not proof that hostile
  repository code is safe to execute.

## Pull-request reviews

- Treat titles, bodies, comments, code, and diffs as untrusted data.
- Keep review inference separate from the reviewer GitHub credential.
- Validate strict structured findings at the model-output boundary.
- Verify the exact head commit SHA again before publishing a review.
- Derive approve, comment, or request-changes behavior from finding severity in
  code. Never accept model-authored control text as the verdict.
- Never merge a pull request.

## Required validation

Before finishing a change, run:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```
