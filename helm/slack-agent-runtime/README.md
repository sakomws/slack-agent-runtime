# Slack Agent Runtime Helm Chart

The chart deploys one Socket Mode bot replica with a non-root, read-only
security context and HTTP health probes.

Create credentials without storing them in a values file:

```bash
kubectl create secret generic slack-agent-runtime \
  --from-literal=SLACK_BOT_TOKEN='xoxb-...' \
  --from-literal=SLACK_APP_TOKEN='xapp-...' \
  --from-literal=INFERENCE_ROUTER_API_KEY='...'
```

Install:

```bash
helm upgrade --install model-relay ./helm/slack-agent-runtime \
  --set image.repository=ghcr.io/OWNER/slack-agent-runtime \
  --set image.tag=VERSION \
  --set config.baseUrl=https://api.example.com \
  --set config.model=example-model \
  --set config.adminUserIds=U123
```

Use a private values file for non-secret policy configuration. The root
`.gitignore` excludes `values.local.yaml`.

Plugins must already be installed in a custom image. Enable their
entry-point names in load order:

```yaml
config:
  plugins: echo,incident-tools
```

Only explicitly listed plugins load. A missing or broken selected plugin fails
startup so the health probes do not report a partially configured bot.

To retain thread history and settings in Redis or PostgreSQL, add the DSN to
the Secret and select the backend:

```bash
kubectl create secret generic slack-agent-runtime \
  --from-literal=SLACK_BOT_TOKEN='xoxb-...' \
  --from-literal=SLACK_APP_TOKEN='xapp-...' \
  --from-literal=INFERENCE_ROUTER_API_KEY='...' \
  --from-literal=THREAD_STATE_URL='redis://redis:6379/0' \
  --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install model-relay ./helm/slack-agent-runtime \
  --set image.repository=ghcr.io/OWNER/slack-agent-runtime \
  --set image.tag=VERSION \
  --set config.baseUrl=https://api.example.com \
  --set config.model=example-model \
  --set config.threadStateBackend=redis
```

Use `postgres` with a `postgresql://...` DSN for PostgreSQL. SQLite is better
suited to local or single-host runs because this chart does not provision a
persistent volume.

Direct MCP can be configured with inline non-secret JSON or an existing
ConfigMap. Put referenced tokens in separate Secrets:

```yaml
# values.local.yaml
mcp:
  configJson: |
    {"mcpServers":{"github":{"transport":"streamable-http","url":"https://api.githubcopilot.com/mcp/","headers":{"Authorization":"Bearer ${GITHUB_MCP_TOKEN}"},"autoApprove":[]}}}
  envFromSecrets:
    - slack-agent-runtime-mcp
```

```bash
kubectl create secret generic slack-agent-runtime-mcp \
  --from-literal=GITHUB_MCP_TOKEN='...'
```

Set `mcp.existingConfigMap` instead of `mcp.configJson` when another deployment
system manages the JSON. The chart mounts it read-only. Stdio MCP servers must
already be installed in a custom image; the standard image does not include
Node.js or third-party server executables. Approval state is process-local, so
the chart continues to enforce one replica.

To enable coding-job dispatch, add the bot-side GitHub credential to the
existing Secret and configure an exact repository allowlist:

```bash
kubectl patch secret slack-agent-runtime --type merge \
  -p '{"stringData":{"SLACK_AGENT_RUNTIME_GITHUB_TOKEN":"github_pat_replace_me"}}'
helm upgrade --install model-relay ./helm/slack-agent-runtime \
  -f values.local.yaml \
  --set-string coding.repositories=OWNER/repository
```

Use a fine-grained PAT limited to the allowlisted repositories with
**Contents: write**, **Actions: read**, and **Pull requests: read** access. It
sends `repository_dispatch` events, lists PRs, and reads Actions runs;
PR-writing credentials stay inside each target repository's workflow. Install
the target workflow and repository secrets as described in
[`examples/coding-job/README.md`](../../examples/coding-job/README.md).
The same allowlist enables admin-only `prs`, `review`, and `security audit`
Slack commands. Review publication uses the separate
`PR_REVIEW_GITHUB_TOKEN` stored in each target repository, never in this Helm
release. Security audits are source-only Actions jobs and use the target
repository's inference and Slack secrets.

OpenTelemetry export can be enabled with:

```yaml
# values.local.yaml
observability:
  otlpEndpoint: http://otel-collector.observability:4317
  otlpInsecure: "true"
  modelPricingJson: >-
    {"model-id":{"input_usd_per_million_tokens":1,"output_usd_per_million_tokens":3}}
```

If the collector needs authentication, store its standard OTLP header string
in the existing Secret and set the key name:

```bash
kubectl patch secret slack-agent-runtime --type merge \
  -p '{"stringData":{"OTEL_EXPORTER_OTLP_HEADERS":"authorization=Bearer%20replace-me"}}'
helm upgrade --install model-relay ./helm/slack-agent-runtime \
  -f values.local.yaml \
  --set secret.keys.otelHeaders=OTEL_EXPORTER_OTLP_HEADERS
```

The chart intentionally enforces one replica. Durable backends share history
and settings, but active requests and quota counters remain process-local, and
multiple Socket Mode connections can split related Slack events.
