<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/slack-agent-runtime-wordmark-dark.svg">
    <img src="docs/images/slack-agent-runtime-wordmark-light.svg" alt="Slack Agent Runtime: the keyboard, not the keys. Logo of a horse in a harness whose reins lead to a terminal key." width="620">
  </picture>
</p>

> Turn Slack threads into review-ready pull requests—without giving the AI
> your GitHub credentials.

This service connects Slack to the [You.com Inference Router](https://you.com/product/inference-router) using Slack Socket Mode. It
supports direct messages and `@app` mentions, sends each message as a
multi-turn streaming chat completion, and updates the originating Slack thread
as model output arrives.

It runs entirely over outbound connections, so a laptop needs no public URL,
tunnel, webhook, or cloud deployment.

## Architecture at a glance

Slack is the interaction harness. MCP provides governed access to data sources
such as Notion specifications, Linear work items, and Slack data. One
OpenAI-compatible endpoint provides inference and search; the current deployment
uses You.com for that endpoint. GitHub Actions runs bounded coding and review
workers, while GitHub stores code artifacts such as branches, diffs, pull
requests, and reviews.

<p align="center">
  <img src="docs/images/slack-engineering-agent-architecture.svg" alt="Slack Engineering Agent Architecture: the Slack harness calls the Agent Runtime, which uses a You.com inference-and-search endpoint, thread state, and an MCP approval broker over Notion, Linear and Slack data, and dispatches bounded workers to GitHub Actions" width="100%">
</p>

<details>
<summary>Mermaid source</summary>

```mermaid
flowchart LR
    U[Engineer] --> S[Slack interaction harness]
    S --> R[Slack Agent Runtime]
    R --> I[You.com OpenAI-compatible inference-and-search endpoint]
    R --> T[(Thread state)]
    R --> M[MCP approval broker and data layer]
    M --> N[Notion specifications]
    M --> L[Linear work items]
    M --> SD[Slack data]
    R -->|repository_dispatch| A[GitHub Actions bounded execution]
    A -->|inference credentials only| W[Bounded coding or review worker]
    W -->|validated output| A
    A -->|GitHub credential after worker exits| G[GitHub code artifacts]
    A -->|Slack credential after worker exits| S
```

</details>

The model-facing worker receives inference credentials only. GitHub and Slack
credentials are scoped to separate later Actions steps after the worker exits.
The Actions runner provides bounded execution, not a security sandbox for
hostile repository code.

See the [technical specification](docs/slack-agent-runtime-tech-spec.md) for
requirements, decisions, risks, and rollout work. The
[30-minute talk](docs/slack-agent-runtime-30-minute-talk.md) provides a guided
architecture walkthrough.

## Install

Requires Python 3.11+ and an OpenAI-compatible endpoint that implements
streaming `POST /v1/chat/completions` responses and `GET /v1/models`.

First create local configuration:

```bash
cp .env.example .env
# Fill in .env with your Slack and inference credentials.
```

### uv

Recommended for development:

```bash
uv sync --extra dev
uv run slack-agent --check
uv run slack-agent
```

### pip

Install an editable development environment:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
slack-agent --check
slack-agent
```

Install the package without development tools:

```bash
pip install .
slack-agent
```

SQLite persistence uses the standard library and needs no extra dependency.
Install the driver for an external state backend when needed:

```bash
pip install '.[redis]'       # Redis only
pip install '.[postgres]'    # PostgreSQL only
pip install '.[persistence]' # Both external backends
```

### Wheel or source archive

```bash
uv build
pip install dist/slack_agent_runtime-*.whl
```

The wheel can be uploaded to a private Python registry or PyPI. This project is
not assumed to be published to either registry.

### Docker

```bash
docker build -t slack-agent-runtime:local .
docker run --rm --env-file .env -p 127.0.0.1:6016:6016 \
  slack-agent-runtime:local
```

### Docker Compose

```bash
docker compose up --build
```

Use another ignored environment file with:

```bash
ENV_FILE=.env.local docker compose up --build
```

For MCP, add a local override so the ignored config is mounted read-only:

```yaml
# compose.override.yaml
services:
  bot:
    environment:
      SLACK_AGENT_RUNTIME_MCP_CONFIG: /run/config/mcp.json
    volumes:
      - ./mcp.json:/run/config/mcp.json:ro
```

### Kubernetes with Helm

See [`helm/slack-agent-runtime/README.md`](helm/slack-agent-runtime/README.md)
for secret creation and installation commands.

## Run locally

After installing with pip:

```bash
slack-agent --check
slack-agent
```

Keep the process running, then send the app a direct message or mention it in a
channel. Press `Ctrl+C` to stop it cleanly. Only run one process per Slack app:
Socket Mode distributes events across all active connections for the same app.

## Commands

### Models and search

- `model [name|default]` — list, select, or reset the model
- `web [on|off]` — show or change server-side web search

### Generation

- `stream [on|off]` — show or change incremental response streaming
- `temperature [0-2|default]` — sampling randomness
- `top-p [0-1|default]` — token diversity
- `max-tokens [1-1000000|default]` — response limit
- `reasoning [level|default]` — reasoning effort; supported levels are `none`,
  `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`

### Thread actions

- `regenerate` — retry the latest prompt
- `cancel` — stop the active request
- `delete last` — remove the latest bot response; repeat for earlier responses
- `clear` — clear thread history and settings

### Settings and information

- `settings` — show current thread settings
- `settings default` — reset settings while preserving history
- `usage [on|off]` — show request and token details
- `help` — show commands

### MCP tools

- `tools` — list available MCP tools and their approval behavior
- `mcp status` — show MCP connection and tool counts
- `mcp servers` — list connected MCP servers

### Coding jobs

- `code owner/repository <request>` — start an allowlisted GitHub coding job
- `code owner/repository from notion <page URL>` — retrieve a grounded Notion
  specification through MCP, then start a coding job
- `code status` — show the latest coding job's durable Actions status

### Specification review

- `spec review from notion <page URL>` — review and rewrite a grounded Notion
  specification for clarity, completeness, ownership, and next steps

### Pull requests

- `prs owner/repository [open|closed|all|draft] [page N]` or `prs https://github.com/owner/repository [open|closed|all|draft] [page N]` — list pull requests with review buttons
- `review owner/repository#number [model MODEL]` or `review https://github.com/owner/repository/pull/number [model MODEL]` — run an independent PR review
- `review status` — show the latest review workflow status

All settings are scoped per thread. `default` removes an override.
Anyone may inspect settings and available models. Changing the model, web
search, streaming, usage display, or generation parameters requires a user ID listed in
`SLACK_AGENT_RUNTIME_ADMIN_USER_IDS`.

## Interactive controls

While a request runs, its progress message includes a **Cancel** button.
Completed responses include buttons to regenerate, toggle web search or streaming, choose a
model, clear the thread, and submit positive or negative feedback. **Model**
opens a searchable Slack modal and applies the selection only to that thread.
Text commands remain available as an accessible fallback.

## Request flow

```mermaid
sequenceDiagram
    actor User as Slack user
    participant Slack
    participant Bot as Slack Agent Runtime
    participant Router as Inference Router
    participant Provider as Model provider

    User->>Slack: Send DM or mention bot
    Slack->>Bot: Deliver Socket Mode event
    Bot->>Bot: Ignore bot events and normalize prompt
    Bot->>Router: POST /v1/chat/completions with stream=true
    Note over Bot,Router: Bearer API key
    Router->>Router: Authenticate and enforce model access
    Router->>Provider: Request completion from selected model
    Provider-->>Router: Stream answer deltas
    Router-->>Bot: Forward OpenAI-compatible SSE events
    Bot-->>Slack: Throttle updates to one progress message
    Bot-->>Slack: Replace with final chunked response
    Slack-->>User: Display answer
```

## Slack app setup

1. Create a Slack app from `slack-manifest.yaml`, or configure the following
   settings manually.
2. Create an app-level token with the `connections:write` scope.
3. Add these bot token scopes:
   - `app_mentions:read`
   - `chat:write`
   - `channels:history`
   - `groups:history`
   - `im:history`
   - `mpim:history`
   - `reactions:read`
4. Subscribe to the `app_mention`, `message.im`, and `reaction_added` bot
   events.
5. Install the app to the workspace and invite it to channels where mentions
   should work.
6. Copy the bot token (`xoxb-...`) and app token (`xapp-...`) into `.env`.

Reinstall the app after changing scopes or event subscriptions.

## Configuration

These core values are required:

- `SLACK_BOT_TOKEN`: Slack bot token beginning with `xoxb-`.
- `SLACK_APP_TOKEN`: Socket Mode app token beginning with `xapp-`.
- `INFERENCE_ROUTER_BASE_URL`: base URL of the OpenAI-compatible API.
- `INFERENCE_ROUTER_API_KEY`: bearer token for that API.
- `INFERENCE_ROUTER_MODEL`: initial model ID returned by `/v1/models`.

Optional in-memory policy settings are documented in `.env.example`.
Workspace, channel, and user allowlists are comma-separated Slack IDs; an empty
allowlist is unrestricted. `SLACK_AGENT_RUNTIME_ADMIN_USER_IDS` controls who
may change the model or generation settings, and an empty value grants that
ability to nobody. Request, token-budget, and concurrency limits are per user;
zero disables the corresponding limit. These counters are process-local and
reset on restart, so replicas do not share quota state.
`SLACK_AGENT_RUNTIME_STREAM_UPDATE_INTERVAL_SECONDS` controls the minimum
delay between progress-message updates and defaults to one second.

Conversation history and settings are scoped to the Slack channel and thread.
History is bounded to 20 messages and 50,000 characters. The default `memory`
backend requires no setup and resets on restart. On a cache miss, the bot can
also rebuild bounded user/assistant history from Slack's
`conversations.replies` API; it can only restore conversations it is allowed
to read.

For durable history and settings, set `THREAD_STATE_BACKEND` and
`THREAD_STATE_URL`:

```dotenv
# SQLite: three slashes for a relative path, four for an absolute path
THREAD_STATE_BACKEND=sqlite
THREAD_STATE_URL=sqlite:///data/thread-state.db

# Redis
THREAD_STATE_BACKEND=redis
THREAD_STATE_URL=redis://localhost:6379/0

# PostgreSQL
THREAD_STATE_BACKEND=postgres
THREAD_STATE_URL=postgresql://bot:password@localhost:5432/bot
```

Snapshots are versioned and validated before restore. Active requests, locks,
and rate-limit counters intentionally remain process-local. Available models
are loaded from the Inference Router's `/v1/models` endpoint. Only one request
runs per thread at a time.

Optional plugins are listed by entry-point name in load order:

```dotenv
SLACK_AGENT_RUNTIME_PLUGINS=echo,incident-tools
```

An empty value loads only the built-in plugins. Installed plugins are never
imported unless explicitly selected.

## Direct MCP tools

MCP is optional and disabled by default. To enable it, copy one of the
credential-free templates and reference the resulting ignored local file:

```bash
cp examples/mcp/github.json mcp.json
```

```dotenv
SLACK_AGENT_RUNTIME_MCP_CONFIG=mcp.json
GITHUB_MCP_TOKEN=replace-me
```

Ready-to-copy templates are provided for:

- GitHub: `examples/mcp/github.json` using Streamable HTTP
- Linear: `examples/mcp/linear.json` using Linear's hosted read-only
  Streamable HTTP endpoint
- Notion: `examples/mcp/notion.json` using the official stdio package
- Slack: `examples/mcp/slack.json` using the stdio reference server
- You.com: `examples/mcp/youdotcom.json` using the official authenticated
  Streamable HTTP endpoint for search, content retrieval, discovery, and
  balance checks

Notion and Slack require Node.js plus `npx` on the host. Set
`NOTION_MCP_HEADERS` to the JSON header object expected by Notion's server.
Set `LINEAR_MCP_TOKEN` to a read-only Linear API key or OAuth access token. Set
`SLACK_MCP_BOT_TOKEN` and `SLACK_MCP_TEAM_ID` for the Slack server. These
credentials are separate from the tokens used by this Slack app. Combine entries
under one `mcpServers` object to enable multiple servers.
The standard container image does not include Node.js or MCP server binaries;
use Streamable HTTP servers or build a derived image containing each configured
stdio executable. Mount the MCP JSON read-only and set
`SLACK_AGENT_RUNTIME_MCP_CONFIG` to its container path.

Each server supports `autoApprove`, a list of original MCP tool names. A tool
runs without confirmation only when it is listed there **and** the server marks
it with the MCP `readOnlyHint`. Write tools, tools without annotations, and all
other tools require an app administrator to approve an opaque Slack button.
Arguments and results are never included in buttons, logs, traces, or metrics.

```json
{
  "mcpServers": {
    "example": {
      "transport": "streamable-http",
      "url": "${EXAMPLE_MCP_URL}",
      "headers": {"Authorization": "Bearer ${EXAMPLE_MCP_TOKEN}"},
      "autoApprove": ["read_record"]
    }
  }
}
```

Environment references use the exact `${NAME}` form and startup fails if one
is unset. Stdio servers use `transport: "stdio"` with `command`, optional
`args`, `cwd`, and `env`. Tool calls are bounded by configurable round,
timeout, approval, and result-size limits documented in `.env.example`.

MCP connects directly from this process; no MCP gateway is required. A gateway
can later centralize discovery, authentication, and audit policy without
changing the agent loop. MCP is not a sandbox. Stdio servers and every tool
they expose run with this process's permissions, so install only trusted
servers and isolate command-executing tools separately.

In-flight runs and approvals are process-local. Keep one runtime replica when
using approvals, matching the existing Socket Mode and active-request
constraint.

## GitHub Actions coding jobs

The optional coding layer lets an app administrator request a feature from
Slack and receive a pull request in the same thread:

```text
code owner/repository Add validation and tests for the import endpoint
```

When a server named `notion` is configured through direct MCP, the runtime can
retrieve and normalize a specification before dispatch:

```text
code owner/repository from notion https://www.notion.so/example-spec
```

Only tools discovered from the `notion` MCP server are exposed during this
step. At least one Notion tool must complete successfully; otherwise no coding
job is created. The normal MCP approval buttons remain mandatory unless the
operator explicitly configured a read-only tool for automatic approval. The
normalized requirements are bounded by the coding prompt limit and are not
stored in thread plugin state.

Review a specification without starting a coding job:

```text
spec review from notion https://www.notion.so/example-spec
```

The review leads with its conclusion, separates facts, assumptions, decisions,
and open questions, identifies missing owners or deadlines without inventing
them, and returns a revised specification in the Slack thread.

Enable it on the bot with an exact repository allowlist:

```dotenv
SLACK_AGENT_RUNTIME_GITHUB_TOKEN=github_pat_replace_me
SLACK_AGENT_RUNTIME_CODING_REPOSITORIES=owner/repository
```

Use a fine-grained PAT or GitHub App installation token limited to those
repositories with **Contents: write**, **Actions: read**, and **Pull requests:
read** permissions. The bot token only dispatches and inspects jobs. Copy
`examples/coding-job/coding-agent.yml` into each target repository as
`.github/workflows/coding-agent.yml`, add the four target-repository secrets
listed in `examples/coding-job/README.md`, and allow Actions to create pull
requests. Pin both reusable-workflow references to a release tag or commit SHA
in production.

```mermaid
sequenceDiagram
    actor Admin as Slack administrator
    participant Bot as Slack Agent Runtime
    participant Actions as GitHub Actions
    participant Worker as Coding worker (inference credentials only)
    participant PR as Pull request

    Admin->>Bot: code owner/repository request
    Bot->>Actions: repository_dispatch
    Actions->>Worker: checkout + prompt + inference credentials
    Worker-->>Actions: validated files + PR metadata
    Actions->>PR: credentialed branch push and PR creation
    Actions-->>Admin: status and PR link in Slack thread
```

GitHub Actions is the durable source of job status. Thread state keeps only the
latest job ID, repository, and queue time. The worker never receives GitHub or
Slack credentials and cannot modify `.git`, `.github/workflows`, environment
files, secrets, or private-key files. It uses bounded file/search/write tools,
an executable allowlist, timeouts, changed-file and diff limits, and read-only
Git commands. The later workflow steps alone can commit, push, create a PR, and
post to Slack. No step approves or merges the PR.

This is a containment boundary, not proof that generated code is safe. Tests
execute code from the target repository, prompts and Actions logs are visible
to repository collaborators, and the worker's allowlisted command runtimes can
run repository code. Install the workflow only in trusted repositories, keep
secrets out of the checkout, review every generated PR, and use additional
runner isolation for hostile code.

Notion credentials stay in the Slack runtime and are never passed to GitHub
Actions. The normalized specification is included in the repository-dispatch
payload, so it and the resulting workflow metadata are visible to collaborators
who can inspect Actions for the target repository.

## Pull request review and approval

App administrators can browse an allowlisted repository and start an
independent review from Slack:

```text
prs owner/repository
review owner/repository#42
review owner/repository#42 model muse
review status
```

`prs` returns up to the configured page size with a **Review** button for each
PR. An explicit review model must exist in the Inference Router's `/v1/models`
response; otherwise the current thread model is used.

Install `examples/coding-job/pr-review.yml` as
`.github/workflows/pr-review.yml` in every target repository and pin both
reusable-workflow references to a release tag or commit SHA. Add
`PR_REVIEW_GITHUB_TOKEN` alongside the inference and Slack secrets. This token
should belong to a reviewer identity different from the PR author and have
**Contents: read**, **Pull requests: write**, and **Checks: read** access.
When the token belongs to the PR author, an approval verdict is safely
downgraded to a non-approving review comment.

The review process receives only the PR title, body, exact diff, selected
model, and inference credentials. It treats all PR content as untrusted data
and emits strict structured findings. A separate credentialed publication
step verifies that the head SHA is unchanged and applies this deterministic
policy:

- any critical or high finding → `REQUEST_CHANGES`;
- only medium/low findings or no findings, with successful required checks →
  `APPROVE`;
- draft PR, absent/pending/failing required checks → `COMMENT` with approval
  withheld.

The reviewer never merges. Branch protection and required CI remain the final
enforcement boundary. AI severity classification can be wrong, so repositories
with high security or regulatory risk should disable automatic approval by
keeping required checks unsatisfied or adapting the wrapper to comment-only.
PR titles and metadata are visible in the Slack channel where `prs` is run;
keep repository, workspace, channel, user, and administrator allowlists narrow.

Web search uses the Inference Router's `web_search` server tool. Search results
include extracted highlights, and Markdown citations are converted to Slack
links. Responses stream into one throttled progress message and are split into
Slack-safe chunks when complete. Incomplete Markdown is only a preview; final
formatting and usage metadata are applied after the terminal SSE event.

Adding 👍 or 👎 to a bot response emits structured feedback containing the
model and gateway request ID. Provider details remain in Inference Router logs
and can be correlated by request ID; they are not exposed to Slack.

## Architecture and extension points

- `src/slack_agent_runtime/domain/` contains commands, state, policy,
  and shared validation.
- `inference/` contains the provider protocol/client and telemetry.
- `storage/` contains versioned snapshots and persistence adapters.
- `mcp/` contains direct server configuration, sessions, discovery, and
  approvals.
- `coding/` contains GitHub dispatch, workspace-bounded tools, the coding
  worker loop, and structured PR review.
- `plugins/` contains typed contracts and the built-in plugins.
- `slack/` contains Bolt wiring, payload parsing, Block Kit, history,
  formatting, and delivery.
- `services/` separates coordination, command execution, the bounded agent
  loop, completion execution, and thread-state access.
- `entrypoints/` owns CLI and ASGI lifecycles.

To add an inference backend, implement the small `InferenceClient` protocol and
inject it into `SlackAgentRuntime`.

## Plugins

A plugin is a normal Python package exposing a zero-argument factory through
the `slack_agent_runtime.plugins` entry-point group. The factory
returns a `PluginDefinition` with a lowercase name, async lifecycle hooks,
and command, event, action, or view routes. Action and view identifiers must
use the plugin's namespace, such as `polls.vote`.

The complete working example is in `examples/echo_plugin/`. Install and enable
it locally with:

```bash
pip install -e ./examples/echo_plugin
SLACK_AGENT_RUNTIME_PLUGINS=echo slack-agent --check
SLACK_AGENT_RUNTIME_PLUGINS=echo slack-agent
```

Its package metadata registers the factory:

```toml
[project.entry-points."slack_agent_runtime.plugins"]
echo = "slack_agent_runtime_echo.plugin:create_plugin"
```

Each route receives one typed route-specific context. Command contexts expose a
narrow `PluginHost`; plugins can use `get_plugin_state`, `set_plugin_state`, and
`clear_plugin_state` to retain JSON-compatible data under their own namespace.
This data follows the configured memory, SQLite, Redis, or PostgreSQL backend.
Plugins can also add `HelpEntry` values to `PluginDefinition.help_entries`;
only help from currently enabled plugins is shown.

Plugin guarantees:

- built-ins always load first and optional plugins follow `SLACK_AGENT_RUNTIME_PLUGINS` order;
- duplicate names or routes and invalid namespaces fail at startup;
- missing or broken selected packages fail readiness with an actionable error;
- handler failures are logged without secrets and do not stop Socket Mode;
- lifecycle startup is ordered, rollback is automatic, and shutdown is
  reverse-ordered;
- unselected installed packages are not imported.

Plugins execute trusted Python inside the bot process. Only install reviewed
packages and keep `SLACK_AGENT_RUNTIME_PLUGINS` under deployment-admin control. Plugins must
not log Slack message content, credentials, or upstream response bodies.

Container deployments need an image containing each selected plugin. For
example:

```dockerfile
FROM ghcr.io/OWNER/slack-agent-runtime:VERSION
USER root
COPY slack_agent_runtime_echo-*.whl /tmp/
RUN pip install --no-cache-dir /tmp/slack_agent_runtime_echo-*.whl
USER 10001:10001
```

The optional FastAPI process remains available for container deployments:

```bash
uvicorn slack_agent_runtime.entrypoints.app:app --host 127.0.0.1 --port 6016
curl --fail http://localhost:6016/healthcheck
```

## Production deployment

The Docker image uses a multi-stage build, runs as UID/GID `10001`, and includes
the Redis and PostgreSQL drivers. Compose binds the health endpoint to
`127.0.0.1` and uses `.env` by default:

```bash
docker compose up --build
curl --fail http://127.0.0.1:6016/healthcheck
docker compose down
```

For SQLite in a container, mount a writable data volume and point
`THREAD_STATE_URL` at it. The supplied Compose service otherwise has a
read-only filesystem and only a temporary `/tmp`.

The Helm chart uses a referenced Kubernetes Secret, restricted security
contexts, health probes, and a `Recreate` strategy. It intentionally permits
one replica: Socket Mode can distribute events between connections, while
active requests and quota counters are process-local. See the chart README for
Redis/PostgreSQL and OpenTelemetry examples.

## Observability

Logs are structured JSON. Known credential values and sensitive fields,
including message content, are redacted automatically. Set an OTLP/gRPC
endpoint to export traces and metrics:

```dotenv
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
OTEL_EXPORTER_OTLP_INSECURE=true
OTEL_EXPORTER_OTLP_HEADERS=authorization=Bearer%20replace-me
```

Without an endpoint, telemetry remains a no-op. Exported data includes
`inference.request` spans and request, error, latency, input/output/total token,
web-search, model, and estimated-cost attributes or metrics.

Cost estimates are opt-in because model prices vary by provider:

```dotenv
INFERENCE_ROUTER_MODEL_PRICING_JSON={"model-id":{"input_usd_per_million_tokens":1.0,"output_usd_per_million_tokens":3.0}}
```

## CI and event fixtures

`.github/workflows/ci.yml` runs Ruff formatting/linting, strict mypy, pytest,
dependency auditing, and Helm lint/render checks. `container.yml` builds the
image and verifies its non-root user and health check. Dependabot tracks Python
and GitHub Actions dependencies.

Representative Slack payloads live in `tests/fixtures/slack/`.
`tests/integration/test_slack_events.py` replays mentions, direct messages, follow-ups,
button actions, modal submissions, and feedback through the real event/service
boundary with fake Slack and inference clients:

```bash
uv run pytest -q tests/integration/test_slack_events.py
```

## Releases, signing, and SBOMs

Pushing a SemVer tag such as `v0.1.0` runs `release.yml`. The tag must match
the versions in `pyproject.toml` and the chart's `version` and `appVersion`.
The workflow:

- reruns all quality gates;
- uploads the wheel, source archive, packaged Helm chart, SPDX JSON SBOM, and
  SHA-256 checksums to a generated GitHub Release;
- publishes signed `linux/amd64` and `linux/arm64` images to GHCR with version,
  major/minor, major, and `latest` tags;
- attaches the SBOM and GitHub build-provenance attestations to the immutable
  image digest.

Create a release after updating all three version declarations:

```bash
uv lock
git commit -am "release: v0.1.0"
git tag v0.1.0
git push origin main v0.1.0
```

Verify the keyless signature and attached SBOM with Cosign:

```bash
IMAGE=ghcr.io/OWNER/REPOSITORY:0.1.0
IDENTITY=https://github.com/OWNER/REPOSITORY/.github/workflows/release.yml@refs/tags/v0.1.0

cosign verify \
  --certificate-identity "$IDENTITY" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  "$IMAGE"
cosign verify-attestation \
  --type spdxjson \
  --certificate-identity "$IDENTITY" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  "$IMAGE"
```

The workflow uses GitHub OIDC for short-lived signing identities, so no private
signing key or additional repository secret is required.

## Development

```bash
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run coverage run -m pytest -q
uv run coverage report
uv build
```

## License

Released under the [MIT License](LICENSE).
