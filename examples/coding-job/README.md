# Target repository setup

Copy `coding-agent.yml` to `.github/workflows/coding-agent.yml`,
`pr-review.yml` to `.github/workflows/pr-review.yml`, and
`security-audit.yml` to `.github/workflows/security-audit.yml` in each trusted
target repository. Pin `runtime_ref` and all reusable-workflow references to a
release tag or commit SHA for production.

Add these repository Actions secrets:

- `INFERENCE_ROUTER_BASE_URL`
- `INFERENCE_ROUTER_API_KEY`
- `INFERENCE_ROUTER_MODEL`
- `SLACK_CODING_BOT_TOKEN`
- `PR_REVIEW_GITHUB_TOKEN`

In **Settings → Actions → General → Workflow permissions**, allow read and write access and
permit GitHub Actions to create pull requests. The workflow creates an `agent/<job-id>` branch
and a pull request; it never approves or merges it.

`PR_REVIEW_GITHUB_TOKEN` must use a separate GitHub App or bot account that is
not the pull request author. Grant only **Contents: read**, **Pull requests:
write**, and **Checks: read** for the target repository. The review workflow
requests changes for critical/high findings, approves only when required checks
are successful, and otherwise leaves a comment. It never merges.

The repository-dispatch payload and Actions logs are visible to people who can read the
repository. Install this workflow only in repositories whose code and collaborators are trusted.

The security-audit workflow is read-only and uses the same inference and Slack
secrets. It runs the pinned Cloudflare security-audit skill without exposing a
target-code execution tool, validates `findings.json` and
`coverage-ledger.json`, and uploads the canonical reports as an Actions
artifact. Repository source is sent to the configured inference provider.
