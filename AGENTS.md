# Contributor Guide

This project is a Slack Socket Mode adapter for an OpenAI-compatible inference
API. Keep it provider-neutral: do not add private hostnames, organization names,
internal headers, deployment paths, feature flags, or real credentials.

## Boundaries

All installable code lives under `src/slack_agent_runtime/`.

- `domain/`: pure commands, state, policy, and shared validation.
- `config.py`: environment loading only.
- `inference/`: provider protocol, HTTP adapter, and provider-neutral
  telemetry, opt-in cost estimation, and
  redacting structured logging. Never attach prompts, messages, or completions
  to logs, spans, or metrics.
- `slack/`: listener registration, payload parsing, UI, history, delivery, and
  formatting.
- `plugins/`: stable typed plugin contracts, allowlisted entry-point
  discovery, route conflicts, lifecycle, and failure isolation.
- `mcp/`: strict server configuration, direct stdio/Streamable HTTP sessions,
  tool discovery, bounded execution, and process-local approvals.
- `coding/`: allowlisted GitHub dispatch/status access and the bounded coding
  worker. The worker receives only inference credentials; it must never commit,
  push, create PRs, or receive GitHub/Slack credentials.
- `services/`: separate conversation coordination, command execution,
  completion execution, and thread-state access; no environment reads.
- `storage/`: versioned thread snapshots, persistence protocol/adapters,
  and backend creation. Persist conversation state only; never runtime tasks,
  locks, credentials, or telemetry payloads.
- `entrypoints/`: local and server lifecycle entry points.

Prefer constructor injection at network boundaries. Keep tokens out of logs and
error messages. User-visible failures must not include upstream response bodies.
Every new command needs parser tests and behavior tests. Every new provider
should implement both `InferenceClient.complete` and OpenAI-compatible
`InferenceClient.stream_complete` instead of branching throughout the service.
Policy state is process-local: do not imply that limits are shared between bot
replicas. Only final reported inference usage may be charged to token budgets.
Active requests, cancellation, hydration locks, and concurrency controls remain
process-local even when thread history/settings use durable persistence.
Plugin state must be JSON-compatible, namespaced by plugin name, and accessed
through the typed `PluginHost` methods. Treat installed plugins as trusted
code: never auto-load entry points, and never pass credentials through plugin
configuration or logs.
Keep streaming previews throttled and bounded; only final content belongs in
conversation history and feedback records.
Model pricing must come only from the typed environment JSON mapping; never
embed or infer vendor pricing. OTLP configuration must use standard
OpenTelemetry environment variables and remain safe when no exporter is set.
Never log or persist MCP arguments or results. Model-facing MCP names must stay
namespaced and bounded, and tool output must be truncated before returning it
to the model. Automatic execution requires both an explicit operator allowlist
and the server's `readOnlyHint`; unknown and write-capable tools require an app
administrator. Treat MCP servers as trusted code and do not describe MCP as a
sandbox. In-flight approval futures remain process-local.
Coding tools must confine resolved paths to the target checkout and protect
`.git`, `.github/workflows`, environment/secret files, and private keys. Keep
command execution shell-free and allowlisted. GitHub Actions must inject
inference, GitHub, and Slack credentials into separate steps; generated pull
requests always require human review and are never auto-merged.
External specifications must be retrieved in the Slack runtime through a
source-specific MCP tool allowlist and existing approval broker. Require a
successful source tool call before dispatch, bound the normalized result, and
never send MCP credentials to a coding workflow.
PR review content is untrusted data. Keep review inference separate from the
reviewer GitHub credential, validate strict structured findings, verify the
exact head SHA again before publication, and derive approve/request-changes
from severity in code rather than model-authored control text. Never merge.

Before finishing a change, run:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```
